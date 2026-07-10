"""Shared register-block decode semantics for every DAU transport backend.

linux-xdma, macos-xdma, and the device layer all decode the same identity
window; one definition here so a semantics change cannot silently diverge
the backends (they were verbatim copies before, and had already drifted).
"""

from __future__ import annotations

from collections.abc import Callable

from dau_core.capabilities import DPV1_PLATFORM_ID, OperatorCapability
from dau_core.registers import DEFAULT_STREAM_JOB_REGISTER_CONTRACT, RegisterBlockSnapshot, RegisterMapVersion, RegisterOffset, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode, output_type_for

DEFAULT_INPUT_TYPES = (
    LogicalType.INT8,
    LogicalType.INT16,
    LogicalType.INT32,
    LogicalType.INT64,
    LogicalType.FLOAT32,
)
# identity window candidates: shell base, then the stream-job contract window
REGISTER_WINDOW_OFFSETS = (0x0, DEFAULT_STREAM_JOB_REGISTER_CONTRACT.register_window_offset)


def decode_platform_id(value: int) -> str:
    platform_bytes = value.to_bytes(4, "little").rstrip(b"\x00")
    if not platform_bytes or any(byte < 0x20 or byte > 0x7E for byte in platform_bytes):
        return ""
    return platform_bytes.decode("ascii")


def decode_register_map_version(value: int) -> RegisterMapVersion:
    if value == 0xFFFF_FFFF:
        return RegisterMapVersion(0, 1)
    return RegisterMapVersion.from_u32(value)


def operators_from_bitmap(operator_bitmap: int) -> tuple[OperatorCapability, ...]:
    operators = []
    for opcode in OperationCode:
        if operator_bitmap & (1 << int(opcode)):
            operators.append(
                OperatorCapability(
                    opcode=opcode,
                    input_types=DEFAULT_INPUT_TYPES,
                    output_type=output_type_for(opcode),
                    supports_group_by=False,
                )
            )
    return tuple(operators)


def read_register_block_snapshot(read_register: Callable[[RegisterOffset], int]) -> RegisterBlockSnapshot:
    """Discovery-time identity snapshot; ``read_register`` abstracts the
    backend-specific window access (dev-node pread vs DriverKit BAR read)."""
    return RegisterBlockSnapshot(
        register_map_version=decode_register_map_version(read_register(RegisterOffset.REGISTER_MAP_VERSION)),
        stream_protocol_version=StreamProtocolVersion.from_u32(read_register(RegisterOffset.STREAM_PROTOCOL_VERSION)),
        platform_id=decode_platform_id(read_register(RegisterOffset.PLATFORM_ID)) or DPV1_PLATFORM_ID,
        operator_bitmap=read_register(RegisterOffset.OPERATOR_BITMAP),
    )
