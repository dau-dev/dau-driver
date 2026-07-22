from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from dau_core.registers import DAU_MAGIC_WORD, RegisterMapVersion, RegisterOffset, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode

from dau_driver.errors import DeviceNotFoundError, WrongBitstreamError
from dau_driver.macos_xdma import (
    DEFAULT_SERVICE_NAMES,
    MacOSXDMAClient,
    MacOSXDMADeviceInfo,
    MacOSXDMAServiceProbe,
    discover_devices,
    identify_device,
    probe_devices,
)


@dataclass
class FakeMacOSXDMAClient:
    values: dict[tuple[int, int], int]
    info: MacOSXDMADeviceInfo = MacOSXDMADeviceInfo(vendor_id=0x10EE, device_id=0x7011, subsystem_vendor_id=0, subsystem_device_id=0)
    closed: bool = False
    memory: bytearray = field(default_factory=bytearray)

    def read_bar_u32(self, bar_index: int, offset: int) -> int:
        return self.values.get((bar_index, offset), 0)

    def write_bar_u32(self, bar_index: int, offset: int, value: int) -> None:
        self.values[(bar_index, offset)] = value

    def get_info(self) -> MacOSXDMADeviceInfo:
        return self.info

    def _ensure_memory(self, end: int) -> None:
        if len(self.memory) < end:
            self.memory.extend(b"\x00" * (end - len(self.memory)))

    def dma_read(self, device_offset: int, length: int) -> bytes:
        self._ensure_memory(device_offset + length)
        return bytes(self.memory[device_offset : device_offset + length])

    def dma_write(self, device_offset: int, data: bytes | bytearray | memoryview) -> int:
        payload = bytes(data)
        self._ensure_memory(device_offset + len(payload))
        self.memory[device_offset : device_offset + len(payload)] = payload
        return len(payload)

    def close(self) -> None:
        self.closed = True


class FakeIOKitBindings:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self._iterated = False

    def matching_iterators(self, service_name: str) -> tuple[int, ...]:
        self.requests.append(service_name)
        return (10,) if service_name == "XDMADriver" else ()

    def next_service(self, iterator: int) -> int:
        if iterator == 10 and not self._iterated:
            self._iterated = True
            return 20
        return 0

    def open_service(self, service: int) -> int:
        return 30 if service == 20 else 0

    def release(self, obj: int) -> None:
        pass

    def close(self, connection: int) -> None:
        pass


def test_macos_xdma_client_open_all_tries_canonical_service_then_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    bindings = FakeIOKitBindings()
    monkeypatch.setattr("dau_driver.macos_xdma._IOKitBindings.load", lambda: bindings)

    clients = MacOSXDMAClient.open_all()

    assert bindings.requests == list(DEFAULT_SERVICE_NAMES)
    assert len(clients) == 1
    assert clients[0].connection == 30


def test_identify_macos_xdma_device_finds_dau_register_window() -> None:
    client = FakeMacOSXDMAClient(
        {
            (2, 0x1000 + int(RegisterOffset.MAGIC)): DAU_MAGIC_WORD,
            (2, 0x1000 + int(RegisterOffset.REGISTER_MAP_VERSION)): RegisterMapVersion(0, 1).to_u32(),
            (2, 0x1000 + int(RegisterOffset.STREAM_PROTOCOL_VERSION)): StreamProtocolVersion(0, 1).to_u32(),
            (2, 0x1000 + int(RegisterOffset.PLATFORM_ID)): int.from_bytes(b"NITE", "little"),
            (2, 0x1000 + int(RegisterOffset.OPERATOR_BITMAP)): 1 << int(OperationCode.AVERAGE),
        }
    )

    device = identify_device(client, index=0)

    assert device.name == "xdma0"
    assert device.backend == "macos-xdma"
    assert device.register_window_offset == 0x1000
    assert device.read_register(RegisterOffset.MAGIC) == DAU_MAGIC_WORD
    assert device.capabilities.platform_id == "NITE"
    assert device.capabilities.pci_vendor_id == 0x10EE
    assert device.capabilities.pci_device_id == 0x7011
    assert device.capabilities.register_map_version == RegisterMapVersion(0, 1)
    assert device.capabilities.stream_protocol_version == StreamProtocolVersion(0, 1)
    assert device.capabilities.supports(OperationCode.AVERAGE, LogicalType.INT32)
    assert device.capabilities.dma.read_channels == 1
    assert device.capabilities.dma.write_channels == 1


def test_identify_macos_xdma_device_rejects_non_dau_bitstream() -> None:
    client = FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): 0x58584D41})

    with pytest.raises(WrongBitstreamError, match="DAU magic"):
        identify_device(client)


def test_discover_macos_xdma_devices_skips_unhealthy_or_wrong_bitstreams() -> None:
    clients = [
        FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): 0xFFFF_FFFF}),
        FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): 0x58584D41}),
        FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): DAU_MAGIC_WORD}),
    ]

    devices = discover_devices(client_factory=lambda: clients)

    assert [device.name for device in devices] == ["xdma2"]


def test_discover_macos_xdma_devices_reports_no_user_client_as_no_devices() -> None:
    def raise_not_found():
        raise DeviceNotFoundError("XDMADriver service not found")

    assert discover_devices(client_factory=raise_not_found) == []


def test_probe_macos_xdma_reports_services_tried_when_no_driverkit_service_is_visible() -> None:
    def raise_not_found():
        raise DeviceNotFoundError("no XDMA DriverKit service matched any candidate")

    result = probe_devices(client_factory=raise_not_found, service_names=DEFAULT_SERVICE_NAMES)

    assert result.status == "no-service"
    assert result.devices == ()
    assert result.service_names == DEFAULT_SERVICE_NAMES
    assert "com.dau-dev.xdma" in result.message
    assert "approved" in result.message


def test_probe_macos_xdma_reports_matched_service_without_open_user_client() -> None:
    result = probe_devices(
        client_factory=lambda: (),
        service_probes=(MacOSXDMAServiceProbe(service_name="com.dau-dev.xdma", matched_services=1, opened_clients=0),),
    )

    assert result.status == "no-user-client"
    assert "matched 1 service" in result.message
    assert "IOServiceOpen" in result.message


def test_probe_macos_xdma_reports_visible_service_with_wrong_bitstream() -> None:
    client = FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): 0x58584D41})

    result = probe_devices(client_factory=lambda: (client,))

    assert result.status == "wrong-bitstream"
    assert result.devices == ()
    assert client.closed is True
    assert "DAU magic" in result.message


def test_macos_xdma_dma_roundtrip_carries_buffers() -> None:
    client = FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): DAU_MAGIC_WORD})
    device = identify_device(client)

    payload = bytes(range(256)) * 16
    transfer = device.write_dma(device_address=0x2000, data=payload)
    assert transfer.byte_count == len(payload)
    assert device.read_dma(device_address=0x2000, byte_count=len(payload)) == payload


def test_macos_xdma_device_close_releases_user_client() -> None:
    client = FakeMacOSXDMAClient({(0, int(RegisterOffset.MAGIC)): DAU_MAGIC_WORD})
    device = identify_device(client)

    device.close()

    assert client.closed is True
