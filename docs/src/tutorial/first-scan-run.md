# Run a scan over many batches

In this tutorial we will run a multi-batch scan job: build batches, hand them to
the streaming runner, and watch it ping-pong between two device regions so each
batch's DMA overlaps the previous batch's compute. Then we will meet the guard
that stops a stateful composition from being fed the wrong kind of batch.

As in [Talk to a device without any hardware](first-device.md), everything runs
against the fake transport, with no board required. Work in a Python session with
`dau-driver` installed.

## Step 1: Build some batches

A `ScanBatch` is a packed row payload and its row count. Here each row is four
int32 fields: a symbol id and a price, with two fields unused. Run:

```python
import struct
from dau_driver.scan_execution import ScanBatch

ROW_BYTES = 16
SYMBOL_IDS = {"AAA": 0, "BBB": 1, "CCC": 2}

readings = [
    ("AAA", 10), ("AAA", 11), ("AAA", 12),
    ("BBB", 20),
    ("CCC", 30), ("CCC", 31),
]


def encode(rows):
    return b"".join(struct.pack("<4i", SYMBOL_IDS[symbol], price, 0, 0) for symbol, price in rows)


batches = [
    ScanBatch(payload=encode(readings[:3]), row_count=3),
    ScanBatch(payload=encode(readings[3:]), row_count=3),
]

for batch in batches:
    print(batch.row_count, len(batch.payload))
```

**Output:**

```text
3 48
3 48
```

Three rows at 16 bytes each is 48 bytes. Note that 48 is not a multiple of the
64-byte DMA grid; Step 2 comes back to that.

## Step 2: Run them

The runner needs two distinct device addresses to alternate between, the row
width, and a `collect` callback that reads each job's results off the device once
it completes. Run:

```python
from pathlib import Path

from dau_core.registers import JobStatusFlag, RegisterOffset
from dau_driver.scan_execution import run_scan_batches
from dau_driver.testing import FakeTransport, fake_device

transport = FakeTransport()
transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
device = fake_device(Path("/tmp"), transport)


def collect(device):
    return device.read_register(RegisterOffset.INPUT_LENGTH_LOW)


results = run_scan_batches(
    device,
    batches,
    per_key_state=False,
    input_regions=(0x0000_0000, 0x0800_0000),
    row_bytes=ROW_BYTES,
    collect=collect,
    timeout_seconds=5.0,
)

print("results:", results)
print("regions:", [hex(address) for address, _ in transport.h2c_writes])
print("bytes:  ", [len(payload) for _, payload in transport.h2c_writes])
```

**Output:**

```text
results: [48, 48]
regions: ['0x0', '0x8000000']
bytes:   [64, 64]
```

Three things happened here.

The two batches landed in *different* regions, `0x0` then `0x8000000`. That is
the ping-pong: while job 0 computes out of region A, batch 1's DMA is already
filling region B. Batch 2 would go back to region A, and so on.

The payloads went out as 64 bytes, not 48. The runner pads the DMA up to the
64-byte grid, while `INPUT_LENGTH`, which is what the device reads, stays exact
at 48. That is why `collect` returned 48 for each batch.

`collect` was called once per batch, after that batch's job reported DONE, and the
results came back in batch order.

Because `batches` here is a list, all the encoding happened before the run. Pass a
generator instead and the runner will pull each batch just before it stages it,
so the encoding work overlaps compute too.

## Step 3: Confine each key to one batch

Some scan compositions keep per-key state across a batch, a grouped aggregation
for instance. Slicing rows by position would then split a key's state across two
jobs. `key_partitioned_scan_batches` builds batches where each key lands in
exactly one. Run:

```python
from dau_driver.scan_execution import key_partitioned_scan_batches

transport = FakeTransport()
transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
device = fake_device(Path("/tmp"), transport)

partitioned = key_partitioned_scan_batches(
    readings,
    batch_count=2,
    key=lambda reading: reading[0],
    encode=encode,
)

results = run_scan_batches(
    device,
    partitioned,
    per_key_state=True,
    input_regions=(0x0000_0000, 0x0800_0000),
    row_bytes=ROW_BYTES,
    collect=collect,
    timeout_seconds=5.0,
)
print("results:", results)
```

**Output:**

```text
results: [48, 48]
```

Six rows across three key groups, balanced into two batches of three. The
assignment is longest-processing-time-first: the largest key group goes into the
currently smallest batch. Records must arrive ordered by key, and record order
within each key is preserved.

## Step 4: Meet the guard

`per_key_state` is not advisory. Try running the plain list of batches from Step 1
with it set:

```python
try:
    run_scan_batches(
        device,
        batches,
        per_key_state=True,
        input_regions=(0x0000_0000, 0x0800_0000),
        row_bytes=ROW_BYTES,
        collect=collect,
    )
except ValueError as exc:
    print("ValueError:", exc)
```

**Output:**

```text
ValueError: scan composition has cross-batch per-key state; batches must be key-partitioned with key_partitioned_scan_batches
```

A stateful composition only accepts batches that came from
`key_partitioned_scan_batches`. The check is on the batch iterable's identity, not
on a flag you could get wrong, so a position-sliced batch cannot reach a stateful
job by accident.

## Step 5: Bound the distinct keys per batch

A different constraint applies to the hash-mode grouped tile: its table holds a
fixed number of *distinct keys*, and a batch presenting more is invalidated by the
device. Rather than react to that, size the batches so it cannot happen. Run:

```python
from dau_driver.scan_execution import key_range_scan_batches

for batch in key_range_scan_batches(readings, key_capacity=2, key=lambda reading: reading[0], encode=encode):
    print(batch.row_count)
```

**Output:**

```text
4
2
```

Two batches: the first holds keys `AAA` and `BBB` (four rows), the second holds
`CCC` (two). The key ranges are equal in *keys*, not in rows, which is why the row
counts differ.

Because each key lands in exactly one batch, combining the per-batch group records
is a plain concatenation. And unlike Step 3, records need not arrive in key
order: the hash table needs no key ordering, so arrival order is preserved within
each batch.

## What you have done

You have run a multi-batch scan with overlapped staging, built key-confined
batches two different ways, and seen the guard that keeps a stateful composition
from reading half its state.

From here:

- For choosing between these batching strategies on real work, and for spreading
  batches across several cards, see
  [Run scan jobs](../how-to/run-scan-jobs.md).
- For the full signature of every runner, see the
  [execution contracts reference](../reference/execution-contracts.md).
- For why the overlap is shaped this way, read
  [Overlapped staging](../explanation/overlapped-staging.md).
