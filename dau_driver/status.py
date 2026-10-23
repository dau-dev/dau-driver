from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceStatus:
    # STATUS as read. No shell assigns its bits yet, so it is not decoded
    raw_status: int
    last_error: int


@dataclass(frozen=True)
class DeviceCounters:
    h2c_bytes: int
    c2h_bytes: int
    rows: int
    batches: int
    stalls: int
