from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass

from dau_core.stream import BatchHeader, ColumnDescriptor, InvalidStreamError, LogicalType, OperationDescriptor, StreamFlag

_ALIGNMENT_BYTES = 64
_SUPPORTED_WIDTHS_BYTES = {
    LogicalType.INT8: 1,
    LogicalType.INT16: 2,
    LogicalType.INT32: 4,
    LogicalType.INT64: 8,
    LogicalType.UINT8: 1,
    LogicalType.UINT16: 2,
    LogicalType.UINT32: 4,
    LogicalType.UINT64: 8,
    LogicalType.FLOAT32: 4,
    LogicalType.FLOAT64: 8,
}
_SCALAR_RESULT_STRUCTS = {
    LogicalType.INT8: struct.Struct("<b"),
    LogicalType.INT16: struct.Struct("<h"),
    LogicalType.INT32: struct.Struct("<i"),
    LogicalType.INT64: struct.Struct("<q"),
    LogicalType.UINT8: struct.Struct("<B"),
    LogicalType.UINT16: struct.Struct("<H"),
    LogicalType.UINT32: struct.Struct("<I"),
    LogicalType.UINT64: struct.Struct("<Q"),
    LogicalType.FLOAT32: struct.Struct("<f"),
    LogicalType.FLOAT64: struct.Struct("<d"),
}


@dataclass(frozen=True)
class DecodedStreamColumn:
    header: BatchHeader
    column: ColumnDescriptor
    payload: bytes
    operations: tuple[OperationDescriptor, ...] = ()


@dataclass(frozen=True)
class DecodedStreamResult:
    header: BatchHeader
    column: ColumnDescriptor
    payload: bytes
    value: int | float


class StreamEncoder:
    @staticmethod
    def encode_column(
        logical_type: LogicalType,
        payload: bytes,
        *,
        row_count: int,
        field_id: int = 0,
        schema_hash: int = 0,
        batch_id: int = 0,
        operations: Iterable[OperationDescriptor] = (),
    ) -> bytes:
        normalized_type = _normalize_supported_type(logical_type)
        operation_descriptors = tuple(_normalize_operation_descriptor(operation) for operation in operations)
        payload_bytes = bytes(payload)
        if row_count < 0:
            raise InvalidStreamError("row_count cannot be negative")
        width_bytes = _SUPPORTED_WIDTHS_BYTES[normalized_type]
        expected_payload_length = row_count * width_bytes
        if len(payload_bytes) != expected_payload_length:
            raise InvalidStreamError(
                f"payload length {len(payload_bytes)} does not match row_count {row_count} and {normalized_type.name.lower()} width {width_bytes}"
            )

        descriptor_table_offset = BatchHeader.SIZE_BYTES
        descriptor_table_length = ColumnDescriptor.SIZE_BYTES + len(operation_descriptors) * OperationDescriptor.SIZE_BYTES
        payload_offset = _align_up(descriptor_table_offset + descriptor_table_length, _ALIGNMENT_BYTES)
        column = ColumnDescriptor(
            field_id=field_id,
            logical_type=normalized_type,
            physical_width_bits=width_bytes * 8,
            row_count=row_count,
            null_count=0,
            payload_offset=payload_offset,
            payload_length=len(payload_bytes),
        )
        header = BatchHeader(
            schema_hash=schema_hash,
            batch_id=batch_id,
            row_count=row_count,
            column_count=1,
            operation_count=len(operation_descriptors),
            group_key_count=0,
            descriptor_table_offset=descriptor_table_offset,
            payload_offset=payload_offset,
            payload_length=len(payload_bytes),
            flags=StreamFlag.NONE,
        )
        encoded = header.pack() + column.pack()
        encoded += b"".join(operation.pack() for operation in operation_descriptors)
        encoded += b"\x00" * (payload_offset - len(encoded))
        encoded += payload_bytes
        encoded += b"\x00" * (_align_up(len(encoded), _ALIGNMENT_BYTES) - len(encoded))
        return encoded


class StreamDecoder:
    @staticmethod
    def decode_column(data: bytes) -> DecodedStreamColumn:
        encoded = bytes(data)
        if len(encoded) < BatchHeader.SIZE_BYTES:
            raise InvalidStreamError("stream data is shorter than a batch header")
        header = BatchHeader.unpack(encoded[: BatchHeader.SIZE_BYTES])
        _validate_header(header, len(encoded))

        descriptor_start = header.descriptor_table_offset
        descriptor_end = descriptor_start + ColumnDescriptor.SIZE_BYTES
        if descriptor_end > len(encoded):
            raise InvalidStreamError("descriptor table extends beyond stream data")
        column = _unpack_column(encoded[descriptor_start:descriptor_end])
        _validate_column(header, column, len(encoded))

        operations_start = descriptor_end
        operations_end = operations_start + header.operation_count * OperationDescriptor.SIZE_BYTES
        if operations_end > len(encoded) or operations_end > header.payload_offset:
            raise InvalidStreamError("operation descriptor table extends beyond stream data")
        operations = tuple(
            _unpack_operation(encoded[offset : offset + OperationDescriptor.SIZE_BYTES])
            for offset in range(operations_start, operations_end, OperationDescriptor.SIZE_BYTES)
        )
        _validate_operations(header, operations)

        payload_end = column.payload_offset + column.payload_length
        return DecodedStreamColumn(header=header, column=column, payload=encoded[column.payload_offset : payload_end], operations=operations)

    @staticmethod
    def decode_scalar_result(data: bytes) -> DecodedStreamResult:
        decoded = StreamDecoder.decode_column(data)
        if decoded.header.row_count != 1 or decoded.column.row_count != 1:
            raise InvalidStreamError("scalar result decode expects exactly one row")
        result_type = _normalize_supported_type(decoded.column.logical_type)
        try:
            scalar_struct = _SCALAR_RESULT_STRUCTS[result_type]
        except KeyError as exc:
            raise InvalidStreamError(f"unsupported scalar result type {result_type.name.lower()}") from exc
        if len(decoded.payload) != scalar_struct.size:
            raise InvalidStreamError("scalar result payload length does not match result type")
        value = scalar_struct.unpack(decoded.payload)[0]
        return DecodedStreamResult(header=decoded.header, column=decoded.column, payload=decoded.payload, value=value)


def _validate_header(header: BatchHeader, encoded_length: int) -> None:
    if (header.version_major, header.version_minor) != (0, 1):
        raise InvalidStreamError(f"unsupported stream version {header.version_major}.{header.version_minor}")
    if header.header_length != BatchHeader.SIZE_BYTES:
        raise InvalidStreamError(f"unsupported batch header length {header.header_length}")
    if header.column_count != 1:
        raise InvalidStreamError(f"expected one column descriptor, found {header.column_count}")
    if header.descriptor_table_offset % _ALIGNMENT_BYTES != 0 or header.payload_offset % _ALIGNMENT_BYTES != 0:
        raise InvalidStreamError("descriptor and payload offsets must be 64-byte aligned")
    _validate_payload_window(header.payload_offset, header.payload_length, encoded_length)


def _validate_column(header: BatchHeader, column: ColumnDescriptor, encoded_length: int) -> None:
    _normalize_supported_type(column.logical_type)
    expected_width_bits = _SUPPORTED_WIDTHS_BYTES[column.logical_type] * 8
    if column.physical_width_bits != expected_width_bits:
        raise InvalidStreamError(f"physical_width_bits must be {expected_width_bits} for {column.logical_type.name.lower()}")
    if column.null_count != 0 or column.validity_length != 0:
        raise InvalidStreamError("single-column decode does not support validity bitmaps yet")
    if column.row_count != header.row_count:
        raise InvalidStreamError("column row_count does not match batch header")
    if column.payload_offset != header.payload_offset or column.payload_length != header.payload_length:
        raise InvalidStreamError("column payload window does not match batch header")
    expected_payload_length = column.row_count * _SUPPORTED_WIDTHS_BYTES[column.logical_type]
    if column.payload_length != expected_payload_length:
        raise InvalidStreamError("column payload length does not match row_count")
    _validate_payload_window(column.payload_offset, column.payload_length, encoded_length)


def _validate_operations(header: BatchHeader, operations: tuple[OperationDescriptor, ...]) -> None:
    if len(operations) != header.operation_count:
        raise InvalidStreamError("operation descriptor count does not match batch header")
    for operation in operations:
        if operation.input_column_id != 0:
            raise InvalidStreamError("single-column streams require operation input_column_id 0")
        _normalize_supported_type(operation.output_type)
        if operation.accumulator_width_bits not in (32, 64):
            raise InvalidStreamError("operation accumulator_width_bits must be 32 or 64")


def _validate_payload_window(payload_offset: int, payload_length: int, encoded_length: int) -> None:
    if payload_offset < 0 or payload_length < 0 or payload_offset + payload_length > encoded_length:
        raise InvalidStreamError("payload window extends beyond stream data")


def _normalize_supported_type(logical_type: LogicalType) -> LogicalType:
    try:
        normalized_type = LogicalType(logical_type)
    except ValueError as exc:
        raise InvalidStreamError(f"unsupported logical type {logical_type!r}") from exc
    if normalized_type not in _SUPPORTED_WIDTHS_BYTES:
        raise InvalidStreamError(f"unsupported logical type {normalized_type.name.lower()}")
    return normalized_type


def _normalize_operation_descriptor(operation: OperationDescriptor) -> OperationDescriptor:
    if not isinstance(operation, OperationDescriptor):
        raise InvalidStreamError(f"expected OperationDescriptor, got {type(operation).__name__}")
    _validate_operations(
        BatchHeader(
            schema_hash=0,
            batch_id=0,
            row_count=0,
            column_count=1,
            operation_count=1,
            group_key_count=0,
            descriptor_table_offset=BatchHeader.SIZE_BYTES,
            payload_offset=BatchHeader.SIZE_BYTES,
            payload_length=0,
            flags=StreamFlag.NONE,
        ),
        (operation,),
    )
    return operation


def _unpack_column(data: bytes) -> ColumnDescriptor:
    try:
        return ColumnDescriptor.unpack(data)
    except ValueError as exc:
        raise InvalidStreamError(str(exc)) from exc


def _unpack_operation(data: bytes) -> OperationDescriptor:
    try:
        return OperationDescriptor.unpack(data)
    except ValueError as exc:
        raise InvalidStreamError(str(exc)) from exc


def _align_up(value: int, alignment: int) -> int:
    remainder = value % alignment
    if remainder == 0:
        return value
    return value + alignment - remainder
