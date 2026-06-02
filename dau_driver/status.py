from __future__ import annotations

from dataclasses import dataclass

LINK_READY_BIT = 1 << 0
MIG_READY_BIT = 1 << 1
OPERATOR_READY_BIT = 1 << 2
RESET_ACTIVE_BIT = 1 << 3


@dataclass(frozen=True)
class DeviceStatus:
    raw_status: int
    last_error: int

    @property
    def link_ready(self) -> bool:
        return bool(self.raw_status & LINK_READY_BIT)

    @property
    def mig_ready(self) -> bool:
        return bool(self.raw_status & MIG_READY_BIT)

    @property
    def operator_ready(self) -> bool:
        return bool(self.raw_status & OPERATOR_READY_BIT)

    @property
    def reset_active(self) -> bool:
        return bool(self.raw_status & RESET_ACTIVE_BIT)


@dataclass(frozen=True)
class DeviceCounters:
    h2c_bytes: int
    c2h_bytes: int
    rows: int
    batches: int
    stalls: int
