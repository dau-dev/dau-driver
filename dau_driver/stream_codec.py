from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from dau_core.stream import (
    DEFAULT_RECORD_BATCH_LIMITS,
    LOGICAL_TYPE_BYTE_WIDTHS,
    BatchHeader,
    ColumnDescriptor,
    InvalidStreamError,
    LogicalType,
    OperationDescriptor,
    RecordBatchLimits,
    StreamFlag,
    validate_record_batch,
)

_ALIGNMENT_BYTES = 64
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
_STRUCT_CODES = {
    LogicalType.INT8: "b",
    LogicalType.INT16: "h",
    LogicalType.INT32: "i",
    LogicalType.INT64: "q",
    LogicalType.UINT8: "B",
    LogicalType.UINT16: "H",
    LogicalType.UINT32: "I",
    LogicalType.UINT64: "Q",
    LogicalType.FLOAT32: "f",
    LogicalType.FLOAT64: "d",
}


def _arrow_to_logical_type(arrow_type) -> LogicalType:
    import pyarrow as pa

    for logical_type, factory in _ARROW_TYPE_FACTORIES.items():
        if arrow_type.equals(factory(pa)):
            return logical_type
    raise InvalidStreamError(f"unsupported arrow type {arrow_type} for record batches")


def _logical_to_arrow_type(logical_type: LogicalType):
    import pyarrow as pa

    return _ARROW_TYPE_FACTORIES[logical_type](pa)


_ARROW_TYPE_FACTORIES = {
    LogicalType.INT8: lambda pa: pa.int8(),
    LogicalType.INT16: lambda pa: pa.int16(),
    LogicalType.INT32: lambda pa: pa.int32(),
    LogicalType.INT64: lambda pa: pa.int64(),
    LogicalType.UINT8: lambda pa: pa.uint8(),
    LogicalType.UINT16: lambda pa: pa.uint16(),
    LogicalType.UINT32: lambda pa: pa.uint32(),
    LogicalType.UINT64: lambda pa: pa.uint64(),
    LogicalType.FLOAT32: lambda pa: pa.float32(),
    LogicalType.FLOAT64: lambda pa: pa.float64(),
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


@dataclass(frozen=True)
class RecordBatchColumn:
    """One fixed-width, null-free input column for record-batch encoding."""

    field_id: int
    logical_type: LogicalType
    payload: bytes


@dataclass(frozen=True)
class DecodedRecordBatch:
    header: BatchHeader
    columns: tuple[ColumnDescriptor, ...]
    payloads: tuple[bytes, ...]
    operations: tuple[OperationDescriptor, ...] = ()

    def column_payload(self, field_id: int) -> bytes:
        for column, payload in zip(self.columns, self.payloads):
            if column.field_id == field_id:
                return payload
        raise InvalidStreamError(f"no column with field_id {field_id}")


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
        width_bytes = LOGICAL_TYPE_BYTE_WIDTHS[normalized_type]
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

    @staticmethod
    def encode_record_batch(
        columns: Sequence[RecordBatchColumn],
        *,
        row_count: int,
        schema_hash: int = 0,
        batch_id: int = 0,
        operations: Iterable[OperationDescriptor] = (),
        limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS,
    ) -> bytes:
        """Encode N fixed-width, null-free columns as one multi-column batch."""
        if row_count < 0:
            raise InvalidStreamError("row_count cannot be negative")
        if not columns:
            raise InvalidStreamError("record batch requires at least one column")
        operation_descriptors = tuple(_normalize_operation_descriptor(operation, column_count=len(columns)) for operation in operations)

        descriptor_table_offset = BatchHeader.SIZE_BYTES
        descriptor_table_length = len(columns) * ColumnDescriptor.SIZE_BYTES + len(operation_descriptors) * OperationDescriptor.SIZE_BYTES
        first_payload_offset = _align_up(descriptor_table_offset + descriptor_table_length, _ALIGNMENT_BYTES)

        descriptors: list[ColumnDescriptor] = []
        offset = first_payload_offset
        for column in columns:
            normalized_type = _normalize_supported_type(column.logical_type)
            width_bytes = LOGICAL_TYPE_BYTE_WIDTHS[normalized_type]
            payload_bytes = bytes(column.payload)
            if len(payload_bytes) != row_count * width_bytes:
                raise InvalidStreamError(
                    f"column {column.field_id} payload length {len(payload_bytes)} does not match "
                    f"row_count {row_count} and {normalized_type.name.lower()} width {width_bytes}"
                )
            descriptors.append(
                ColumnDescriptor(
                    field_id=column.field_id,
                    logical_type=normalized_type,
                    physical_width_bits=width_bytes * 8,
                    row_count=row_count,
                    null_count=0,
                    payload_offset=offset,
                    payload_length=len(payload_bytes),
                )
            )
            offset = _align_up(offset + len(payload_bytes), _ALIGNMENT_BYTES)

        header = BatchHeader(
            schema_hash=schema_hash,
            batch_id=batch_id,
            row_count=row_count,
            column_count=len(columns),
            operation_count=len(operation_descriptors),
            group_key_count=0,
            descriptor_table_offset=descriptor_table_offset,
            payload_offset=first_payload_offset,
            payload_length=offset - first_payload_offset,
            flags=StreamFlag.NONE,
        )
        validate_record_batch(header, tuple(descriptors), limits=limits)

        encoded = header.pack()
        encoded += b"".join(descriptor.pack() for descriptor in descriptors)
        encoded += b"".join(operation.pack() for operation in operation_descriptors)
        for descriptor, column in zip(descriptors, columns):
            encoded += b"\x00" * (descriptor.payload_offset - len(encoded))
            encoded += bytes(column.payload)
        encoded += b"\x00" * (_align_up(len(encoded), _ALIGNMENT_BYTES) - len(encoded))
        return encoded

    @staticmethod
    def encode_arrow_batch(
        batch,
        *,
        schema_hash: int = 0,
        batch_id: int = 0,
        operations: Iterable[OperationDescriptor] = (),
        limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS,
    ) -> bytes:
        """Encode a PyArrow ``RecordBatch``/``Table`` of fixed-width, null-free columns."""
        columns = []
        for field_id, (name, column) in enumerate(zip(batch.schema.names, batch.columns)):
            array = column.combine_chunks() if hasattr(column, "combine_chunks") else column
            logical_type = _arrow_to_logical_type(array.type)
            if array.null_count != 0:
                raise InvalidStreamError(f"column {name!r} has nulls; record batches must be null-free")
            # zero-copy view of the primitive data buffer (offset-aware for
            # sliced arrays); one copy happens at stream assembly
            width = LOGICAL_TYPE_BYTE_WIDTHS[logical_type]
            start = array.offset * width
            payload = bytes(memoryview(array.buffers()[1])[start : start + len(array) * width])
            columns.append(RecordBatchColumn(field_id=field_id, logical_type=logical_type, payload=payload))
        return StreamEncoder.encode_record_batch(
            columns,
            row_count=batch.num_rows,
            schema_hash=schema_hash,
            batch_id=batch_id,
            operations=operations,
            limits=limits,
        )


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
    def decode_record_batch(data: bytes, *, limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS) -> DecodedRecordBatch:
        """Decode a multi-column record batch (the N-column counterpart of decode_column)."""
        encoded = bytes(data)
        if len(encoded) < BatchHeader.SIZE_BYTES:
            raise InvalidStreamError("stream data is shorter than a batch header")
        header = BatchHeader.unpack(encoded[: BatchHeader.SIZE_BYTES])
        if (header.version_major, header.version_minor) != (0, 1):
            raise InvalidStreamError(f"unsupported stream version {header.version_major}.{header.version_minor}")
        if header.header_length != BatchHeader.SIZE_BYTES:
            raise InvalidStreamError(f"unsupported batch header length {header.header_length}")
        _validate_payload_window(header.payload_offset, header.payload_length, len(encoded))

        descriptor_start = header.descriptor_table_offset
        descriptor_end = descriptor_start + header.column_count * ColumnDescriptor.SIZE_BYTES
        if descriptor_end > len(encoded):
            raise InvalidStreamError("descriptor table extends beyond stream data")
        columns = tuple(
            _unpack_column(encoded[offset : offset + ColumnDescriptor.SIZE_BYTES])
            for offset in range(descriptor_start, descriptor_end, ColumnDescriptor.SIZE_BYTES)
        )
        for column in columns:
            _normalize_supported_type(column.logical_type)
            if column.null_count != 0 or column.validity_length != 0:
                raise InvalidStreamError("record-batch decode does not support validity bitmaps yet")
            expected_width_bits = LOGICAL_TYPE_BYTE_WIDTHS[column.logical_type] * 8
            if column.physical_width_bits != expected_width_bits:
                raise InvalidStreamError(f"physical_width_bits must be {expected_width_bits} for {column.logical_type.name.lower()}")
            if column.payload_length != column.row_count * LOGICAL_TYPE_BYTE_WIDTHS[column.logical_type]:
                raise InvalidStreamError(f"column {column.field_id} payload length does not match row_count")
            _validate_payload_window(column.payload_offset, column.payload_length, len(encoded))
        validate_record_batch(header, columns, limits=limits)

        operations_start = descriptor_end
        operations_end = operations_start + header.operation_count * OperationDescriptor.SIZE_BYTES
        if operations_end > len(encoded) or operations_end > header.payload_offset:
            raise InvalidStreamError("operation descriptor table extends beyond stream data")
        operations = tuple(
            _unpack_operation(encoded[offset : offset + OperationDescriptor.SIZE_BYTES])
            for offset in range(operations_start, operations_end, OperationDescriptor.SIZE_BYTES)
        )
        _validate_operations(header, operations)

        payloads = tuple(encoded[column.payload_offset : column.payload_offset + column.payload_length] for column in columns)
        return DecodedRecordBatch(header=header, columns=columns, payloads=payloads, operations=operations)

    @staticmethod
    def decode_record_batch_to_arrow(data: bytes, *, names: Sequence[str] | None = None, limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS):
        """Decode and rematerialize as a PyArrow ``RecordBatch``."""
        import pyarrow as pa

        decoded = StreamDecoder.decode_record_batch(data, limits=limits)
        arrays = []
        fields = []
        for index, (column, payload) in enumerate(zip(decoded.columns, decoded.payloads)):
            values = struct.unpack(f"<{column.row_count}{_STRUCT_CODES[column.logical_type]}", payload)
            arrow_type = _logical_to_arrow_type(column.logical_type)
            name = names[index] if names is not None else f"f{column.field_id}"
            arrays.append(pa.array(values, type=arrow_type))
            fields.append(pa.field(name, arrow_type))
        return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))

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
    expected_width_bits = LOGICAL_TYPE_BYTE_WIDTHS[column.logical_type] * 8
    if column.physical_width_bits != expected_width_bits:
        raise InvalidStreamError(f"physical_width_bits must be {expected_width_bits} for {column.logical_type.name.lower()}")
    if column.null_count != 0 or column.validity_length != 0:
        raise InvalidStreamError("single-column decode does not support validity bitmaps yet")
    if column.row_count != header.row_count:
        raise InvalidStreamError("column row_count does not match batch header")
    if column.payload_offset != header.payload_offset or column.payload_length != header.payload_length:
        raise InvalidStreamError("column payload window does not match batch header")
    expected_payload_length = column.row_count * LOGICAL_TYPE_BYTE_WIDTHS[column.logical_type]
    if column.payload_length != expected_payload_length:
        raise InvalidStreamError("column payload length does not match row_count")
    _validate_payload_window(column.payload_offset, column.payload_length, encoded_length)


def _validate_operations(header: BatchHeader, operations: tuple[OperationDescriptor, ...]) -> None:
    if len(operations) != header.operation_count:
        raise InvalidStreamError("operation descriptor count does not match batch header")
    for operation in operations:
        if operation.input_column_id >= header.column_count:
            raise InvalidStreamError(f"operation input_column_id {operation.input_column_id} is out of range for {header.column_count} column(s)")
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
    if normalized_type not in LOGICAL_TYPE_BYTE_WIDTHS:
        raise InvalidStreamError(f"unsupported logical type {normalized_type.name.lower()}")
    return normalized_type


def _normalize_operation_descriptor(operation: OperationDescriptor, *, column_count: int = 1) -> OperationDescriptor:
    if not isinstance(operation, OperationDescriptor):
        raise InvalidStreamError(f"expected OperationDescriptor, got {type(operation).__name__}")
    _validate_operations(
        BatchHeader(
            schema_hash=0,
            batch_id=0,
            row_count=0,
            column_count=column_count,
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
