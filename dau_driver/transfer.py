from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dau_core.capabilities import DmaConstraints

from .errors import TransferValidationError


class DmaDirection(StrEnum):
    H2C = "h2c"
    C2H = "c2h"


@dataclass(frozen=True)
class DmaTransfer:
    direction: DmaDirection
    device_address: int
    byte_count: int
    channel_path: Path
    timeout_seconds: float = 5.0


def validate_transfer(direction: DmaDirection, device_address: int, byte_count: int, constraints: DmaConstraints) -> None:
    if byte_count <= 0:
        raise TransferValidationError("byte_count must be positive")
    if device_address < 0:
        raise TransferValidationError("device_address cannot be negative")
    if byte_count > constraints.max_transfer_bytes:
        raise TransferValidationError(f"byte_count exceeds max_transfer_bytes {constraints.max_transfer_bytes}")
    if not constraints.is_aligned(device_address):
        raise TransferValidationError(f"device_address must be aligned to {constraints.alignment_bytes} bytes")
    if not constraints.is_aligned(byte_count):
        raise TransferValidationError(f"byte_count must be aligned to {constraints.alignment_bytes} bytes")
    if direction == DmaDirection.H2C and constraints.write_channels <= 0:
        raise TransferValidationError("H2C channel is not available")
    if direction == DmaDirection.C2H and constraints.read_channels <= 0:
        raise TransferValidationError("C2H channel is not available")
