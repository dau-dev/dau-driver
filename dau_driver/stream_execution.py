from __future__ import annotations

from collections.abc import Callable

from dau_core.capabilities import DeviceCapabilities
from dau_core.stream import LogicalType, OperationDescriptor

from .device import DauDevice
from .stream_codec import DecodedStreamResult, StreamDecoder, StreamEncoder


class StreamExecutionError(ValueError):
    pass


StreamTrigger = Callable[[DauDevice, bytes], None]


def execute_scalar_operation(
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
) -> DecodedStreamResult:
    _validate_operation_supported(device.capabilities, logical_type, operation)
    encoded_input = StreamEncoder.encode_column(logical_type, payload, row_count=row_count, operations=(operation,))
    device.write_dma(device_address=input_device_address, data=encoded_input, timeout_seconds=timeout_seconds)
    if trigger is not None:
        trigger(device, encoded_input)
    encoded_result = device.read_dma(device_address=output_device_address, byte_count=output_byte_count, timeout_seconds=timeout_seconds)
    return StreamDecoder.decode_scalar_result(encoded_result)


def _validate_operation_supported(capabilities: DeviceCapabilities, logical_type: LogicalType, operation: OperationDescriptor) -> None:
    input_type = LogicalType(logical_type)
    output_type = LogicalType(operation.output_type)
    for operator in capabilities.operators:
        if operator.opcode == operation.opcode and input_type in operator.input_types:
            if operator.output_type != output_type:
                raise StreamExecutionError(
                    f"{operation.opcode.name.lower()} for {input_type.name.lower()} outputs {operator.output_type.name.lower()}, not {output_type.name.lower()}"
                )
            return
    raise StreamExecutionError(f"device does not support {operation.opcode.name.lower()} for {input_type.name.lower()}")
