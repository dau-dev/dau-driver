from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from dau_driver.verify_advertisement import advertisement_mismatches, contract_from_manifest_path

# the four words the hash-grouped-scan build advertises
_CONTRACT = {
    "name": "hash-grouped-scan",
    "lane_count": 1,
    "operator_bitmap": "0x00000018",
    "host_opcode_bitmap": "0x00000020",
    "sort_capacity": 0,
}


@dataclass
class _Snapshot:
    lane_count: int
    operator_bitmap: int
    host_opcode_bitmap: int
    sort_capacity: int


def _manifest(tmp_path: Path, contract=_CONTRACT) -> Path:
    path = tmp_path / "shell-build.artifacts.yaml"
    path.write_text(yaml.safe_dump({"name": "s", "metadata": {"capability_contract": json.dumps(contract)}}), encoding="utf-8")
    return path


def test_a_matching_device_reports_no_mismatches() -> None:
    snapshot = _Snapshot(lane_count=1, operator_bitmap=0x18, host_opcode_bitmap=0x20, sort_capacity=0)
    assert advertisement_mismatches(snapshot, _CONTRACT) == ()


def test_the_resident_design_that_fooled_the_flash_is_caught() -> None:
    """The case this exists for. A volatile program that does not take
    leaves the FPGA on its flash-resident design, and the endpoint
    enumerates exactly as it would on success — same vendor, same device id,
    same address. Only the composed advertisement differs: here the resident
    shell carries three lanes where the manifest declares one."""
    resident = _Snapshot(lane_count=3, operator_bitmap=0x18, host_opcode_bitmap=0x20, sort_capacity=0)
    (mismatch,) = advertisement_mismatches(resident, _CONTRACT)
    assert "lane_count" in mismatch
    assert "device advertises 3" in mismatch and "manifest expects 1" in mismatch


def test_every_mismatch_is_reported_not_just_the_first() -> None:
    """A caller staring at a failed flash wants all of them: 'lane count
    differs' and 'operator bitmap differs' have different causes."""
    wrong = _Snapshot(lane_count=4, operator_bitmap=0x6, host_opcode_bitmap=0x20, sort_capacity=8)
    mismatches = advertisement_mismatches(wrong, _CONTRACT)
    assert len(mismatches) == 3
    assert {"lane_count", "operator_bitmap", "sort_capacity"} == {m.split(":")[0] for m in mismatches}


def test_hex_and_int_spellings_of_a_word_compare_equal() -> None:
    contract = {**_CONTRACT, "operator_bitmap": 24}  # 0x18 as an int
    snapshot = _Snapshot(lane_count=1, operator_bitmap=0x18, host_opcode_bitmap=0x20, sort_capacity=0)
    assert advertisement_mismatches(snapshot, contract) == ()


def test_a_word_the_manifest_does_not_record_is_skipped() -> None:
    """An older manifest predating a word must not fail a device that
    advertises it -- absence is not disagreement."""
    contract = {"lane_count": 1}
    snapshot = _Snapshot(lane_count=1, operator_bitmap=0xDEAD, host_opcode_bitmap=0xBEEF, sort_capacity=99)
    assert advertisement_mismatches(snapshot, contract) == ()


def test_contract_is_read_from_a_manifest(tmp_path: Path) -> None:
    assert contract_from_manifest_path(_manifest(tmp_path))["lane_count"] == 1


def test_a_manifest_without_a_contract_refuses(tmp_path: Path) -> None:
    path = tmp_path / "shell-build.artifacts.yaml"
    path.write_text(yaml.safe_dump({"name": "s", "metadata": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no capability_contract"):
        contract_from_manifest_path(path)


def test_a_contract_that_advertises_nothing_is_refused() -> None:
    """All-zero is exactly what a dead or undecoded aperture reads back as,
    because the driver normalizes all-ones to zero. A contract of all zeros
    would therefore MATCH a board that is not answering, which is the
    failure this module exists to prevent."""
    dead = _Snapshot(lane_count=0, operator_bitmap=0, host_opcode_bitmap=0, sort_capacity=0)
    with pytest.raises(ValueError, match="cannot identify a design"):
        advertisement_mismatches(dead, {"lane_count": 0, "operator_bitmap": 0, "host_opcode_bitmap": 0, "sort_capacity": 0})


def test_a_dead_aperture_fails_against_a_real_contract() -> None:
    dead = _Snapshot(lane_count=0, operator_bitmap=0, host_opcode_bitmap=0, sort_capacity=0)
    mismatches = advertisement_mismatches(dead, _CONTRACT)
    assert {"lane_count", "operator_bitmap", "host_opcode_bitmap"} == {m.split(":")[0] for m in mismatches}
