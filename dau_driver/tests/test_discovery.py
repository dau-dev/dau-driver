from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import pytest
from dau_core.registers import DAU_MAGIC_WORD, RegisterBlockSnapshot, RegisterMapVersion, RegisterOffset, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode

import dau_driver.discovery as discovery_module
from dau_driver import (
    DmaDirection,
    TransferValidationError,
    UnhealthyBarError,
    WrongBitstreamError,
    device as device_module,
    discover_devices,
    identify_device,
)


@dataclass(frozen=True)
class FakeXdmaPaths:
    user: Path
    h2c: Path
    c2h: Path


def test_discover_devices_returns_empty_list_when_no_xdma_paths(tmp_path: Path) -> None:
    assert discover_devices(tmp_path) == []


def test_discover_devices_uses_macos_backend_on_darwin_default_dev_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery_module.sys, "platform", "darwin")
    monkeypatch.setattr(discovery_module.linux_xdma, "discover_devices", lambda root: [])
    monkeypatch.setattr(discovery_module, "_discover_macos_devices", lambda: ["mac-device"])

    assert discover_devices() == ["mac-device"]


def test_identify_device_rejects_generic_xdma_bitstream(tmp_path: Path) -> None:
    paths = write_fake_xdma_device(tmp_path, magic=0x58584D41)

    with pytest.raises(WrongBitstreamError, match="DAU magic"):
        identify_device(paths.user)


def test_identify_device_reports_unhealthy_bar_when_magic_reads_all_ones(tmp_path: Path) -> None:
    paths = write_fake_xdma_device(tmp_path, magic=0xFFFF_FFFF)

    with pytest.raises(UnhealthyBarError, match="all ones"):
        identify_device(paths.user)

    assert discover_devices(tmp_path) == []


def test_discover_devices_reports_one_matching_fake_device(tmp_path: Path) -> None:
    write_fake_xdma_device(tmp_path)

    devices = discover_devices(tmp_path)

    assert len(devices) == 1
    device = devices[0]
    assert device.name == "xdma0"
    assert device.backend == "linux-xdma"
    assert device.user_path == tmp_path / "xdma0_user"
    assert device.h2c_path == tmp_path / "xdma0_h2c_0"
    assert device.c2h_path == tmp_path / "xdma0_c2h_0"
    assert device.capabilities.platform_id == "NITE"
    assert device.capabilities.pci_vendor_id == 0x10EE
    assert device.capabilities.pci_device_id == 0x7011
    assert device.capabilities.register_map_version == RegisterMapVersion(0, 1)
    assert device.capabilities.stream_protocol_version == StreamProtocolVersion(0, 1)
    assert device.capabilities.dma.is_aligned(128)
    # whole-transfer reads: the chunk quirk belonged to the retired shell
    assert device.read_chunk_bytes is None
    assert device.capabilities.supports(OperationCode.AVERAGE, LogicalType.INT32)
    assert device.capabilities.supports(OperationCode.AVERAGE, LogicalType.FLOAT32)


def test_identify_device_supports_register_window_offset(tmp_path: Path) -> None:
    paths = write_fake_xdma_device(tmp_path, register_window_offset=0x1000)

    device = identify_device(paths.user)

    assert device.register_window_offset == 0x1000
    assert device.read_register(RegisterOffset.MAGIC) == DAU_MAGIC_WORD
    assert device.capabilities.platform_id == "NITE"


def test_identify_device_retries_transient_zero_magic_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the first AXI-Lite reads after a PCIe rescan can transiently return 0
    # before the link settles; the window probe must retry rather than raise
    # WrongBitstreamError on a healthy shell
    from dau_driver import linux_xdma

    paths = write_fake_xdma_device(tmp_path, register_window_offset=0x1000)
    real_read_u32 = linux_xdma.read_u32
    reads = {"n": 0}

    def flaky_read_u32(path: Path, offset: int) -> int:
        reads["n"] += 1
        # zero out the first full window sweep, then read for real
        if reads["n"] <= len(linux_xdma.REGISTER_WINDOW_OFFSETS):
            return 0
        return real_read_u32(path, offset)

    monkeypatch.setattr(linux_xdma, "read_u32", flaky_read_u32)

    device = identify_device(paths.user)

    assert device.register_window_offset == 0x1000
    assert device.read_register(RegisterOffset.MAGIC) == DAU_MAGIC_WORD


def test_identify_device_defaults_legacy_gpio_metadata(tmp_path: Path) -> None:
    user_path = tmp_path / "xdma0_user"
    h2c_path = tmp_path / "xdma0_h2c_0"
    c2h_path = tmp_path / "xdma0_c2h_0"
    register_image = bytearray(0x1100)
    struct.pack_into("<I", register_image, 0x1000 + int(RegisterOffset.MAGIC), DAU_MAGIC_WORD)
    struct.pack_into("<I", register_image, 0x1000 + int(RegisterOffset.REGISTER_MAP_VERSION), 0xFFFF_FFFF)
    struct.pack_into("<I", register_image, 0x1000 + int(RegisterOffset.STREAM_PROTOCOL_VERSION), StreamProtocolVersion(0, 1).to_u32())
    struct.pack_into("<I", register_image, 0x1000 + int(RegisterOffset.PLATFORM_ID), 0xFFFF_FFFF)
    struct.pack_into("<I", register_image, 0x1000 + int(RegisterOffset.OPERATOR_BITMAP), 1 << OperationCode.AVERAGE)
    user_path.write_bytes(register_image)
    h2c_path.write_bytes(b"")
    c2h_path.write_bytes(b"")

    device = identify_device(user_path)

    assert device.register_window_offset == 0x1000
    assert device.capabilities.platform_id == "NITE"
    assert device.capabilities.register_map_version == RegisterMapVersion(0, 1)


def test_device_decodes_status_and_counters_from_register_block(tmp_path: Path) -> None:
    write_fake_xdma_device(
        tmp_path,
        status=0b0111,
        last_error=0x42,
        h2c_bytes=0x1_0000_0040,
        c2h_bytes=0x2_0000_0080,
        rows=0x3_0000_0007,
        batches=9,
        stalls=13,
    )
    device = discover_devices(tmp_path)[0]

    status = device.read_status()
    counters = device.read_counters()

    assert status.link_ready
    assert status.mig_ready
    assert status.operator_ready
    assert not status.reset_active
    assert status.last_error == 0x42
    assert counters.h2c_bytes == 0x1_0000_0040
    assert counters.c2h_bytes == 0x2_0000_0080
    assert counters.rows == 0x3_0000_0007
    assert counters.batches == 9
    assert counters.stalls == 13


def test_device_reads_register_block_snapshot_from_dau_core_contract(tmp_path: Path) -> None:
    write_fake_xdma_device(
        tmp_path,
        platform_id="NITE",
        status=0b0011,
        last_error=0x24,
        h2c_bytes=0x1000_0040,
        c2h_bytes=0x2000_0080,
        rows=0x3000_0007,
        batches=11,
        stalls=17,
    )
    device = discover_devices(tmp_path)[0]

    assert device.read_register_block_snapshot() == RegisterBlockSnapshot(
        platform_id="NITE",
        register_map_version=RegisterMapVersion(0, 1),
        stream_protocol_version=StreamProtocolVersion(0, 1),
        status=0b0011,
        operator_bitmap=1 << OperationCode.AVERAGE,
        last_error=0x24,
        h2c_bytes=0x1000_0040,
        c2h_bytes=0x2000_0080,
        rows=0x3000_0007,
        batches=11,
        stalls=17,
    )


def test_read_u64_uses_two_u32_register_reads_for_xdma_user_bar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_path = tmp_path / "xdma0_user"
    user_path.write_bytes(b"")
    reads: list[tuple[int, int, str]] = []

    def fake_read_register_bytes(path: Path, offset: int, byte_count: int, offset_name: str) -> bytes:
        assert path == user_path
        reads.append((offset, byte_count, offset_name))
        values = {0x1014: 0x89ABCDEF, 0x1018: 0x01234567}
        return struct.pack("<I", values[offset])

    monkeypatch.setattr(device_module, "_read_register_bytes", fake_read_register_bytes)

    assert device_module.read_u64(user_path, 0x1014) == 0x01234567_89ABCDEF
    assert reads == [(0x1014, 4, "0x1014"), (0x1018, 4, "0x1018")]


def test_device_bar_health_check_reports_all_ones_for_selected_register(tmp_path: Path) -> None:
    paths = write_fake_xdma_device(tmp_path, status=0xFFFF_FFFF)
    device = identify_device(paths.user)

    with pytest.raises(UnhealthyBarError, match="STATUS.*all ones"):
        device.check_bar_health(offsets=(RegisterOffset.STATUS,))


def test_device_validates_dma_transfer_shape_before_real_xdma_io(tmp_path: Path) -> None:
    write_fake_xdma_device(tmp_path)
    device = discover_devices(tmp_path)[0]

    transfer = device.prepare_transfer(DmaDirection.H2C, device_address=0x1000, byte_count=4096)

    assert transfer.direction == DmaDirection.H2C
    assert transfer.device_address == 0x1000
    assert transfer.byte_count == 4096
    assert transfer.channel_path == tmp_path / "xdma0_h2c_0"

    with pytest.raises(TransferValidationError, match="aligned"):
        device.prepare_transfer(DmaDirection.H2C, device_address=0x1003, byte_count=4096)

    with pytest.raises(TransferValidationError, match="max_transfer_bytes"):
        device.prepare_transfer(DmaDirection.C2H, device_address=0x1000, byte_count=device.capabilities.dma.max_transfer_bytes + 64)


def write_fake_xdma_device(
    root: Path,
    *,
    index: int = 0,
    magic: int = DAU_MAGIC_WORD,
    platform_id: str = "NITE",
    register_map_version: RegisterMapVersion = RegisterMapVersion(0, 1),
    stream_protocol_version: StreamProtocolVersion = StreamProtocolVersion(0, 1),
    operator_bitmap: int = 1 << OperationCode.AVERAGE,
    status: int = 0,
    last_error: int = 0,
    h2c_bytes: int = 0,
    c2h_bytes: int = 0,
    rows: int = 0,
    batches: int = 0,
    stalls: int = 0,
    register_window_offset: int = 0,
) -> FakeXdmaPaths:
    user_path = root / f"xdma{index}_user"
    h2c_path = root / f"xdma{index}_h2c_0"
    c2h_path = root / f"xdma{index}_c2h_0"

    register_image = bytearray(register_window_offset + 256)
    write_u32(register_image, register_window_offset, RegisterOffset.MAGIC, magic)
    write_u32(register_image, register_window_offset, RegisterOffset.REGISTER_MAP_VERSION, register_map_version.to_u32())
    write_u32(register_image, register_window_offset, RegisterOffset.STREAM_PROTOCOL_VERSION, stream_protocol_version.to_u32())
    write_u32(register_image, register_window_offset, RegisterOffset.PLATFORM_ID, int.from_bytes(platform_id.encode("ascii"), "little"))
    write_u32(register_image, register_window_offset, RegisterOffset.STATUS, status)
    write_u32(register_image, register_window_offset, RegisterOffset.OPERATOR_BITMAP, operator_bitmap)
    write_u32(register_image, register_window_offset, RegisterOffset.LAST_ERROR, last_error)
    write_u64(register_image, register_window_offset, RegisterOffset.H2C_BYTES_LOW, h2c_bytes)
    write_u64(register_image, register_window_offset, RegisterOffset.C2H_BYTES_LOW, c2h_bytes)
    write_u64(register_image, register_window_offset, RegisterOffset.ROWS_LOW, rows)
    write_u32(register_image, register_window_offset, RegisterOffset.BATCHES, batches)
    write_u32(register_image, register_window_offset, RegisterOffset.STALLS, stalls)
    user_path.write_bytes(register_image)
    h2c_path.write_bytes(b"")
    c2h_path.write_bytes(b"")

    return FakeXdmaPaths(user=user_path, h2c=h2c_path, c2h=c2h_path)


def write_u32(register_image: bytearray, register_window_offset: int, offset: RegisterOffset, value: int) -> None:
    struct.pack_into("<I", register_image, register_window_offset + int(offset), value)


def write_u64(register_image: bytearray, register_window_offset: int, offset: RegisterOffset, value: int) -> None:
    struct.pack_into("<Q", register_image, register_window_offset + int(offset), value)
