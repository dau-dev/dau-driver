from __future__ import annotations

import struct
from pathlib import Path

import pytest
from dau_core.capabilities import OperatorCapability
from dau_core.stream import LogicalType, OperationCode, OperationDescriptor

from dau_driver import DauDevice
from dau_driver.stream_codec import StreamDecoder, StreamEncoder
from dau_driver.stream_execution import StreamExecutionError, execute_scalar_operation
from dau_driver.testing import FakeTransport, default_int32_capabilities, fake_device


def test_execute_scalar_operation_writes_encoded_stream_triggers_device_and_decodes_result(tmp_path: Path) -> None:
    transport = FakeTransport()
    device = _make_device(tmp_path, transport)
    operation = _operation(OperationCode.SUM, output_type=LogicalType.INT64)
    payload = struct.pack("<3i", 1, 2, 3)

    def trigger(triggered_device: DauDevice, encoded_input: bytes) -> None:
        assert triggered_device is device
        decoded_input = StreamDecoder.decode_column(encoded_input)
        assert decoded_input.column.logical_type == LogicalType.INT32
        assert decoded_input.operations == (operation,)
        transport.c2h_payloads[0x2000] = StreamEncoder.encode_column(LogicalType.INT64, struct.pack("<q", 6), row_count=1, field_id=9)

    result = execute_scalar_operation(
        device,
        LogicalType.INT32,
        payload,
        row_count=3,
        operation=operation,
        input_device_address=0x1000,
        output_device_address=0x2000,
        output_byte_count=192,
        trigger=trigger,
    )

    assert result.value == 6
    assert transport.h2c_writes == [(0x1000, StreamEncoder.encode_column(LogicalType.INT32, payload, row_count=3, operations=(operation,)))]
    assert transport.c2h_reads == [(0x2000, 192)]


def test_execute_scalar_operation_rejects_unsupported_operation_before_dma(tmp_path: Path) -> None:
    transport = FakeTransport()
    device = _make_device(tmp_path, transport)

    with pytest.raises(StreamExecutionError, match="does not support average for int32"):
        execute_scalar_operation(
            device,
            LogicalType.INT32,
            struct.pack("<2i", 1, 2),
            row_count=2,
            operation=_operation(OperationCode.AVERAGE, output_type=LogicalType.INT64),
            input_device_address=0x1000,
            output_device_address=0x2000,
            output_byte_count=192,
        )

    assert transport.h2c_writes == []
    assert transport.c2h_reads == []


def test_execute_scalar_operation_rejects_capability_output_type_mismatch(tmp_path: Path) -> None:
    transport = FakeTransport()
    device = _make_device(tmp_path, transport)

    with pytest.raises(StreamExecutionError, match="outputs int64, not uint64"):
        execute_scalar_operation(
            device,
            LogicalType.INT32,
            struct.pack("<2i", 1, 2),
            row_count=2,
            operation=_operation(OperationCode.SUM, output_type=LogicalType.UINT64),
            input_device_address=0x1000,
            output_device_address=0x2000,
            output_byte_count=192,
        )

    assert transport.h2c_writes == []


def _make_device(tmp_path: Path, transport: FakeTransport) -> DauDevice:
    capabilities = default_int32_capabilities(
        operators=(OperatorCapability(OperationCode.SUM, (LogicalType.INT32,), LogicalType.INT64, supports_group_by=False),),
    )
    return fake_device(tmp_path, transport, capabilities=capabilities)


def _operation(opcode: OperationCode, *, output_type: LogicalType) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id=1,
        opcode=opcode,
        input_column_id=0,
        output_field_id=9,
        output_type=output_type,
        accumulator_width_bits=64,
    )
