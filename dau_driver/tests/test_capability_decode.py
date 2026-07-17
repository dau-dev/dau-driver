from __future__ import annotations

import json

from dau_core.capability_contract import CapabilityContract, LaneCapability, tile_capability
from dau_core.registers import RegisterBlockSnapshot, RegisterMapVersion, RegisterOffset
from dau_core.stream import LogicalType, OperationCode

from dau_driver.capability_decode import (
    DEFAULT_INPUT_TYPES,
    contract_from_manifest_metadata,
    operators_from_bitmap,
    operators_from_contract,
    operators_from_snapshot,
    read_register_block_snapshot,
)


def test_legacy_bitmap_expansion_is_kept_for_register_map_0_1() -> None:
    """The proven resident dpv1 bitstream reports 0.1/0x1E; its decode
    stays byte-for-byte the historical five-type expansion."""
    snapshot = RegisterBlockSnapshot(register_map_version=RegisterMapVersion(0, 1), operator_bitmap=0x1E)
    operators, host_operators = operators_from_snapshot(snapshot)
    assert operators == operators_from_bitmap(0x1E)
    assert host_operators == ()
    assert {operator.opcode for operator in operators} == {OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT}
    assert all(operator.input_types == DEFAULT_INPUT_TYPES for operator in operators)


def test_register_map_0_2_decodes_hardware_and_host_words_concretely() -> None:
    snapshot = RegisterBlockSnapshot(register_map_version=RegisterMapVersion(0, 2), operator_bitmap=0x1E, host_opcode_bitmap=0x20)
    operators, host_operators = operators_from_snapshot(snapshot)
    assert {operator.opcode for operator in operators} == {OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT}
    assert all(operator.input_types == (LogicalType.INT32,) for operator in operators)
    assert [operator.opcode for operator in host_operators] == [OperationCode.AVERAGE]
    # a truthfully-empty 0.2 advertisement decodes to no capability at all
    empty, empty_host = operators_from_snapshot(RegisterBlockSnapshot(register_map_version=RegisterMapVersion(0, 2)))
    assert empty == () and empty_host == ()


def test_snapshot_reader_gates_capability_words_by_register_map() -> None:
    image_0_1 = {
        RegisterOffset.REGISTER_MAP_VERSION: 0x1,
        RegisterOffset.STREAM_PROTOCOL_VERSION: 0x1,
        RegisterOffset.PLATFORM_ID: int.from_bytes(b"NITE", "little"),
        RegisterOffset.OPERATOR_BITMAP: 0x1E,
    }
    reads: list[RegisterOffset] = []

    def read_0_1(offset: RegisterOffset) -> int:
        reads.append(offset)
        return image_0_1.get(offset, 0xFFFF_FFFF)

    snapshot = read_register_block_snapshot(read_0_1)
    assert snapshot.register_map_version == RegisterMapVersion(0, 1)
    assert snapshot.lane_count == 0 and snapshot.host_opcode_bitmap == 0 and snapshot.sort_capacity == 0
    # a 0.1 shell does not decode the capability offsets, so they are never read
    assert RegisterOffset.LANE_COUNT not in reads

    image_0_2 = dict(image_0_1)
    image_0_2[RegisterOffset.REGISTER_MAP_VERSION] = 0x2
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
            LaneCapability(tile=tile_capability("int32-bitonic-sorter")),
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
