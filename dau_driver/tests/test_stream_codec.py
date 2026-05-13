from __future__ import annotations

import struct

import pytest
from dau_core.stream import BatchHeader, ColumnDescriptor, InvalidStreamError, LogicalType, OperationCode, OperationDescriptor, StreamFlag

from dau_driver.stream_codec import DecodedStreamResult, StreamDecoder, StreamEncoder


def test_encode_int32_column_round_trips_batch_descriptor_and_payload() -> None:
    payload = struct.pack("<4i", -3, -1, 1, 3)

    encoded = StreamEncoder.encode_column(LogicalType.INT32, payload, row_count=4, field_id=7, schema_hash=0x1234, batch_id=99)
    decoded = StreamDecoder.decode_column(encoded)

    assert decoded.header == BatchHeader(
        schema_hash=0x1234,
        batch_id=99,
        row_count=4,
        column_count=1,
        operation_count=0,
        group_key_count=0,
        descriptor_table_offset=BatchHeader.SIZE_BYTES,
        payload_offset=BatchHeader.SIZE_BYTES + ColumnDescriptor.SIZE_BYTES,
        payload_length=len(payload),
        flags=StreamFlag.NONE,
    )
    assert decoded.column == ColumnDescriptor(
        field_id=7,
        logical_type=LogicalType.INT32,
        physical_width_bits=32,
        row_count=4,
        null_count=0,
        payload_offset=BatchHeader.SIZE_BYTES + ColumnDescriptor.SIZE_BYTES,
        payload_length=len(payload),
    )
    assert decoded.payload == payload
    assert len(encoded) % 64 == 0


def test_encode_rejects_payload_length_that_does_not_match_row_count() -> None:
    with pytest.raises(InvalidStreamError, match="payload length"):
        StreamEncoder.encode_column(LogicalType.INT32, b"\x00\x00", row_count=1)


def test_encode_rejects_unsupported_logical_type() -> None:
    with pytest.raises(InvalidStreamError, match="unsupported logical type"):
        StreamEncoder.encode_column(LogicalType.BOOL, b"\x01", row_count=1)


@pytest.mark.parametrize(
    ("logical_type", "payload"),
    (
        (LogicalType.INT8, struct.pack("<2b", -1, 2)),
        (LogicalType.INT16, struct.pack("<2h", -1, 2)),
        (LogicalType.INT64, struct.pack("<2q", -1, 2)),
        (LogicalType.UINT8, struct.pack("<2B", 1, 2)),
        (LogicalType.UINT16, struct.pack("<2H", 1, 2)),
        (LogicalType.UINT32, struct.pack("<2I", 1, 2)),
        (LogicalType.UINT64, struct.pack("<2Q", 1, 2)),
        (LogicalType.FLOAT32, struct.pack("<2f", 1.0, 2.0)),
        (LogicalType.FLOAT64, struct.pack("<2d", 1.0, 2.0)),
    ),
)
def test_encode_column_round_trips_supported_fixed_width_types(logical_type: LogicalType, payload: bytes) -> None:
    decoded = StreamDecoder.decode_column(StreamEncoder.encode_column(logical_type, payload, row_count=2))

    assert decoded.column.logical_type == logical_type
    assert decoded.column.payload_length == len(payload)
    assert decoded.payload == payload


def test_decode_rejects_malformed_payload_window() -> None:
    encoded = bytearray(StreamEncoder.encode_column(LogicalType.INT32, struct.pack("<i", 5), row_count=1))
    header = BatchHeader.unpack(bytes(encoded[: BatchHeader.SIZE_BYTES]))
    corrupt_header = BatchHeader(
        schema_hash=header.schema_hash,
        batch_id=header.batch_id,
        row_count=header.row_count,
        column_count=header.column_count,
        operation_count=header.operation_count,
        group_key_count=header.group_key_count,
        descriptor_table_offset=header.descriptor_table_offset,
        payload_offset=header.payload_offset,
        payload_length=len(encoded),
        flags=header.flags,
    )
    encoded[: BatchHeader.SIZE_BYTES] = corrupt_header.pack()

    with pytest.raises(InvalidStreamError, match="payload window"):
        StreamDecoder.decode_column(bytes(encoded))


def test_encode_column_round_trips_operation_descriptor() -> None:
    payload = struct.pack("<3i", 3, 4, 5)
    operation = OperationDescriptor(
        operation_id=2,
        opcode=OperationCode.SUM,
        input_column_id=0,
        output_field_id=9,
        output_type=LogicalType.INT64,
        accumulator_width_bits=64,
    )

    decoded = StreamDecoder.decode_column(StreamEncoder.encode_column(LogicalType.INT32, payload, row_count=3, operations=(operation,)))

    assert decoded.header.operation_count == 1
    assert decoded.operations == (operation,)
    assert decoded.column.payload_offset == BatchHeader.SIZE_BYTES + ColumnDescriptor.SIZE_BYTES + OperationDescriptor.SIZE_BYTES
    assert decoded.payload == payload


def test_decode_scalar_result_interprets_single_row_payload() -> None:
    encoded = StreamEncoder.encode_column(LogicalType.INT64, struct.pack("<q", -17), row_count=1, field_id=9)

    result = StreamDecoder.decode_scalar_result(encoded)

    assert result == DecodedStreamResult(
        header=BatchHeader.unpack(encoded[: BatchHeader.SIZE_BYTES]),
        column=StreamDecoder.decode_column(encoded).column,
        payload=struct.pack("<q", -17),
        value=-17,
    )
