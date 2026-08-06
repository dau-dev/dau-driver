# How to run scan jobs

This guide shows you how to drive the scan-composition register contract over
many batches: choosing a batching strategy, overlapping staging with compute,
spreading work across several cards, and reading the failures when a job does not
come back.

Everything shell-specific stays yours. Lane output addresses and static
configuration are written before the run, and your `collect` callback reads each
job's results. The runner owns the input staging, the trigger, and the wait.

## Run a sequence of batches

```python
from dau_driver.scan_execution import ScanBatch, run_scan_batches

results = run_scan_batches(
    device,
    batches,
    per_key_state=False,
    input_regions=(0x0000_0000, 0x0800_0000),
    row_bytes=16,
    collect=collect_results,
    timeout_seconds=60.0,
)
```

`input_regions` must be two distinct device addresses. The runner alternates
between them, so batch *i + 1* DMAs into one region while job *i* computes out of
the other. `collect` is called once per batch after that job reports DONE, and
its return values come back in batch order.

Each batch's `payload` must be exactly `row_count * row_bytes`. The runner pads
the DMA out to the 64-byte grid while `INPUT_LENGTH` stays exact.

Your `collect` reads lane registers and output regions, which are disjoint from
the input staging — so collection overlaps the next batch's DMA too.

## Overlap the encoding as well

Pass a generator and the runner pulls each batch just before staging it, so
whatever work the iterator does happens concurrently with the running job:

```python
def batches():
    for chunk in chunks:
        yield ScanBatch(payload=encode(chunk), row_count=len(chunk))


results = run_scan_batches(device, batches(), per_key_state=False, ...)
```

A failure raised inside the iterator surfaces from the wait rather than hiding
behind its timeout.

## Choose a batching strategy

| Strategy | Use when | Guarantee |
| --- | --- | --- |
| Position slicing (build `ScanBatch` yourself) | The composition is stateless per batch. | None beyond batch size. |
| `key_partitioned_scan_batches` | The composition carries per-key state across a batch. | Each key lands in exactly one batch. |
| `key_range_scan_batches` | The composition uses a hash-mode grouped tile with a fixed key capacity. | Each batch holds at most `key_capacity` distinct keys, and each key lands in exactly one batch. |

### Key-confined batches

```python
from dau_driver.scan_execution import key_partitioned_scan_batches

batches = key_partitioned_scan_batches(
    records,
    batch_count=8,
    key=lambda record: record.symbol,
    encode=encode_rows,
)
```

Assignment is longest-processing-time-first: the largest key group goes to the
currently smallest batch. **Records must arrive ordered by key** — a key
appearing in two runs raises `ValueError`. Record order within each key is
preserved.

The first iteration consumes one complete grouping pass, because LPT needs every
group size before it can assign. After that, each batch's payload is built only
when that batch is requested. Key groups are indivisible, so one hot key can
dominate a batch.

Pass `per_key_state=True` when running these. The runner requires it for a
stateful composition and checks that the batches actually came from this function
— a position-sliced iterable raises `ValueError` rather than silently splitting a
key's state.

### Capacity-bounded batches

```python
from dau_driver.scan_execution import key_range_scan_batches

batches = key_range_scan_batches(
    records,
    key_capacity=512,
    key=lambda record: record.symbol,
    encode=encode_rows,
)
```

Size the batches so the hash table cannot overflow rather than reacting to the
device's `GROUP_CAPACITY_EXCEEDED` close-out. The key space is partitioned into
ranges of `key_capacity` keys and one batch is emitted per range.

Records need **not** arrive in key order here, and arrival order is preserved
within each batch — the hash table needs no key ordering, and imposing one would
give back the staging freedom the tile exists to provide. The trade is a counting
pass over every key before the first batch, and row counts that may be uneven:
key ranges are equal in keys, not in rows.

Because each key lands in exactly one batch, combining the per-batch group records
is a plain concatenation. If your batches came from somewhere else, use
`dau_core.operators.merge_hash_grouped` for the general additive merge instead.

## Spread batches across several devices

```python
from dau_driver.fleet import run_scan_batches_fleet

results = run_scan_batches_fleet(
    devices,
    batches,
    per_key_state=False,
    input_regions=(0x0000_0000, 0x0800_0000),
    row_bytes=16,
    collect=collect_results,
)

for result in results:
    print(result.batch_index, result.device.name, result.value)
```

One independent pipeline per device pulls from one shared lazy iterable. Each
batch is taken exactly once by whichever pipeline demands it, so the fleet
self-balances by actual device throughput without any pre-partitioning. Results
come back in global batch order as `FleetScanResult` records, each retaining the
exact device object that produced it.

`input_regions` is reused as **device-local** addresses on every card, never
shared memory.

Bias the split with `weights` — one positive, finite value per device — to
pre-weight each shared pull toward the requested relative shares. Batches stay
lazy and are never queued ahead.

`per_key_state` must be `False`. A stateful composition cannot run on a fleet at
all: batches go to whichever card is free, so a key's rows split across cards that
never see each other's state. Run it on a single device with
`key_partitioned_scan_batches` instead.

A failure is raised as `FleetScanError` annotated with the batch index and the
device. Note that cancelling wakes pipelines blocked pulling a batch, but one
already inside its status wait runs to its own completion or timeout first — so
an early failure can surface only after its peers finish.

## Report progress

```python
run_scan_batches(device, batches, ..., progress=lambda completed: print(f"{completed} batches"))
```

Called with the running completion count after each batch. On a fleet it counts
across all devices.

## Handle a job that does not finish

Both runners raise `PipelineJobError` when a job's status neither reports DONE
before `timeout_seconds` nor reports an error flag:

```text
scan job 3 timed out (status=0x0)
```

A device-side error flag is raised the same way, with `LAST_ERROR` included:

```text
scan job 3 failed: status=0x3 last_error=2
```

Raise `timeout_seconds` for large batches; the default is 60 seconds.

If you are running concurrent pipelines against a pure-Python transport — a fake
in a test — set `poll_yield_seconds` to a small value. The real transports release
the GIL in their register read, so pipelines interleave without it and a sleep
would only add latency per poll; a pure-Python transport spinning would hold the
GIL and flatten self-balancing toward an even split regardless of device speed.

## Next

- [Allocate device storage](allocate-device-storage.md) — where the input regions
  and output windows come from.
- [Execution contracts reference](../reference/execution-contracts.md) — every
  runner's full signature.
- [Overlapped staging](../explanation/overlapped-staging.md) — why the ping-pong
  and the `per_key_state` guard are shaped this way.
