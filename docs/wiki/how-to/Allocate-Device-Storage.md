# How to allocate device storage

This guide shows you how to hand out device addresses without hard-coding them:
one allocator per shared storage tier, generation-checked handles, and staging
through the handle rather than around it.

The host owns the allocator; tiles consume grants and never allocate. The handle
table this keeps is exactly what an allocator tile would drive, so the API does
not change when one exists.

## Create an allocator for a tier

```python
from dau_driver.storage import StorageAllocator

allocator = StorageAllocator(
    tier="ddr",
    capacity_bytes=1 << 30,
    base=0x0000_0000,
    reserved=((0x0000_0000, 0x1000),),
    align=64,
)
```

| Parameter        | Meaning                                                                                                    |
| ---------------- | ---------------------------------------------------------------------------------------------------------- |
| `tier`           | The tier's name; stamped onto every handle it issues.                                                      |
| `capacity_bytes` | The tier's size. Must be positive.                                                                         |
| `base`           | The tier's first address. Defaults to 0.                                                                   |
| `reserved`       | `(base, size)` regions carved out before anything is allocated — lane output windows, fixed staging areas. |
| `align`          | Allocation alignment. Must be a positive power of two; defaults to 64, the DMA grid.                       |

A reserved region outside `[base, base + capacity_bytes)` or with a
non-positive size raises `ValueError` at construction, so a wrong map fails
immediately rather than handing out an address inside a lane's output window.

Create one allocator per shared tier. A handle is bound to the allocator that
issued it, so a handle from one tier can never free or address another's memory.

## Allocate and resolve

```python
inputs = allocator.allocate(4096)
outputs = allocator.allocate(1024)

print(hex(inputs.base), inputs.capacity, inputs.tier)
print(hex(allocator.resolve(inputs)))
```

```text
0x1000 4096 ddr
0x1000
```

The first allocation starts at `0x1000` because the first 4 KiB were reserved.
Allocation is first-fit over the free list. `resolve(handle)` returns the device
address, generation-checked — use it wherever a register or a transfer needs an
address, rather than caching `handle.base`.

`StorageExhaustedError` is raised when no free span can hold the request, before
anything is staged:

```text
StorageExhaustedError: tier 'ddr' cannot hold 1073741824 bytes
```

## Stage data through the handle

```python
allocator.stage(device, inputs, payload)
```

Resolves the handle, checks the payload against the *live* grant — never the
handle's own copy of the capacity — and writes it with `device.write_dma`. A
payload larger than the grant raises `ValueError` rather than overrunning into
the next allocation.

`stage` and `resolve` take any object with a `write_dma` method, following the
duck-typed device convention, so a fake works here too.

## Free, and rely on the generation guard

```python
allocator.free(inputs)

allocator.resolve(inputs)
# StaleHandleError: handle 0 generation 1 is stale, forged, or from another
# allocator (current generation: 1); freed storage is never readdressed
```

Freeing bumps the handle's generation and returns the span to the free list,
coalesced with its neighbours. The id itself is recycled — a fixed-slot hardware
handle table reuses slots, and the generation is what makes that safe.

The consequence you want: freed storage is never readdressed through an old name.
A stale handle raises instead of resolving to memory that now belongs to someone
else. A double free raises for the same reason, because `free` resolves first.

## Order your teardown

Because every operation trusts the allocator's live record rather than the
handle, the only rule is not to hold a resolved address across a `free`:

```python
address = allocator.resolve(handle)   # valid now
allocator.free(handle)
device.read_dma(device_address=address, byte_count=n)   # reads whatever is there now
```

Resolve at the point of use.

## Next

- [Run scan jobs](Run-Scan-Jobs) — the input regions and output windows an
  allocator hands out.
- [Move data to and from a device](Move-Data) — the transfers that consume
  those addresses.
- [Storage handles](Storage-Handles) — why generations rather
  than plain pointers.
