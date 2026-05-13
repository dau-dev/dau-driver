from __future__ import annotations

from pathlib import Path

import pytest
from dau_core.capabilities import DeviceCapabilities, DmaConstraints
from dau_core.registers import RegisterMapVersion, StreamProtocolVersion

from dau_driver import DauDevice, DmaDirection, TransferIoError, TransferValidationError, device as device_module
from dau_driver.xdma_io import read_exact_at, write_all_at


def test_transfer_validation_rejects_zero_length_and_unavailable_direction(tmp_path: Path) -> None:
    device = DauDevice(
        name="xdma0",
        backend="linux-xdma",
        user_path=tmp_path / "xdma0_user",
        h2c_path=tmp_path / "xdma0_h2c_0",
        c2h_path=tmp_path / "xdma0_c2h_0",
        capabilities=DeviceCapabilities(
            platform_id="NITE",
            pci_vendor_id=0x10EE,
            pci_device_id=0x7011,
            register_map_version=RegisterMapVersion(0, 1),
            stream_protocol_version=StreamProtocolVersion(0, 1),
            dma=DmaConstraints(alignment_bytes=64, descriptor_bytes=32, max_transfer_bytes=4096, read_channels=0, write_channels=1),
            operators=(),
        ),
    )

    with pytest.raises(TransferValidationError, match="positive"):
        device.prepare_transfer(DmaDirection.H2C, device_address=0x1000, byte_count=0)

    with pytest.raises(TransferValidationError, match="C2H channel"):
        device.prepare_transfer(DmaDirection.C2H, device_address=0x1000, byte_count=64)


def test_blocking_dma_write_uses_h2c_channel_at_device_address(tmp_path: Path) -> None:
    device = make_fake_device(tmp_path)
    payload = bytes(range(64))
    device.h2c_path.write_bytes(b"\x00" * 8192)

    transfer = device.write_dma(device_address=0x1000, data=payload)

    assert transfer.direction == DmaDirection.H2C
    assert transfer.channel_path == device.h2c_path
    assert device.h2c_path.read_bytes()[0x1000 : 0x1000 + len(payload)] == payload


def test_blocking_dma_read_uses_c2h_channel_at_device_address(tmp_path: Path) -> None:
    device = make_fake_device(tmp_path)
    payload = bytes(reversed(range(64)))
    c2h_image = bytearray(8192)
    c2h_image[0x1000 : 0x1000 + len(payload)] = payload
    device.c2h_path.write_bytes(c2h_image)

    assert device.read_dma(device_address=0x1000, byte_count=len(payload)) == payload


def test_blocking_dma_read_chunks_c2h_when_chunk_size_is_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = make_fake_device(tmp_path, read_chunk_bytes=128)
    payload = bytes(index % 256 for index in range(512))
    calls: list[tuple[Path, int, int]] = []

    def fake_read_exact_at(path: Path, offset: int, byte_count: int) -> bytes:
        calls.append((path, offset, byte_count))
        start = offset - 0x1000
        return payload[start : start + byte_count]

    monkeypatch.setattr(device_module, "read_exact_at", fake_read_exact_at)

    assert device.read_dma(device_address=0x1000, byte_count=len(payload)) == payload
    assert calls == [
        (device.c2h_path, 0x1000, 128),
        (device.c2h_path, 0x1080, 128),
        (device.c2h_path, 0x1100, 128),
        (device.c2h_path, 0x1180, 128),
    ]


def test_blocking_dma_io_wraps_missing_channel_as_transfer_error(tmp_path: Path) -> None:
    device = make_fake_device(tmp_path)

    with pytest.raises(TransferIoError, match="xdma0_c2h_0"):
        device.read_dma(device_address=0x1000, byte_count=64)


def test_dma_short_read_error_includes_offset_expected_and_actual_bytes(tmp_path: Path) -> None:
    c2h_path = tmp_path / "xdma0_c2h_0"
    c2h_path.write_bytes((b"\x00" * 0x1000) + b"abcd")

    with pytest.raises(TransferIoError, match="offset 0x1000.*expected 64 bytes.*read 4"):
        read_exact_at(c2h_path, 0x1000, 64)


def test_dma_short_write_error_includes_offset_expected_and_actual_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h2c_path = tmp_path / "xdma0_h2c_0"
    h2c_path.write_bytes(b"\x00" * 4096)

    def fake_pwrite(file_descriptor: int, data: memoryview, offset: int) -> int:
        return 0

    monkeypatch.setattr(device_module.write_all_at.__globals__["os"], "pwrite", fake_pwrite)

    with pytest.raises(TransferIoError, match="offset 0x1000.*expected 64 bytes.*wrote 0"):
        write_all_at(h2c_path, 0x1000, bytes(64))


def make_fake_device(tmp_path: Path, *, read_chunk_bytes: int | None = None) -> DauDevice:
    return DauDevice(
        name="xdma0",
        backend="linux-xdma",
        user_path=tmp_path / "xdma0_user",
        h2c_path=tmp_path / "xdma0_h2c_0",
        c2h_path=tmp_path / "xdma0_c2h_0",
        capabilities=DeviceCapabilities(
            platform_id="NITE",
            pci_vendor_id=0x10EE,
            pci_device_id=0x7011,
            register_map_version=RegisterMapVersion(0, 1),
            stream_protocol_version=StreamProtocolVersion(0, 1),
            dma=DmaConstraints(alignment_bytes=64, descriptor_bytes=32, max_transfer_bytes=4096, read_channels=1, write_channels=1),
            operators=(),
        ),
        read_chunk_bytes=read_chunk_bytes,
    )
