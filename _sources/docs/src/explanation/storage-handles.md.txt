# Storage handles: why a generation instead of a pointer

`StorageAllocator` hands out `StorageHandle` values, not addresses. Every handle
carries a generation counter and the identity of the allocator that issued it, and
every operation re-checks both against the allocator's live record. This page is
about why a host-side allocator is built this way.

## The bug this design does not have

A conventional allocator hands back an address. Free it, allocate again, and the
new allocation may sit at the same address, so a stale reference to the old one
now reads and writes storage that belongs to something else. On a host that is a
use-after-free. On a device it is worse, because there is no page table to fault:
the DMA succeeds, the data is wrong, and nothing anywhere reports a problem.

The fix is not to hand out addresses. A handle is `(id, generation)` plus a
snapshot of its placement, and `resolve(handle)` returns an address only if the
allocator's live record still matches. Freeing bumps the generation, so a stale
handle raises `StaleHandleError` instead of resolving:

```text
handle 0 generation 1 is stale, forged, or from another allocator
(current generation: 1); freed storage is never readdressed
```

That sentence is the invariant: **freed storage is never readdressed through an
old name.** A few other checks follow from it. A double free faults, because
`free` resolves first. A handle from another allocator faults, because the
allocator token does not match. And a handle whose fields were tampered with
faults, because the comparison is against the live record, not the caller's copy.

## The live record is always authoritative

Every operation reads the allocator's record rather than the handle's own
`base` and `capacity`. `stage` checks the payload length against the *live* grant.
`free` returns the *live* span to the free list.

The reason is not malicious callers. A frozen handle passed around a program is a
copy of facts that were true when it was issued, and the allocator is the only
thing that knows whether they still are. Trusting the copy would make the
generation check pointless: you would validate the identity and then act on
unvalidated data.

## Why ids are recycled

The allocator could instead never reuse an id: a monotonic counter, no recycling,
and no need for a generation. That would be simpler, but it would not match the
hardware.

The shell's handle table has a fixed number of slots, because that is what fits
in BRAM. Slots get reused, and the generation is what makes reuse safe. Because
the software allocator already followed the same rule, the API did not change
when the table arrived in hardware: `HandleTable` programs a slot straight from
the allocator's live record, and nothing above it changed.

The general rule: shape the simple software half of a lifecycle like the
hardware half that will replace it, not like whatever is easiest in Python.

## The host allocates; tiles consume

The division of labour is that the host owns an allocator per shared storage tier
and hands out grants, and tiles consume grants and never allocate. There is no
device-side allocation path today and the API is designed so there does not need
to be one later.

Reserved regions are how the fixed parts of the map stay out of the way. Lane
output windows and fixed staging areas are carved out of the free list at
construction, so nothing can be allocated on top of them. A reserved region
outside the tier, or with a non-positive size, raises immediately, so a wrong
memory map fails at construction rather than by handing out an address inside a
lane's output window and corrupting a result three jobs later.

## Why the device has to make the check

A host-side check is enough right up until the device reads on its own. A job
over rows already resident on the device names the region it should read, and a
raw address outlasts its allocation: free the region, hand the space to someone
else, and the old address still reads without error. It reads the new owner's
bytes, and no layer above can tell that from a real answer.

`HandleTable` closes that by programming the grant into a slot and letting the
job name `(id, generation)` instead of an address. When the table disagrees with
a resolution, the shell does not start the job (the reader never issues a beat),
and the host gets a `HandleFaultError`, never results, so a caller cannot mistake
it for a job that ran and summed nothing.

It is also why `arm` validates nothing. The stale handle has to reach the
device, or the table never checks it, and the fault would be a host-side
assertion presented as a hardware one.

## What it is not

This is a first-fit allocator with a sorted, coalesced free list, and it makes no
claim beyond that. It is not concurrent, has no size classes, and does not
compact. Allocation is linear in the number of free spans.

That fits the problem. Device tiers hold a handful of long-lived regions (input
staging, output windows, resident tables), not millions of small transient
objects. Exhaustion raises before anything is staged, naming the tier
and the request:

```text
StorageExhaustedError: tier 'ddr' cannot hold 1073741824 bytes
```

One thing to watch: `stage` and `resolve` take any object with a
`write_dma` method rather than a typed device. That is the duck-typed device
convention used throughout the package, and it is what lets a fake stand in for a
board, for the same reason [the transport boundary](transport-boundary.md) is a
protocol rather than a base class.

## Related

- [Allocate device storage](../how-to/allocate-device-storage.md): the allocator
  in practice.
- [Device and discovery reference](../reference/device-and-discovery.md): every
  method and error.
- [The transport boundary](transport-boundary.md): the same duck-typing choice,
  one layer down.
