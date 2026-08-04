"""Host-side storage allocation over a platform's declared tiers.

The dumb half of the allocation lifecycle in STORAGE_AND_NOC.md: the host
owns an allocator per shared tier and hands out generation-checked handles;
tiles consume grants and never allocate. The handle table this allocator
maintains is exactly what a future allocator tile would drive — the API does
not change when it does.

Freed storage is never readdressed: ``free`` bumps the handle's generation,
and a stale handle raises instead of resolving to reused memory. Exhaustion
raises before anything is staged. ``stage``/``resolve`` take any object with
``write_dma`` (the duck-typed device convention).
"""

from __future__ import annotations

from dataclasses import dataclass


class StaleHandleError(RuntimeError):
    """A handle whose allocation was freed (or never existed) was used."""


class StorageExhaustedError(RuntimeError):
    """The tier cannot hold the requested allocation."""


@dataclass(frozen=True)
class StorageHandle:
    """One allocation: identity plus placement. ``generation`` is the
    freed-storage guard — the allocator refuses any handle whose id has been
    freed since, so reused space is never readable through an old name —
    and ``allocator`` binds the handle to the allocator that issued it, so a
    handle from one tier can never free or address another's memory. Every
    operation trusts only the allocator's LIVE record, never the handle's
    own copy of base/capacity."""

    id: int
    generation: int
    base: int
    capacity: int
    tier: str
    allocator: int


class StorageAllocator:
    """First-fit allocator over one declared tier's ``[base, base+capacity)``,
    minus reserved regions (lane-output windows, staging areas)."""

    def __init__(
        self,
        *,
        tier: str,
        capacity_bytes: int,
        base: int = 0,
        reserved: tuple[tuple[int, int], ...] = (),
        align: int = 64,
    ) -> None:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        if align <= 0 or align & (align - 1):
            raise ValueError("align must be a positive power of two")
        self.tier = tier
        self.align = align
        self._token = id(object())  # binds handles to THIS allocator instance
        # free list of [start, end) spans: the tier minus the reserved regions
        spans = [(base, base + capacity_bytes)]
        for res_base, res_size in sorted(reserved):
            if res_size <= 0:
                raise ValueError(f"reserved region ({res_base:#x}, {res_size}) must have positive size")
            if res_base < base or res_base + res_size > base + capacity_bytes:
                raise ValueError(f"reserved region ({res_base:#x}, {res_size}) lies outside the tier")
            res_end = res_base + res_size
            next_spans = []
            for start, end in spans:
                if res_end <= start or res_base >= end:
                    next_spans.append((start, end))
                    continue
                if start < res_base:
                    next_spans.append((start, res_base))
                if res_end < end:
                    next_spans.append((res_end, end))
            spans = next_spans
        self._free: list[tuple[int, int]] = spans
        self._live: dict[int, StorageHandle] = {}
        self._generations: dict[int, int] = {}
        self._next_id = 0

    def allocate(self, nbytes: int) -> StorageHandle:
        if nbytes <= 0:
            raise ValueError("nbytes must be positive")
        for index, (start, end) in enumerate(self._free):
            aligned = (start + self.align - 1) & ~(self.align - 1)
            if aligned + nbytes > end:
                continue
            # split the span around the allocation
            remainder = []
            if aligned > start:
                remainder.append((start, aligned))
            if aligned + nbytes < end:
                remainder.append((aligned + nbytes, end))
            self._free[index : index + 1] = remainder
            handle_id = self._next_id
            self._next_id += 1
            generation = self._generations.get(handle_id, 0) + 1
            self._generations[handle_id] = generation
            handle = StorageHandle(id=handle_id, generation=generation, base=aligned, capacity=nbytes, tier=self.tier, allocator=self._token)
            self._live[handle_id] = handle
            return handle
        raise StorageExhaustedError(f"tier {self.tier!r} cannot hold {nbytes} bytes (free: {self._free})")

    def free(self, handle: StorageHandle) -> None:
        self.resolve(handle)  # generation- and identity-checked: double-free faults
        live = self._live.pop(handle.id)
        self._generations[handle.id] = live.generation  # freed; next use of the id bumps past it
        self._insert_free(live.base, live.base + live.capacity)  # the LIVE span, never the caller's copy

    def resolve(self, handle: StorageHandle) -> int:
        """The handle's device address, generation-checked. Freed storage is
        never readdressed through an old name."""
        live = self._live.get(handle.id)
        if live is None or live != handle or handle.allocator != self._token:
            current = self._generations.get(handle.id)
            raise StaleHandleError(
                f"handle {handle.id} generation {handle.generation} is stale, forged, or from another "
                f"allocator (current generation: {current}); freed storage is never readdressed"
            )
        return live.base

    def stage(self, device, handle: StorageHandle, data: bytes) -> None:
        """Write ``data`` into the handle's allocation through the device."""
        base = self.resolve(handle)
        live = self._live[handle.id]
        if len(data) > live.capacity:  # the LIVE grant, never the caller's copy
            raise ValueError(f"{len(data)} bytes exceeds the handle's {live.capacity}-byte grant")
        device.write_dma(device_address=base, data=data)

    def _insert_free(self, start: int, end: int) -> None:
        # keep the free list sorted and coalesced so freed space is reusable
        spans = sorted([*self._free, (start, end)])
        merged: list[tuple[int, int]] = []
        for span_start, span_end in spans:
            if merged and span_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
            else:
                merged.append((span_start, span_end))
        self._free = merged
