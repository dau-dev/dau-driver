from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from dau_core.capabilities import DeviceCapabilities
from dau_core.registers import RegisterOffset

from .errors import RegisterAccessError
from .status import DeviceCounters, DeviceStatus
from .transfer import DmaDirection, DmaTransfer, validate_transfer
from .xdma_io import read_exact_at, write_all_at


@dataclass(frozen=True)
class DauDevice:
    name: str
    backend: str
    user_path: Path
    h2c_path: Path
    c2h_path: Path
    capabilities: DeviceCapabilities

    def read_register(self, offset: RegisterOffset) -> int:
        return read_u32(self.user_path, offset)

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(
            raw_status=self.read_register(RegisterOffset.STATUS),
            last_error=self.read_register(RegisterOffset.LAST_ERROR),
        )

    def read_counters(self) -> DeviceCounters:
        return DeviceCounters(
            h2c_bytes=read_u64(self.user_path, RegisterOffset.H2C_BYTES_LOW),
            c2h_bytes=read_u64(self.user_path, RegisterOffset.C2H_BYTES_LOW),
            rows=read_u64(self.user_path, RegisterOffset.ROWS_LOW),
            batches=self.read_register(RegisterOffset.BATCHES),
            stalls=self.read_register(RegisterOffset.STALLS),
        )

    def prepare_transfer(
        self,
        direction: DmaDirection,
        *,
        device_address: int,
        byte_count: int,
        timeout_seconds: float = 5.0,
    ) -> DmaTransfer:
        validate_transfer(direction, device_address, byte_count, self.capabilities.dma)
        return DmaTransfer(
            direction=direction,
            device_address=device_address,
            byte_count=byte_count,
            channel_path=self.h2c_path if direction == DmaDirection.H2C else self.c2h_path,
            timeout_seconds=timeout_seconds,
        )

    def read_dma(self, *, device_address: int, byte_count: int, timeout_seconds: float = 5.0) -> bytes:
        transfer = self.prepare_transfer(
            DmaDirection.C2H,
            device_address=device_address,
            byte_count=byte_count,
            timeout_seconds=timeout_seconds,
        )
        return read_exact_at(transfer.channel_path, transfer.device_address, transfer.byte_count)

    def write_dma(
        self,
        *,
        device_address: int,
        data: bytes | bytearray | memoryview,
        timeout_seconds: float = 5.0,
    ) -> DmaTransfer:
        transfer = self.prepare_transfer(
            DmaDirection.H2C,
            device_address=device_address,
            byte_count=len(data),
            timeout_seconds=timeout_seconds,
        )
        write_all_at(transfer.channel_path, transfer.device_address, data)
        return transfer


def read_u32(path: Path, offset: RegisterOffset) -> int:
    try:
        with path.open("rb") as register_file:
            register_file.seek(int(offset))
            register_bytes = register_file.read(4)
    except OSError as exc:
        raise RegisterAccessError(f"could not read {offset.name} from {path}") from exc

    if len(register_bytes) != 4:
        raise RegisterAccessError(f"short read for {offset.name} from {path}")
    return struct.unpack("<I", register_bytes)[0]


def read_u64(path: Path, offset: RegisterOffset) -> int:
    try:
        with path.open("rb") as register_file:
            register_file.seek(int(offset))
            register_bytes = register_file.read(8)
    except OSError as exc:
        raise RegisterAccessError(f"could not read {offset.name} from {path}") from exc

    if len(register_bytes) != 8:
        raise RegisterAccessError(f"short read for {offset.name} from {path}")
    return struct.unpack("<Q", register_bytes)[0]
