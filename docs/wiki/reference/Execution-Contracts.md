# Execution contracts reference

`dau_driver.stream_execution`, `dau_driver.scan_execution`,
`dau_driver.pipeline_execution`, and `dau_driver.fleet`. Each module implements
one shell's register contract; register offsets and flags come from
`dau_core.registers`.

Every runner polls `JOB_STATUS` through one shared wait, so timeout and error
semantics are identical across shells: `PipelineJobError` on timeout, and
`PipelineJobError` including `LAST_ERROR` when the status carries the error flag.

## Single stream job

### `execute_scalar_operation`

```python
execute_scalar_operation(
    device: DauDevice,
    logical_type: LogicalType,
    payload: bytes,
    *,
    row_count: int,
    operation: OperationDescriptor,
    input_device_address: int,
    output_device_address: int,
    output_byte_count: int,
    trigger: StreamTrigger | None = None,
    timeout_seconds: float = 5.0,
) -> DecodedStreamResult
```

Validate the operation against the device's capabilities, encode the payload as a
single-column stream batch, write it H2C, invoke `trigger`, read the result C2H,
and decode it as a scalar.

Raises `StreamExecutionError` before any DMA when the device advertises no
operator for `(opcode, input type)`, or when the advertised output type differs
from the descriptor's.

`StreamTrigger` is `Callable[[DauDevice, bytes], None]`, called with the device
and the encoded input.

## Scan jobs

### `ScanBatch`

| Field       | Type    | Description                                              |
| ----------- | ------- | -------------------------------------------------------- |
| `payload`   | `bytes` | The packed row payload, exactly `row_count * row_bytes`. |
| `row_count` | `int`   | Rows in the batch.                                       |

### `run_scan_batches`

```python
run_scan_batches(
    device,
    batches: Iterable[ScanBatch],
    *,
    per_key_state: bool,
    input_regions: tuple[int, int],
    row_bytes: int,
    collect: Callable[[Any], Any],
    timeout_seconds: float = 60.0,
    progress=None,
    poll_yield_seconds: float | None = None,
) -> list[Any]
```

Run one job per batch with ping-pong input staging: batch *i + 1* DMAs into the
alternate region — and, for a lazy iterable, is produced — while job *i* computes.
Returns the `collect(device)` values in batch order.

Per batch the runner writes `INPUT_ADDRESS_LOW`/`HIGH` and `INPUT_LENGTH_LOW`,
sets `JOB_CONTROL` to `START`, waits for DONE, then calls `collect`. The DMA is
padded to the 64-byte grid while `INPUT_LENGTH` stays exact.

| Parameter            | Notes                                                                                                          |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| `per_key_state`      | Required. When True, `batches` must be the result of `key_partitioned_scan_batches` or `ValueError` is raised. |
| `input_regions`      | Two distinct device addresses. Equal addresses raise `ValueError`.                                             |
| `row_bytes`          | Must be positive.                                                                                              |
| `collect`            | Called once per batch after DONE, with the device.                                                             |
| `progress`           | Called with the running completion count after each batch.                                                     |
| `poll_yield_seconds` | Sleep per status poll. `None` spins.                                                                           |

Raises `ValueError` for a zero-row batch and for a payload whose length is not
`row_count * row_bytes`.

The caller owns everything shell-specific: lane output addresses and static
configuration are written before the run, and `collect` reads the results. Output
regions are disjoint from input staging, so collection overlaps the next DMA.

### `key_partitioned_scan_batches`

```python
key_partitioned_scan_batches(
    records: Iterable[Record],
    *,
    batch_count: int,
    key: Callable[[Record], Hashable],
    encode: Callable[[Iterable[Record]], bytes],
) -> Iterator[ScanBatch]
```

Build key-confined batches by longest-processing-time-first greedy assignment:
the largest key group goes into the currently smallest batch. Records must be
ordered by key — a key appearing in more than one run raises `ValueError`. Record
order within each key is preserved.

Creating the iterable consumes nothing. First iteration consumes one complete
grouping pass, because LPT needs every group size before assignment; each batch's
payload is built only when that batch is requested. Key groups are indivisible,
so one hot key can dominate a batch. `batch_count` must be positive.

The number of batches produced is `min(batch_count, number of key groups)`.

### `key_range_scan_batches`

```python
key_range_scan_batches(
    records: Iterable[Record],
    *,
    key_capacity: int,
    key: Callable[[Record], Hashable],
    encode: Callable[[Iterable[Record]], bytes],
) -> Iterator[ScanBatch]
```

Split records into batches each holding at most `key_capacity` distinct keys — the
host half of the hash-mode grouped tile's overflow contract. The sorted key space
is partitioned into ranges of `key_capacity` keys and one batch is emitted per
range.

Records need not arrive in key order, and arrival order is preserved within each
batch. Each key lands in exactly one batch, so combining the per-batch group
records is a concatenation; use `dau_core.operators.merge_hash_grouped` for the
general additive merge when batches were produced some other way.

Costs a counting pass over every key before the first batch. Row counts may be
uneven: key ranges are equal in keys, not rows. `key_capacity` must be positive.

## Fleet

### `run_scan_batches_fleet`

```python
run_scan_batches_fleet(
    devices,
    batches: Iterable[ScanBatch],
    *,
    per_key_state: bool,
    input_regions: tuple[int, int],
    row_bytes: int,
    collect: Callable[[Any], Any],
    timeout_seconds: float = 60.0,
    progress=None,
    weights: Sequence[float] | None = None,
    poll_yield_seconds: float | None = None,
) -> list[FleetScanResult]
```

Run one independent `run_scan_batches` pipeline per device over one shared lazy
input. Each batch is pulled exactly once by whichever pipeline demands it, so with
`weights=None` scheduling is purely demand-driven and self-balances by device
throughput. Results are returned in global batch order.

`input_regions` is reused as device-local addresses on every card, never shared
memory.

`per_key_state` must be `False`; True raises `ValueError`. A stateful composition
has no fleet form: a key's rows split across cards that never see each other's
state.

`weights` takes one positive, finite value per device and pre-biases each shared
pull by weighted round-robin; batches remain lazy and are never partitioned or
queued ahead. A length mismatch or a non-positive or non-finite weight raises
`ValueError`.

An empty batch iterable returns `[]`. An empty device sequence raises
`ValueError`. A single device runs the same pipeline and adds ordered provenance.

Known limitation: cancelling on a device failure wakes pipelines blocked pulling a
batch, but one already inside its status wait runs to its own completion or
timeout first, so an early failure can surface only after its peers finish.

### `FleetScanResult`

| Field         | Type  | Description                                      |
| ------------- | ----- | ------------------------------------------------ |
| `batch_index` | `int` | Global batch index.                              |
| `device`      | `Any` | The exact device object that produced the value. |
| `value`       | `Any` | The `collect` return value.                      |

### `FleetScanError`

`RuntimeError` annotated with `batch_index` (`None` when the failure preceded any
batch) and `device`.

## Keyed pipeline and bar jobs

The `dau_mm_ohlcv_job` and bar shells run keyed `{value, key}` INT32 pair rows
through filter, map, and ordered grouped aggregation, staging fixed-width records
in the output region.

### `KeyedPipelineConfig`

A host-side mirror of the `PIPELINE_*` configuration registers, as a dataclass.

| Field           | Type                | Default |
| --------------- | ------------------- | ------- |
| `predicate`     | `FilterPredicate`   | —       |
| `filter_on_key` | `bool`              | `False` |
| `map_config`    | `MapConfig \| None` | `None`  |
| `map_on_key`    | `bool`              | `False` |

`filter_op_word()`, `map_op_word()`, and `map_constant_word()` produce the packed
register values. A `None` `map_config` packs as ADD 0 on the value lane —
identity.

`configure_pipeline(device, config)` writes all five `PIPELINE_*` registers.

### Keyed pipeline execution

| Function                                                                                         | Behaviour                                                                                    |
| ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `pack_keyed_rows(rows)`                                                                          | `(key, value)` pairs packed onto the 64-byte grid; the logical length stays `len(rows) * 8`. |
| `decode_group_records(data, count)`                                                              | 40-byte group records as `GroupAggregate` values.                                            |
| `execute_keyed_pipeline_job(device, rows, *, timeout_seconds=5.0, contract=None, max_rows=None)` | One batch through a configured pipeline. Returns `(group records, selected row count)`.      |

Constants: `GROUP_RECORD_BYTES = 40`, `DMA_ALIGN_BYTES = 64`. These are **wire
and record shapes** — fixed by the format both sides compile against, not by
the board.

Capacity is not a constant. `device_job_capacity(device)` reads the input row
and output record capacities the shell advertises; exceeding them raises
`PipelineJobError`. A module constant naming a capacity would be a second
source of truth for something the platform already declares, and
`dau_driver/tests/test_no_board_geometry_constants.py` asserts the allowed
module-level constants exactly, in both directions.

### Bar jobs

The bar wire layout is declared once, in `BAR_ROW_FIELDS` and
`BAR_RECORD_FIELDS`, with `struct_format(fields)` and `numpy_dtype_fields(fields)`
deriving the two consumers' views so a padding, signedness, or field-order change
cannot make them read one bitstream differently.

| Function                                                                                    | Behaviour                                                                                                                     |
| ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `pack_bar_rows(rows)`                                                                       | `(key, price, shares, frac)` rows on the DMA grid.                                                                            |
| `decode_bar_records(data, count)`                                                           | 48-byte records as `BarAggregate` values.                                                                                     |
| `execute_bar_job(device, rows, *, timeout_seconds=5.0, contract=None, max_rows=None)`       | One batch through the SRAM-staged fused bar shell.                                                                            |
| `stage_bar_ddr_input(device, payload, row_count, *, staging, input_address=None)`           | DMA one pre-packed batch into a memory input region.                                                                          |
| `start_bar_ddr_job(device, row_count, *, staging, input_address=None, output_address=None)` | Program the addresses and length, then start.                                                                                 |
| `finish_bar_ddr_job(device, *, staging, output_address=None, timeout_seconds=60.0)`         | Wait and read back. Returns `(raw record bytes, count)`; decoding stays with the caller so hosts can use vectorized decoders. |
| `execute_bar_ddr_packed(device, payload, row_count, *, staging, timeout_seconds=60.0)`      | Stage, start, finish.                                                                                                         |
| `bar_ddr_max_rows_per_job(staging)`                                                         | The row budget the given staging layout supports.                                                                             |

Constants: `BAR_ROW_BYTES = 16`, `BAR_RECORD_BYTES = 48`. Both are wire shapes.

`staging` is the board's `DdrStaging` — its input regions, output region, sizes,
and alignment — passed in from the composed platform. It is a keyword argument
rather than a module constant because addresses and region sizes are board
geometry, and a package-level default is how one board's memory map ends up
applied to another.

Rows per job are bounded by the worst case of one record per row, so an output
overrun is impossible by construction. Staging addresses must meet the layout's
declared alignment or `PipelineJobError` is raised.

> `execute_bar_ddr_job(device, rows, *, timeout_seconds=60.0)`, the tuple-row
> convenience wrapper, does not forward `staging` to `execute_bar_ddr_packed`
> and currently raises `TypeError`. Call `execute_bar_ddr_packed` with
> `pack_bar_rows(rows)` and an explicit `staging` instead.

### Broadcast-NoC bar shell

One scan computes every lane's partition concurrently, each lane writing its own
slice of the output region.

| Name                                                                                                                                                          | Behaviour                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `device_lane_count(device)`                                                                                                                                   | The lane count the shell advertises.                                                               |
| `bar_noc_lane_output_bytes(staging, lanes)`, `bar_noc_max_rows_per_job(staging, lanes)`                                                                       | The per-lane output slice and row budget for a staging layout.                                     |
| `bar_noc_lane_output_address(staging, lane, lanes)`                                                                                                           | One lane's output base.                                                                            |
| `configure_bar_noc_lanes(device, *, staging, lanes=None)`                                                                                                     | Point every lane's writer at its slice.                                                            |
| `execute_bar_noc_packed(device, payload, row_count, *, staging, lanes=None, record_bytes=BAR_RECORD_BYTES, validate_record_count=True, timeout_seconds=60.0)` | One pre-packed batch through a configured NoC shell. Returns `(raw record bytes, count)` per lane. |

A `lanes` of `None` asks the device how many it has, rather than assuming. A
host that assumed four lanes on a shell advertising its own count is the
reason the constant is gone. `record_bytes` defaults to the fused bar shell's
record; key-partitioned scan shells reuse the runner with their own width. Pass
`validate_record_count=False` for a lane whose count register latches an *input*
row count and emits one fixed-width record — the `result_length == count * record_bytes` invariant does not hold there, and `RESULT_LENGTH` is trusted alone.
