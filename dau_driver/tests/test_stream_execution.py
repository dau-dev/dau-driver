from __future__ import annotations

import struct
from pathlib import Path

import pytest
from dau_core.capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from dau_core.registers import RegisterMapVersion, StreamProtocolVersion
from dau_core.stream import InvalidStreamError, LogicalType, OperationCode, OperationDescriptor

from dau_driver import DauDevice
from dau_driver.stream_codec import StreamDecoder, StreamEncoder
from dau_driver.stream_execution import StreamExecutionError, execute_scalar_operation


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


class FakeTransport:
    def __init__(self) -> None:
        self.h2c_writes: list[tuple[int, bytes]] = []
        self.c2h_reads: list[tuple[int, int]] = []
        self.c2h_payloads: dict[int, bytes] = {}

    def read_u32(self, offset: int) -> int:
        return 0

    def write_u32(self, offset: int, value: int) -> None:
        pass

    def read_dma(self, device_address: int, byte_count: int) -> bytes:
        self.c2h_reads.append((device_address, byte_count))
        try:
            payload = self.c2h_payloads[device_address]
        except KeyError as exc:
            raise InvalidStreamError(f"no fake C2H payload at 0x{device_address:x}") from exc
        return payload[:byte_count]

    def write_dma(self, device_address: int, data: bytes | bytearray | memoryview) -> int:
        payload = bytes(data)
        self.h2c_writes.append((device_address, payload))
        return len(payload)


def _make_device(tmp_path: Path, transport: FakeTransport) -> DauDevice:
    return DauDevice(
        name="xdma0",
        backend="linux-xdma",
        user_path=tmp_path / "xdma0_user",
        h2c_path=tmp_path / "xdma0_h2c_0",
        c2h_path=tmp_path / "xdma0_c2h_0",
        capabilities=DeviceCapabilities(
            platform_id="NITE",
            pci_vendor_id=0x10EE,
            pci_device_id=0x7011,
            register_map_version=RegisterMapVersion(0, 1),
            stream_protocol_version=StreamProtocolVersion(0, 1),
            dma=DmaConstraints(alignment_bytes=64, descriptor_bytes=32, max_transfer_bytes=4096, read_channels=1, write_channels=1),
            operators=(OperatorCapability(OperationCode.SUM, (LogicalType.INT32,), LogicalType.INT64, supports_group_by=False),),
        ),
        transport=transport,
    )


def _operation(opcode: OperationCode, *, output_type: LogicalType) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id=1,
        opcode=opcode,
        input_column_id=0,
        output_field_id=9,
        output_type=output_type,
        accumulator_width_bits=64,
    )
