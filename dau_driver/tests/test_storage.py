from __future__ import annotations

import pytest

from dau_driver.storage import StaleHandleError, StorageAllocator, StorageExhaustedError


def test_allocate_respects_alignment_and_reserved_regions() -> None:
    # reserve a 1 MiB window near the start (a lane-output region)
    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 30, reserved=((0x1000, 1 << 20),), align=64)
    a = alloc.allocate(100)
    assert a.base % 64 == 0 and a.capacity == 100
    assert a.base + 100 <= 0x1000  # fits before the reserved window
    b = alloc.allocate(8192)  # cannot fit before it; placed after it
    assert b.base >= 0x1000 + (1 << 20)
    assert b.base % 64 == 0


def test_free_bumps_the_generation_so_stale_handles_fault() -> None:
    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20)
    h1 = alloc.allocate(4096)
    alloc.free(h1)
    h2 = alloc.allocate(4096)  # id and space may be reused...
    assert h2.generation > h1.generation or h2.id != h1.id
    with pytest.raises(StaleHandleError):
        alloc.resolve(h1)  # ...but the OLD handle never resolves
    assert alloc.resolve(h2) == h2.base


def test_double_free_is_refused() -> None:
    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20)
    handle = alloc.allocate(64)
    alloc.free(handle)
    with pytest.raises(StaleHandleError):
        alloc.free(handle)


def test_exhaustion_raises_before_anything_is_staged() -> None:
    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=8192)
    alloc.allocate(8192)
    with pytest.raises(StorageExhaustedError):
        alloc.allocate(1)


def test_freed_space_is_reusable() -> None:
    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=8192)
    first = alloc.allocate(8192)
    alloc.free(first)
    second = alloc.allocate(8192)  # the whole tier again
    assert second.base == first.base


def test_stage_writes_through_the_device_and_checks_bounds() -> None:
    class FakeDevice:
        def __init__(self):
            self.writes = []

        def write_dma(self, *, device_address, data):
            self.writes.append((device_address, len(data)))

    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20)
    handle = alloc.allocate(4096)
    device = FakeDevice()
    alloc.stage(device, handle, b"\x00" * 4096)
    assert device.writes == [(handle.base, 4096)]
    with pytest.raises(ValueError, match="exceeds"):
        alloc.stage(device, handle, b"\x00" * 4097)


def test_a_handle_from_another_allocator_is_refused() -> None:
    # two tiers, both issuing {id=0, generation=1}: a handle must bind to the
    # allocator that issued it, or freeing through the wrong one corrupts spans
    a = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20)
    b = StorageAllocator(tier="ddr-sodimm", capacity_bytes=1 << 20)
    foreign = a.allocate(4096)
    b.allocate(4096)
    with pytest.raises(StaleHandleError):
        b.resolve(foreign)
    with pytest.raises(StaleHandleError):
        b.free(foreign)


def test_a_forged_handle_never_frees_live_space() -> None:
    import dataclasses

    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20)
    real = alloc.allocate(4096)
    forged = dataclasses.replace(real, base=0, capacity=1 << 20)
    with pytest.raises(StaleHandleError):
        alloc.free(forged)
    assert alloc.resolve(real) == real.base  # the real allocation survives


def test_reserved_regions_must_be_positive_and_inside_the_tier() -> None:
    with pytest.raises(ValueError, match="positive size"):
        StorageAllocator(tier="t", capacity_bytes=100, reserved=((50, -10),))
    with pytest.raises(ValueError, match="outside the tier"):
        StorageAllocator(tier="t", capacity_bytes=100, reserved=((90, 20),))


def test_recycled_ids_carry_bumped_generations() -> None:
    # the generation guard must be LIVE, not decorative: a freed id is
    # recycled (as a fixed-slot hardware handle table would) and the old
    # handle's generation no longer matches
    alloc = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20)
    h1 = alloc.allocate(64)
    alloc.free(h1)
    h2 = alloc.allocate(64)
    assert h2.id == h1.id  # the slot is reused...
    assert h2.generation == h1.generation + 1  # ...at the next generation
    with pytest.raises(StaleHandleError):
        alloc.resolve(h1)
    assert alloc.resolve(h2) == h2.base
