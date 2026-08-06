"""Shared register-block decode semantics for every DAU transport backend.

linux-xdma, macos-xdma, and the device layer all decode the same identity
window; one definition here so a semantics change cannot silently diverge
the backends (they were verbatim copies before, and had already drifted).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from dau_core.capabilities import DPV1_PLATFORM_ID, OperatorCapability
from dau_core.capability_contract import CapabilityContract
from dau_core.registers import DEFAULT_STREAM_JOB_REGISTER_CONTRACT, RegisterBlockSnapshot, RegisterMapVersion, RegisterOffset, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode, output_type_for

# the register-level type claim of the bitmap words — every
# opcode-driven tile in the palette is INT32 (per-tile types travel in the
# manifest capability contract, not in registers)
REGISTER_BITMAP_INPUT_TYPES = (LogicalType.INT32,)
CAPABILITY_WORDS_REGISTER_MAP_VERSION = RegisterMapVersion(0, 2)
# identity window candidates: shell base, then the stream-job contract window
REGISTER_WINDOW_OFFSETS = (0x0, DEFAULT_STREAM_JOB_REGISTER_CONTRACT.register_window_offset)


def decode_platform_id(value: int) -> str:
    platform_bytes = value.to_bytes(4, "little").rstrip(b"\x00")
    if not platform_bytes or any(byte < 0x20 or byte > 0x7E for byte in platform_bytes):
        return ""
    return platform_bytes.decode("ascii")


# all-ones on an identity offset means the shell does not decode it. MAGIC
# reading correctly is what proves the path healthy; see check_bar_health.
NOT_ADVERTISED_REGISTER_MAP_VERSION = RegisterMapVersion(0, 0)


def decode_register_map_version(value: int) -> RegisterMapVersion:
    """Decode the register-map version word.

    An all-ones read means NOT ADVERTISED — the same normalization the
    capability words use — not a version. Two readers used to disagree on
    exactly this word: one mapped it to (0, 1), a plausible-looking old
    shell, while the other ran it through ``from_u32`` and got
    (65535, 65535), which compares GREATER than the capability-word
    version and so read words the shell never decoded. A sentinel below
    every real version makes both readers agree and keeps those offsets
    unread.
    """
    if value == 0xFFFF_FFFF:
        return NOT_ADVERTISED_REGISTER_MAP_VERSION
    return RegisterMapVersion.from_u32(value)


def _typed_operators_from_bitmap(bitmap: int, input_types: tuple[LogicalType, ...]) -> tuple[OperatorCapability, ...]:
    return tuple(
        OperatorCapability(
            opcode=opcode,
            input_types=input_types,
            output_type=output_type_for(opcode),
            supports_group_by=False,
        )
        for opcode in OperationCode
        if bitmap & (1 << int(opcode))
    )


def operators_from_snapshot(snapshot: RegisterBlockSnapshot) -> tuple[tuple[OperatorCapability, ...], tuple[OperatorCapability, ...]]:
    """(hardware operators, host-computed operators) from the identity
    registers, decoded by register map version.

    OPERATOR_BITMAP carries only HDL-executed opcodes (typed INT32 at
    register level) and HOST_OPCODE_BITMAP carries protocol-level opcodes
    the host derives from hardware results (AVERAGE = SUM/COUNT divide) —
    those must never be submitted to the device. A shell that advertises
    neither word reports nothing here, and its manifest capability
    contract is the authority."""
    return (
        _typed_operators_from_bitmap(snapshot.operator_bitmap, REGISTER_BITMAP_INPUT_TYPES),
        _typed_operators_from_bitmap(snapshot.host_opcode_bitmap, REGISTER_BITMAP_INPUT_TYPES),
    )


def contract_from_manifest_metadata(metadata: Mapping[str, str]) -> CapabilityContract | None:
    """The machine-readable capability contract a shell-build manifest
    carries (``capability_contract`` key, JSON), when present."""
    serialized = metadata.get("capability_contract")
    if not serialized:
        return None
    import json

    return CapabilityContract.from_mapping(json.loads(serialized))


def operators_from_contract(contract: CapabilityContract) -> tuple[tuple[OperatorCapability, ...], tuple[OperatorCapability, ...]]:
    """(hardware operators, host-computed operators) from the concrete
    composition capability contract — per-tile opcodes and input types,
    no bitmap-times-types expansion."""
    hardware: dict[OperationCode, set[LogicalType]] = {}
    host: dict[OperationCode, set[LogicalType]] = {}
    for lane in contract.lanes:
        for opcode in lane.tile.opcodes:
            hardware.setdefault(opcode, set()).update(lane.tile.input_types)
        for opcode in lane.tile.host_opcodes:
            host.setdefault(opcode, set()).update(lane.tile.input_types)

    def build(table: dict[OperationCode, set[LogicalType]]) -> tuple[OperatorCapability, ...]:
        return tuple(
            OperatorCapability(
                opcode=opcode,
                input_types=tuple(sorted(table[opcode], key=int)),
                output_type=output_type_for(opcode),
                supports_group_by=False,
            )
            for opcode in sorted(table, key=int)
        )

    return build(hardware), build(host)


def _capability_word(read_register: Callable[[RegisterOffset], int], offset: RegisterOffset) -> int:
    # all-ones means the word is not decoded (should not happen on a 0.2
    # shell); treat it as not-advertised rather than a 4-billion lane count
    # or a device claiming every opcode it has a bit for
    value = read_register(offset)
    return 0 if value == 0xFFFF_FFFF else value


def _build_id(read_register: Callable[[RegisterOffset], int]) -> int:
    """The shell's 64-bit build identity, as TWO 32-bit reads.

    Deliberately not one 8-byte access: the XDMA user BAR can short-read
    those. Low half at BUILD_ID_LOW, high at BUILD_ID_HIGH, matching the
    identity block's split.

    All-ones normalizes to 0 like every other capability word — a word
    nobody decoded advertises nothing, and 0 is the reserved "not stamped"
    value a consumer already has to handle for shells built before stamping
    existed.
    """
    low = _capability_word(read_register, RegisterOffset.BUILD_ID_LOW)
    high = _capability_word(read_register, RegisterOffset.BUILD_ID_HIGH)
    return (high << 32) | low


def _capability_words(read_register: Callable[[RegisterOffset], int], register_map_version: RegisterMapVersion) -> dict[str, int]:
    """The composed-capability words, read only from a shell that
    advertises them. Shared by every snapshot reader so the gate and the
    all-ones normalization cannot diverge between them."""
    if register_map_version < CAPABILITY_WORDS_REGISTER_MAP_VERSION:
        return {}
    return {
        "lane_count": _capability_word(read_register, RegisterOffset.LANE_COUNT),
        "host_opcode_bitmap": _capability_word(read_register, RegisterOffset.HOST_OPCODE_BITMAP),
        "sort_capacity": _capability_word(read_register, RegisterOffset.SORT_CAPACITY),
    }


def read_register_block_snapshot(read_register: Callable[[RegisterOffset], int]) -> RegisterBlockSnapshot:
    """Discovery-time identity snapshot; ``read_register`` abstracts the
    backend-specific window access (dev-node pread vs DriverKit BAR read).
    The composed-capability words are only read on register map >= 0.2 —
    older shells do not decode those offsets."""
    register_map_version = decode_register_map_version(read_register(RegisterOffset.REGISTER_MAP_VERSION))
    capability_words = _capability_words(read_register, register_map_version)
    return RegisterBlockSnapshot(
        register_map_version=register_map_version,
        stream_protocol_version=StreamProtocolVersion.from_u32(read_register(RegisterOffset.STREAM_PROTOCOL_VERSION)),
        platform_id=decode_platform_id(read_register(RegisterOffset.PLATFORM_ID)) or DPV1_PLATFORM_ID,
        # an all-ones operator word is a word nobody decoded, NOT a device
        # that implements every opcode. Read raw it advertises AVERAGE as
        # hardware, which this module states must never be submitted.
        operator_bitmap=_capability_word(read_register, RegisterOffset.OPERATOR_BITMAP),
        build_id=_build_id(read_register),
        **capability_words,
    )
