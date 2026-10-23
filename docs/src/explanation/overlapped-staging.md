# Overlapped staging: why the runner ping-pongs, and when it stops

A scan over a large table is not one job. It is many, and how the host feeds them
decides how much of the device's time is spent computing rather than waiting.
This page explains the shape `run_scan_batches` takes and the two cases it
refuses to run.

## The serial version, and what it wastes

The obvious loop is: stage a batch, start the job, wait, collect, repeat. Its
problem is that DMA and compute are two independent engines and this loop runs
exactly one of them at a time. Half the wall clock is a busy PCIe link with an
idle datapath, then an idle link with a busy datapath.

The fix is a ping-pong: two input regions in device memory, alternating. While job
*i* computes out of region A, batch *i + 1*'s DMA fills region B. That is all
`input_regions` is, two distinct addresses, and it is why passing the same
address twice raises rather than serializing without telling you.

The overlap extends further than the DMA. `batches` may be a lazy iterable, and
the runner pulls the next batch on a worker thread while the current job runs, so
whatever work the iterator does (encoding rows, reading a file) overlaps compute
too. And because output regions are disjoint from input staging, the `collect`
callback reading one job's results also overlaps the next batch's DMA.

## The overlap is a claim, so it is tested as one

An overlap that degraded into serial execution would go unnoticed: the results
would still be correct, only slower. So the test suite pins it with a
transport that reports DONE only once the *next* batch's H2C write has landed. A
serial stage-after-finish runner deadlocks into the timeout against that fake; a
green run is proof the staging overlapped.

This is the general pattern for testing a concurrency property. Asserting on
timings is flaky; making the fake refuse to complete unless the property holds is
not.

## How much it saves

Less than the arithmetic suggests, and the module says so rather than implying
otherwise. Host writes and the job's own burst reads share one memory controller
on the reference board, so the gain is bounded by the bandwidth left over, not by
the ratio of DMA time to job time.

That is why the batch count is not a constant in the driver. Splitting a table
into more batches hides more of the tail but adds per-job overhead; the crossover
is measured on silicon for a given workload, not derived. The runner takes whatever
batches you hand it and the decision stays with the caller.

## The `per_key_state` guard

Some scan compositions keep state per key across the rows of a batch, such as a
grouped aggregation holding a running total per key. Slice such a workload by
row position and a key's rows land in two different jobs, each producing a partial
the host has no way to know it must merge. The results look right and are
incomplete.

So `per_key_state` is a required keyword, and when it is True the runner checks
that `batches` is the object `key_partitioned_scan_batches` returns. The check
is on the type of the iterable, not on a flag or a docstring.

This choice has a cost. It makes the runner aware of one specific batch producer, which is a coupling you would
normally avoid. The alternative is a boolean the caller sets correctly and one
day does not, on a failure mode that produces plausible numbers. Coupling one
function to one other function is the cheaper mistake.

`key_partitioned_scan_batches` assigns longest-processing-time-first: the largest
key group goes into the currently smallest batch. That needs every group's size
before it can assign anything, so the first iteration consumes a full grouping
pass, the one place the lazy pipeline is not lazy. Key groups are indivisible, so
one hot key can still dominate a batch. LPT bounds the imbalance by the largest
group; it cannot beat it.

## Two batching contracts

`key_range_scan_batches` looks like the same idea but is a different contract. It bounds the number
of *distinct keys* in a batch, because the hash-mode grouped tile's table is a
fixed-size hardware fact: a batch presenting more keys than it holds closes out
`GROUP_CAPACITY_EXCEEDED` and is invalidated.

The host could react to that close-out. Sizing the batches so it cannot happen is
better: partition the key space into ranges of `key_capacity` keys and emit one
batch per range.

The interesting difference is what each function *does not* require.
`key_partitioned_scan_batches` needs records ordered by key, because it groups
them with a single pass. `key_range_scan_batches` does not, and preserves arrival
order within each batch. The hash table needs no key ordering, and imposing one
would give up the staging freedom that tile exists to provide.

Both give the same strong property: each key lands in one batch, so merging the
per-batch group records is a concatenation rather than an additive merge. The
general merge still exists in `dau_core.operators.merge_hash_grouped` for batches
produced some other way. The device's close-out remains the safety net for a
caller whose `key_capacity` does not match the built design.

## Why a fleet cannot be stateful at all

`run_scan_batches_fleet` runs one pipeline per device over one shared lazy input,
with each batch pulled exactly once by whichever pipeline demands it. That is what
makes it self-balancing: a faster card asks for more batches, with no
pre-partitioning and no throughput model.

It also makes `per_key_state=True` impossible, so the function raises on it
rather than running with split state. A key's rows would go to whichever cards happen to be idle, and
per-key state would split across devices that never see each other's rows.
Key-partitioning the input does not help: the affinity would have to be
device-wide, which is the pre-partitioning that demand-driven scheduling exists
to avoid. It is the same guard `run_scan_batches` applies to position-sliced
batches, one level up.

`weights` biases the shared pull toward requested relative shares by weighted
round-robin. It is a bias on *pull order*, not a partition: batches stay lazy and
are never queued ahead. Demand still wins if a weighted device turns out to be
slow.

## The GIL detail that only shows up in tests

`poll_yield_seconds` defaults to `None`, meaning the status poll spins. That is
right for the real transports, which release the GIL in their register read
(`os.pread` on Linux, `ctypes` on macOS), so concurrent pipelines interleave freely
and a sleep would only add latency per poll.

It is wrong for a pure-Python transport, which is what a concurrent test uses.
There the spin holds the GIL and flattens self-balancing toward an even split
regardless of device speed: on fakes with a large speed gap the spinning split was
close to even and took twice as long as the yielding one, which followed the gap.
The parameter exists for that case only.

## What the fleet does not do

Cancellation is incomplete, and the docstring says so rather than leaving you to
discover it. A device failure wakes pipelines blocked pulling a batch, but one
already inside its status wait runs to its own completion or timeout first, so an
early failure can surface only after its peers finish.

Naming a limitation is cheaper than a caller building a timeout budget on an
assumption that does not hold.

## Related

- [Run a scan over many batches](../tutorial/first-scan-run.md): the runner and
  its guards, hands on.
- [Run scan jobs](../how-to/run-scan-jobs.md): choosing a strategy for real work.
- [Execution contracts reference](../reference/execution-contracts.md): every
  signature and constant.
