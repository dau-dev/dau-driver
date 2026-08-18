from __future__ import annotations

import json

from dau_core.capability_contract import CapabilityContract, LaneCapability, tile_capability
from dau_core.registers import RegisterBlockSnapshot, RegisterMapVersion, RegisterOffset
from dau_core.stream import LogicalType, OperationCode

from dau_driver.capability_decode import (
    contract_from_manifest_metadata,
    decode_register_map_version,
    operators_from_contract,
    operators_from_snapshot,
    read_register_block_snapshot,
)


def test_an_all_ones_version_word_decodes_as_not_advertised() -> None:
    """Two readers used to disagree on this exact word: one mapped it to
    (0, 1) — a plausible-looking old shell — while the other ran it
    through from_u32 and got (65535, 65535), which compares GREATER than
    the capability-word version and so read words the shell never
    decoded. A sentinel below every real version makes them agree."""
    from dau_driver.capability_decode import NOT_ADVERTISED_REGISTER_MAP_VERSION

    assert decode_register_map_version(0xFFFF_FFFF) == NOT_ADVERTISED_REGISTER_MAP_VERSION
    assert NOT_ADVERTISED_REGISTER_MAP_VERSION < RegisterMapVersion(0, 2)
    assert decode_register_map_version(0x2) == RegisterMapVersion(0, 2)


def test_the_bitmap_words_decode_concretely() -> None:
    snapshot = RegisterBlockSnapshot(register_map_version=RegisterMapVersion(0, 2), operator_bitmap=0x1E, host_opcode_bitmap=0x20)
    operators, host_operators = operators_from_snapshot(snapshot)
    assert {operator.opcode for operator in operators} == {OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT}
    assert all(operator.input_types == (LogicalType.INT32,) for operator in operators)
    assert [operator.opcode for operator in host_operators] == [OperationCode.AVERAGE]
    # a truthfully-empty 0.2 advertisement decodes to no capability at all
    empty, empty_host = operators_from_snapshot(RegisterBlockSnapshot(register_map_version=RegisterMapVersion(0, 2)))
    assert empty == () and empty_host == ()


def test_snapshot_reader_normalizes_undecoded_capability_words() -> None:
    image_0_2 = {
        RegisterOffset.REGISTER_MAP_VERSION: 0x2,
        RegisterOffset.STREAM_PROTOCOL_VERSION: 0x1,
        RegisterOffset.PLATFORM_ID: int.from_bytes(b"DPV1", "little"),
        RegisterOffset.OPERATOR_BITMAP: 0x1E,
    }
    image_0_2[RegisterOffset.LANE_COUNT] = 4
    image_0_2[RegisterOffset.HOST_OPCODE_BITMAP] = 0x20
    image_0_2[RegisterOffset.SORT_CAPACITY] = 16
    snapshot = read_register_block_snapshot(lambda offset: image_0_2.get(offset, 0xFFFF_FFFF))
    assert snapshot.lane_count == 4
    assert snapshot.host_opcode_bitmap == 0x20
    assert snapshot.sort_capacity == 16

    # an undecoded (all-ones) capability word reads as not-advertised
    image_0_2.pop(RegisterOffset.SORT_CAPACITY)
    snapshot = read_register_block_snapshot(lambda offset: image_0_2.get(offset, 0xFFFF_FFFF))
    assert snapshot.sort_capacity == 0


def test_operators_from_contract_uses_per_tile_truth_not_expansion() -> None:
    contract = CapabilityContract(
        name="mixed",
        lanes=(
            LaneCapability(tile=tile_capability("int32-record-batch-aggregation")),
            LaneCapability(tile=tile_capability("bitonic-sorter")),
        ),
    )
    operators, host_operators = operators_from_contract(contract)
    assert {operator.opcode for operator in operators} == {OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT}
    assert all(operator.input_types == (LogicalType.INT32,) for operator in operators)
    assert [operator.opcode for operator in host_operators] == [OperationCode.AVERAGE]
    assert host_operators[0].output_type == LogicalType.FLOAT64

    bar_only = CapabilityContract(name="bar", lanes=(LaneCapability(tile=tile_capability("int32-bar-aggregation")),))
    assert operators_from_contract(bar_only) == ((), ())


def test_contract_from_manifest_metadata_round_trips() -> None:
    contract = CapabilityContract(name="record-batch", lanes=(LaneCapability(tile=tile_capability("int32-record-batch-aggregation")),))
    metadata = {"capability_contract": json.dumps(contract.to_mapping(), sort_keys=True, separators=(",", ":"))}
    assert contract_from_manifest_metadata(metadata) == contract
    assert contract_from_manifest_metadata({}) is None


def test_device_snapshot_normalizes_undecoded_capability_words(tmp_path) -> None:
    from dau_driver.discovery import discover_devices
    from dau_driver.tests.test_discovery import write_fake_xdma_device, write_u32

    paths = write_fake_xdma_device(tmp_path, register_map_version=RegisterMapVersion(0, 2), lane_count=4)
    image = bytearray(paths.user.read_bytes())
    write_u32(image, 0, RegisterOffset.SORT_CAPACITY, 0xFFFF_FFFF)
    paths.user.write_bytes(bytes(image))

    snapshot = discover_devices(tmp_path)[0].read_register_block_snapshot()
    assert snapshot.lane_count == 4
    assert snapshot.sort_capacity == 0  # all-ones = not decoded = not advertised
