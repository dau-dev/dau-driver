from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import pytest

from dau_core.registers import DAU_MAGIC_WORD, RegisterMapVersion, RegisterOffset, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode
from dau_driver import DmaDirection, TransferValidationError, WrongBitstreamError, discover_devices, identify_device


@dataclass(frozen=True)
class FakeXdmaPaths:
    user: Path
    h2c: Path
    c2h: Path


def test_discover_devices_returns_empty_list_when_no_xdma_paths(tmp_path: Path) -> None:
    assert discover_devices(tmp_path) == []


def test_identify_device_rejects_generic_xdma_bitstream(tmp_path: Path) -> None:
    paths = write_fake_xdma_device(tmp_path, magic=0x58584D41)

    with pytest.raises(WrongBitstreamError, match="DAU magic"):
        identify_device(paths.user)


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
    assert device.capabilities.supports(OperationCode.AVERAGE, LogicalType.INT32)
    assert device.capabilities.supports(OperationCode.AVERAGE, LogicalType.FLOAT32)


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
) -> FakeXdmaPaths:
    user_path = root / f"xdma{index}_user"
    h2c_path = root / f"xdma{index}_h2c_0"
    c2h_path = root / f"xdma{index}_c2h_0"

    register_image = bytearray(256)
    write_u32(register_image, RegisterOffset.MAGIC, magic)
    write_u32(register_image, RegisterOffset.REGISTER_MAP_VERSION, register_map_version.to_u32())
    write_u32(register_image, RegisterOffset.STREAM_PROTOCOL_VERSION, stream_protocol_version.to_u32())
    write_u32(register_image, RegisterOffset.PLATFORM_ID, int.from_bytes(platform_id.encode("ascii"), "little"))
    write_u32(register_image, RegisterOffset.STATUS, status)
    write_u32(register_image, RegisterOffset.OPERATOR_BITMAP, operator_bitmap)
    write_u32(register_image, RegisterOffset.LAST_ERROR, last_error)
    write_u64(register_image, RegisterOffset.H2C_BYTES_LOW, h2c_bytes)
    write_u64(register_image, RegisterOffset.C2H_BYTES_LOW, c2h_bytes)
    write_u64(register_image, RegisterOffset.ROWS_LOW, rows)
    write_u32(register_image, RegisterOffset.BATCHES, batches)
    write_u32(register_image, RegisterOffset.STALLS, stalls)
    user_path.write_bytes(register_image)
    h2c_path.write_bytes(b"")
    c2h_path.write_bytes(b"")

    return FakeXdmaPaths(user=user_path, h2c=h2c_path, c2h=c2h_path)


def write_u32(register_image: bytearray, offset: RegisterOffset, value: int) -> None:
    struct.pack_into("<I", register_image, int(offset), value)


def write_u64(register_image: bytearray, offset: RegisterOffset, value: int) -> None:
    struct.pack_into("<Q", register_image, int(offset), value)
