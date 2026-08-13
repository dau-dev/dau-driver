# Stream codec reference

`dau_driver.stream_codec` encodes typed columns as DAU stream batches and decodes
them back. The wire format — `BatchHeader`, `ColumnDescriptor`,
`OperationDescriptor`, `LogicalType`, `StreamFlag`, `RecordBatchLimits`,
`InvalidStreamError` — is defined in `dau_core.stream`; this module marshals it.

Batches are 64-byte aligned throughout: the descriptor table starts at
`BatchHeader.SIZE_BYTES`, each payload begins on a 64-byte boundary, and the
encoded batch is padded up to one.

Supported logical types: `INT8`, `INT16`, `INT32`, `INT64`, `UINT8`, `UINT16`,
`UINT32`, `UINT64`, `FLOAT32`, `FLOAT64`. Any other value raises
`InvalidStreamError`.

Validity bitmaps are not supported. Columns must be fixed-width and null-free.

## `StreamEncoder`

All methods are static.

### `encode_column`

```python
StreamEncoder.encode_column(
    logical_type: LogicalType,
    payload: bytes,
    *,
    row_count: int,
    field_id: int = 0,
    schema_hash: int = 0,
    batch_id: int = 0,
    operations: Iterable[OperationDescriptor] = (),
) -> bytes
```

Encode one column as a single-column batch. Raises `InvalidStreamError` when
`row_count` is negative or `len(payload)` is not `row_count * width`.

### `encode_record_batch`

```python
StreamEncoder.encode_record_batch(
    columns: Sequence[RecordBatchColumn],
    *,
    row_count: int,
    schema_hash: int = 0,
    batch_id: int = 0,
    operations: Iterable[OperationDescriptor] = (),
    limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS,
) -> bytes
```

Encode N fixed-width, null-free columns as one multi-column batch. Raises
`InvalidStreamError` when `row_count` is negative, `columns` is empty, or any
column's payload length does not match its row count and width. The assembled
header and descriptors are checked against `limits` before serialization.

### `encode_arrow_batch`

```python
StreamEncoder.encode_arrow_batch(
    batch,
    *,
    schema_hash: int = 0,
    batch_id: int = 0,
    operations: Iterable[OperationDescriptor] = (),
    limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS,
) -> bytes
```

Encode a PyArrow `RecordBatch` or `Table` of fixed-width, null-free columns.
Field ids are assigned by column position. Column data is taken as a zero-copy,
offset-aware view of the primitive data buffer, so a sliced array encodes without
a materializing copy; one copy happens at stream assembly. A column with nulls
raises `InvalidStreamError` naming the column. Requires `pyarrow`.

## `StreamDecoder`

All methods are static.

| Method                                                                                  | Returns                                                                      |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `decode_column(data)`                                                                   | `DecodedStreamColumn` — a single-column batch. Requires `column_count == 1`. |
| `decode_record_batch(data, *, limits=DEFAULT_RECORD_BATCH_LIMITS)`                      | `DecodedRecordBatch` — the N-column counterpart.                             |
| `decode_record_batch_to_arrow(data, *, names=None, limits=DEFAULT_RECORD_BATCH_LIMITS)` | A PyArrow `RecordBatch`. Column names default to `f{field_id}`.              |
| `decode_scalar_result(data)`                                                            | `DecodedStreamResult` — a one-row batch with its `value` unpacked.           |

Decoding validates the stream version (0.1), the header length, 64-byte
alignment of the descriptor and payload offsets, every payload window against the
data length, each column's logical type, physical width, row count, and payload
length, and each operation descriptor's input column id, output type, and
accumulator width (32 or 64). Any mismatch raises `InvalidStreamError`.

`decode_scalar_result` additionally requires exactly one row in both the header
and the column, and a payload length matching the result type's width.

## Data models

### `RecordBatchColumn`

One fixed-width, null-free input column for record-batch encoding.

| Field          | Type          |
| -------------- | ------------- |
| `field_id`     | `int`         |
| `logical_type` | `LogicalType` |
| `payload`      | `bytes`       |

### `DecodedStreamColumn`

| Field        | Type                              | Default |
| ------------ | --------------------------------- | ------- |
| `header`     | `BatchHeader`                     | —       |
| `column`     | `ColumnDescriptor`                | —       |
| `payload`    | `bytes`                           | —       |
| `operations` | `tuple[OperationDescriptor, ...]` | `()`    |

### `DecodedRecordBatch`

| Field        | Type                              | Default |
| ------------ | --------------------------------- | ------- |
| `header`     | `BatchHeader`                     | —       |
| `columns`    | `tuple[ColumnDescriptor, ...]`    | —       |
| `payloads`   | `tuple[bytes, ...]`               | —       |
| `operations` | `tuple[OperationDescriptor, ...]` | `()`    |

`column_payload(field_id)` returns the payload for one column by field id, not by
position. Raises `InvalidStreamError` when no column carries that id.

### `DecodedStreamResult`

| Field     | Type               |
| --------- | ------------------ |
| `header`  | `BatchHeader`      |
| `column`  | `ColumnDescriptor` |
| `payload` | `bytes`            |
| `value`   | `int \| float`     |
