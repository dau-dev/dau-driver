from __future__ import annotations

import struct

import pyarrow as pa
import pytest
from dau_core.stream import InvalidStreamError, LogicalType, OperationCode, OperationDescriptor, RecordBatchLimits

from dau_driver.stream_codec import RecordBatchColumn, StreamDecoder, StreamEncoder


def _columns(rows: int = 4) -> list[RecordBatchColumn]:
    return [
        RecordBatchColumn(field_id=0, logical_type=LogicalType.INT32, payload=struct.pack(f"<{rows}i", *range(1, rows + 1))),
        RecordBatchColumn(field_id=1, logical_type=LogicalType.INT64, payload=struct.pack(f"<{rows}q", *[v * 1_000_000 for v in range(rows)])),
        RecordBatchColumn(field_id=2, logical_type=LogicalType.INT32, payload=struct.pack(f"<{rows}i", *[570] * rows)),
    ]


def test_record_batch_round_trip() -> None:
    columns = _columns()
    encoded = StreamEncoder.encode_record_batch(columns, row_count=4, batch_id=7)

    assert encoded[:4] == b"1UAD"
    assert len(encoded) % 64 == 0

    decoded = StreamDecoder.decode_record_batch(encoded)
    assert decoded.header.batch_id == 7
    assert decoded.header.column_count == 3
    assert decoded.header.row_count == 4
    for original, descriptor, payload in zip(columns, decoded.columns, decoded.payloads):
        assert descriptor.field_id == original.field_id
        assert descriptor.logical_type == original.logical_type
        assert payload == original.payload
    assert decoded.column_payload(1) == columns[1].payload


def test_payload_regions_are_64_byte_aligned_and_non_overlapping() -> None:
    decoded = StreamDecoder.decode_record_batch(StreamEncoder.encode_record_batch(_columns(), row_count=4))
    previous_end = 0
    for column in decoded.columns:
        assert column.payload_offset % 64 == 0
        assert column.payload_offset >= previous_end
        previous_end = column.payload_offset + column.payload_length


def test_operations_travel_with_the_batch() -> None:
    operation = OperationDescriptor(
        operation_id=0,
        opcode=OperationCode.SUM,
        input_column_id=1,
        output_field_id=0,
        output_type=LogicalType.INT64,
        accumulator_width_bits=64,
    )
    encoded = StreamEncoder.encode_record_batch(_columns(), row_count=4, operations=(operation,))
    decoded = StreamDecoder.decode_record_batch(encoded)
    assert decoded.operations == (operation,)


def test_arrow_round_trip() -> None:
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array([1, -2, 3], type=pa.int32()),
            pa.array([10_500_000, 9_900_000, 10_750_000], type=pa.int64()),
            pa.array([1.5, 2.5, -3.5], type=pa.float64()),
        ],
        names=["symbol_id", "price_micro", "weight"],
    )
    encoded = StreamEncoder.encode_arrow_batch(batch)
    restored = StreamDecoder.decode_record_batch_to_arrow(encoded, names=batch.schema.names)
    assert restored.equals(batch)


def test_arrow_batch_with_nulls_rejected() -> None:
    batch = pa.RecordBatch.from_arrays([pa.array([1, None, 3], type=pa.int32())], names=["v"])
    with pytest.raises(InvalidStreamError, match="null-free"):
        StreamEncoder.encode_arrow_batch(batch)


def test_arrow_batch_with_strings_rejected() -> None:
    batch = pa.RecordBatch.from_arrays([pa.array(["a", "b"], type=pa.string())], names=["sym"])
    with pytest.raises(InvalidStreamError, match="unsupported arrow type"):
        StreamEncoder.encode_arrow_batch(batch)


def test_column_payload_length_mismatch_rejected() -> None:
    bad = [RecordBatchColumn(field_id=0, logical_type=LogicalType.INT32, payload=b"\x00" * 10)]
    with pytest.raises(InvalidStreamError, match="does not match"):
        StreamEncoder.encode_record_batch(bad, row_count=4)


def test_limits_enforced_on_encode_and_decode() -> None:
    limits = RecordBatchLimits(max_columns=2)
    with pytest.raises(InvalidStreamError, match="exceeds limit 2"):
        StreamEncoder.encode_record_batch(_columns(), row_count=4, limits=limits)

    encoded = StreamEncoder.encode_record_batch(_columns(), row_count=4)
    with pytest.raises(InvalidStreamError, match="exceeds limit 2"):
        StreamDecoder.decode_record_batch(encoded, limits=limits)


def test_truncated_batch_rejected() -> None:
    encoded = StreamEncoder.encode_record_batch(_columns(), row_count=4)
    with pytest.raises(InvalidStreamError):
        StreamDecoder.decode_record_batch(encoded[: len(encoded) - 64])


def test_prepared_taq_shaped_batch_round_trips() -> None:
    # The OHLCV prepared-trades shape: symbol_id, bucket int32; price/size int64.
    rows = 8
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array([0] * rows, type=pa.int32()),
            pa.array([570 + (i // 4) for i in range(rows)], type=pa.int32()),
            pa.array([10_500_000 + 1_000 * i for i in range(rows)], type=pa.int64()),
            pa.array([100_000_000 + i for i in range(rows)], type=pa.int64()),
        ],
        names=["symbol_id", "bucket", "price_micro", "size_micro"],
    )
    encoded = StreamEncoder.encode_arrow_batch(batch)
    restored = StreamDecoder.decode_record_batch_to_arrow(encoded, names=batch.schema.names)
    assert restored.equals(batch)


def test_arrow_encode_handles_sliced_arrays() -> None:
    # sliced arrays carry a nonzero offset into their data buffer; the
    # zero-copy path must respect it
    base = pa.array(list(range(100)), type=pa.int32())
    sliced = base.slice(37, 8)
    batch = pa.RecordBatch.from_arrays([sliced], names=["v"])
    encoded = StreamEncoder.encode_arrow_batch(batch)
    restored = StreamDecoder.decode_record_batch_to_arrow(encoded, names=["v"])
    assert restored.column(0).to_pylist() == list(range(37, 45))
