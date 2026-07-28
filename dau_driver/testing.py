"""Shared test doubles for DAU driver consumers.

Used by the dau-driver, dau, and dau-polars test suites so the fake device's
semantics cannot drift between packages. Not imported by production code.
"""

from __future__ import annotations

import struct
from pathlib import Path

from dau_core.capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from dau_core.operators import aggregate_values
from dau_core.registers import RegisterMapVersion, StreamProtocolVersion
from dau_core.stream import InvalidStreamError, LogicalType, OperationCode

from .device import DauDevice
from .stream_codec import StreamDecoder, StreamEncoder


class FakeTransport:
    """In-memory DeviceTransport: records DMA traffic and register writes."""

    def __init__(self) -> None:
        self.h2c_writes: list[tuple[int, bytes]] = []
        self.c2h_reads: list[tuple[int, int]] = []
        self.c2h_payloads: dict[int, bytes] = {}
        self.registers: dict[int, int] = {}
        self.register_reads: list[int] = []

    def read_u32(self, offset: int) -> int:
        self.register_reads.append(offset)
        return self.registers.get(offset, 0)

    def write_u32(self, offset: int, value: int) -> None:
        self.registers[offset] = value

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


def default_int32_capabilities(
    operators: tuple[OperatorCapability, ...] | None = None,
    *,
    max_transfer_bytes: int = 4096,
) -> DeviceCapabilities:
    """The standard fake INT32 aggregation device capability set."""
    if operators is None:
        operators = (
            OperatorCapability(OperationCode.SUM, (LogicalType.INT32,), LogicalType.INT64, supports_group_by=False),
            OperatorCapability(OperationCode.MIN, (LogicalType.INT32,), LogicalType.INT64, supports_group_by=False),
            OperatorCapability(OperationCode.MAX, (LogicalType.INT32,), LogicalType.INT64, supports_group_by=False),
            OperatorCapability(OperationCode.COUNT, (LogicalType.INT32,), LogicalType.UINT64, supports_group_by=False),
        )
    return DeviceCapabilities(
        platform_id="DPV1",
        pci_vendor_id=0x10EE,
        pci_device_id=0x7011,
        register_map_version=RegisterMapVersion(0, 1),
        stream_protocol_version=StreamProtocolVersion(0, 1),
        dma=DmaConstraints(alignment_bytes=64, descriptor_bytes=32, max_transfer_bytes=max_transfer_bytes, read_channels=1, write_channels=1),
        operators=operators,
    )


def fake_device(tmp_path: Path, transport: FakeTransport, *, capabilities: DeviceCapabilities | None = None) -> DauDevice:
    """A DauDevice backed by a FakeTransport (paths are never opened)."""
    return DauDevice(
        name="xdma0",
        backend="linux-xdma",
        user_path=tmp_path / "xdma0_user",
        h2c_path=tmp_path / "xdma0_h2c_0",
        c2h_path=tmp_path / "xdma0_c2h_0",
        capabilities=capabilities if capabilities is not None else default_int32_capabilities(),
        transport=transport,
    )


def golden_aggregation_trigger(transport: FakeTransport, output_address: int):
    """A StreamTrigger that emulates the aggregation hardware: decode the
    encoded input batch, compute the golden scalar, stage the result stream
    at ``output_address`` for the subsequent C2H read."""

    def trigger(device: DauDevice, encoded_input: bytes) -> None:  # noqa: ARG001 (StreamTrigger interface)
        decoded = StreamDecoder.decode_column(encoded_input)
        values = list(struct.unpack(f"<{decoded.column.row_count}i", decoded.payload))
        operation = decoded.operations[0]
        golden = aggregate_values(operation.opcode, values, LogicalType.INT32)
        result_format = "<Q" if operation.output_type == LogicalType.UINT64 else "<q"
        transport.c2h_payloads[output_address] = StreamEncoder.encode_column(
            operation.output_type, struct.pack(result_format, int(golden)), row_count=1
        )

    return trigger
