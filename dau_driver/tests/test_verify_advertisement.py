from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from dau_core.capability_contract import CapabilityContract, CoreCapability, LaneCapability
from dau_core.design import UnitKind
from dau_core.registers import RegisterBlockSnapshot
from dau_core.stream import LogicalType, OperationCode

from dau_driver.verify_advertisement import advertisement_mismatches, contract_from_manifest_path


def _lane(core: str = "hash-grouped-scan") -> LaneCapability:
    return LaneCapability(
        sink=CoreCapability(
            core=core,
            module=f"dau_{core.replace('-', '_')}",
            contract_kind=None,
            count_port=None,
            opcodes=(OperationCode.SUM, OperationCode.COUNT),
            input_types=(LogicalType.INT32,),
            unit_kind=UnitKind.AGGREGATION,
        )
    )


# the hash-grouped-scan build: one lane, SUM and COUNT in hardware (0x18),
# AVERAGE on the host (0x20), no sort
_CONTRACT = CapabilityContract(name="hash-grouped-scan", lanes=(_lane(),))


def _snapshot(*, lane_count: int = 1, operator_bitmap: int = 0x18, host_opcode_bitmap: int = 0x20, sort_capacity: int = 0, build_id: int = 0):
    return RegisterBlockSnapshot(
        lane_count=lane_count,
        operator_bitmap=operator_bitmap,
        host_opcode_bitmap=host_opcode_bitmap,
        sort_capacity=sort_capacity,
        build_id=build_id,
    )


def _manifest(tmp_path: Path, contract=None, **metadata) -> Path:
    serialized = json.dumps((_CONTRACT if contract is None else contract).to_mapping())
    path = tmp_path / "shell-build.artifacts.yaml"
    path.write_text(yaml.safe_dump({"name": "s", "metadata": {"capability_contract": serialized, **metadata}}), encoding="utf-8")
    return path


def test_a_matching_device_reports_no_mismatches() -> None:
    assert advertisement_mismatches(_snapshot(), _CONTRACT) == ()


def test_the_resident_design_that_fooled_the_flash_is_caught() -> None:
    """The case this exists for. A volatile program that does not take
    leaves the FPGA on its flash-resident design, and the endpoint
    enumerates exactly as it would on success — same vendor, same device id,
    same address. Only the composed advertisement differs: here the resident
    shell has three lanes where the manifest declares one."""
    (mismatch,) = advertisement_mismatches(_snapshot(lane_count=3), _CONTRACT)
    assert "lane_count" in mismatch
    assert "device advertises 3" in mismatch and "manifest expects 1" in mismatch


def test_every_mismatch_is_reported_not_just_the_first() -> None:
    """A caller staring at a failed flash wants all of them: 'lane count
    differs' and 'operator bitmap differs' have different causes."""
    mismatches = advertisement_mismatches(_snapshot(lane_count=4, operator_bitmap=0x6, sort_capacity=8), _CONTRACT)
    assert len(mismatches) == 3
    assert {"lane_count", "operator_bitmap", "sort_capacity"} == {m.split(":")[0] for m in mismatches}


def test_contract_is_read_from_a_manifest_as_the_typed_model(tmp_path: Path) -> None:
    contract, spec_hash, platform = contract_from_manifest_path(_manifest(tmp_path, spec_hash="5:abc", platform="dpv1"))
    assert contract == _CONTRACT
    assert (spec_hash, platform) == ("5:abc", "dpv1")


def test_the_words_compared_are_the_contract_lanes_not_its_summary_fields(tmp_path: Path) -> None:
    """The manifest's summary words are a serialization of its lanes. The
    lanes are what the typed contract keeps, so a summary that disagrees
    with them cannot mislead the check."""
    mapping = {**_CONTRACT.to_mapping(), "lane_count": 4, "operator_bitmap": "0xffffffff"}
    path = tmp_path / "shell-build.artifacts.yaml"
    path.write_text(yaml.safe_dump({"metadata": {"capability_contract": json.dumps(mapping)}}), encoding="utf-8")
    contract, _, _ = contract_from_manifest_path(path)
    assert (contract.lane_count, contract.operator_bitmap) == (1, 0x18)


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
    with pytest.raises(ValueError, match="cannot identify a design"):
        advertisement_mismatches(_snapshot(lane_count=0, operator_bitmap=0, host_opcode_bitmap=0), CapabilityContract(name="empty", lanes=()))


def test_a_zero_device_word_means_not_advertised_not_zero() -> None:
    """Fixed binding tops place no NoC lane block, so they advertise
    LANE_COUNT 0 while their manifest records 1. dau's build_tasks states
    the merge rule: let the manifest win over a 0 word, never the reverse.
    Treating it as a mismatch failed a correctly programmed board, and
    because the verify runs before the deadman disarm, that rebooted the
    host."""
    assert advertisement_mismatches(_snapshot(lane_count=0), _CONTRACT) == ()


def test_a_dead_board_is_caught_by_the_required_identity_not_by_the_words() -> None:
    """The consequence of the rule above: a board answering nothing at all
    now looks the same as a top that advertises nothing, so the WORDS can no
    longer catch it. The build identity is what does -- which is why the
    flash path requires one."""
    from dau_driver.verify_advertisement import build_id_mismatch

    dead = _snapshot(lane_count=0, operator_bitmap=0, host_opcode_bitmap=0, sort_capacity=0, build_id=0)
    assert advertisement_mismatches(dead, _CONTRACT) == ()  # words are blind to it
    assert build_id_mismatch(dead, _SPEC_HASH, _PLATFORM, required=True) is not None  # identity is not
    assert build_id_mismatch(dead, _SPEC_HASH, _PLATFORM) is None  # and only when required


def test_the_command_reports_a_contract_it_cannot_parse_in_one_line(tmp_path: Path, capsys) -> None:
    """A malformed contract fails the verification with its reason instead
    of a traceback; the step runs before the deadman disarm."""
    from dau_driver.verify_advertisement import main

    for mapping in ({**_CONTRACT.to_mapping(), "schema": 99}, {"contract": "dau-capability-contract", "schema": 1}):
        path = tmp_path / "shell-build.artifacts.yaml"
        path.write_text(yaml.safe_dump({"metadata": {"capability_contract": json.dumps(mapping)}}), encoding="utf-8")
        assert main(["--manifest", str(path), "--device-root", str(tmp_path)]) == 1
        assert capsys.readouterr().err.startswith("cannot verify: ")


@pytest.mark.posix_only
def test_device_name_for_endpoint_resolves_through_sysfs(tmp_path) -> None:
    from dau_driver.verify_advertisement import device_name_for_endpoint

    xdma = tmp_path / "0000:04:00.0" / "xdma"
    xdma.mkdir(parents=True)
    for node in ("xdma0_user", "xdma0_h2c_0", "xdma0_c2h_0"):
        (xdma / node).touch()
    assert device_name_for_endpoint("0000:04:00.0", sysfs_root=tmp_path) == "xdma0"


def test_an_endpoint_that_did_not_re_enumerate_refuses(tmp_path) -> None:
    from dau_driver.verify_advertisement import device_name_for_endpoint

    with pytest.raises(ValueError, match="owns no xdma devnodes"):
        device_name_for_endpoint("0000:04:00.0", sysfs_root=tmp_path)


_SPEC_HASH = "5:cbbe0c39ca6857cf1854804711d7abac7c301b620d4a478185ca0686dedd5575"
_PLATFORM = "dpv1"


def _stamped(build_id: int) -> RegisterBlockSnapshot:
    return _snapshot(build_id=build_id)


def test_a_matching_build_id_is_the_exact_check() -> None:
    from dau_core.composition import build_id

    from dau_driver.verify_advertisement import build_id_mismatch

    assert build_id_mismatch(_stamped(build_id(_SPEC_HASH, _PLATFORM)), _SPEC_HASH, _PLATFORM) is None


def test_build_id_catches_what_the_capability_words_cannot() -> None:
    """The incident's own pair: q19-scan and q19-packed-wide advertise
    identical lane count, operator bitmap, host-opcode bitmap and sort
    capacity, so a word-level check passes when the board is running the
    other one. BUILD_ID does not."""
    from dau_driver.verify_advertisement import build_id_mismatch

    other_design = _stamped(0xDEADBEEFCAFEF00D)
    assert advertisement_mismatches(other_design, _CONTRACT) == ()  # words agree
    mismatch = build_id_mismatch(other_design, _SPEC_HASH, _PLATFORM)  # identity does not
    assert mismatch is not None and "build_id" in mismatch


def test_a_board_built_under_a_superseded_palette_still_verifies(tmp_path: Path) -> None:
    """Verification is registry-INDEPENDENT, and it has to stay that way.

    A design flashed before a core rename records a manifest naming cores
    the palette no longer carries, under a superseded canonical-form
    version. The design cache refuses such an entry — it cannot re-express
    that spec as one it could build today — but this check asks a different
    question: is the board running the design THIS manifest describes. Both
    sides of that comparison are recorded facts, so the answer does not
    depend on which names the registry happens to carry now, and a resident
    board must not become unidentifiable because the vocabulary moved on.

    The manifest is a build's immutable record. Reading it here needs no
    palette and must never acquire one.
    """
    from dau_driver.verify_advertisement import build_id_mismatch

    pre_rename = CapabilityContract(name="hash-grouped-scan", lanes=(_lane("int32-field-sum-aggregation"),))
    contract, spec_hash, platform = contract_from_manifest_path(_manifest(tmp_path, pre_rename))
    assert contract.lanes[0].sink.core == "int32-field-sum-aggregation"  # read back verbatim
    assert advertisement_mismatches(_stamped(0x1234), contract) == ()
    assert spec_hash is None and platform is None  # this fixture records neither; the words are all there is

    from dau_core.composition import build_id

    assert build_id_mismatch(_stamped(build_id(_SPEC_HASH, _PLATFORM)), _SPEC_HASH, _PLATFORM) is None


def test_an_unstamped_shell_does_not_fail_the_exact_check() -> None:
    """Every shell built before stamping existed advertises 0. That is a
    weaker verification, not a failing one -- main() says so on stderr."""
    from dau_driver.verify_advertisement import build_id_mismatch

    assert build_id_mismatch(_stamped(0), _SPEC_HASH, _PLATFORM) is None


def test_a_manifest_without_a_spec_hash_does_not_fail_the_exact_check() -> None:
    from dau_driver.verify_advertisement import build_id_mismatch

    assert build_id_mismatch(_stamped(0x1234), None, _PLATFORM) is None


def test_the_same_spec_built_for_another_board_is_a_mismatch() -> None:
    """A dpv2 image of the manifest's spec is not the dpv1 image the
    manifest describes."""
    from dau_core.composition import build_id

    from dau_driver.verify_advertisement import build_id_mismatch

    mismatch = build_id_mismatch(_stamped(build_id(_SPEC_HASH, "dpv2")), _SPEC_HASH, "dpv1")
    assert mismatch is not None and "on dpv1" in mismatch


def test_a_manifest_without_a_platform_does_not_fail_the_exact_check() -> None:
    from dau_driver.verify_advertisement import build_id_mismatch

    assert build_id_mismatch(_stamped(0x1234), _SPEC_HASH, None) is None


def test_the_snapshot_actually_reads_build_id_back_from_the_registers() -> None:
    """Without this the exact check is decorative: `build_id` would sit at
    its dataclass default of 0 forever, every verification would silently
    fall back to the capability words, and the fallback note would be the
    only hint. The word ORDER matters as much -- low at 0x0014, high at
    0x0018, per the identity block -- because a swap makes the check fail on
    a correctly flashed board."""
    from dau_core.registers import RegisterOffset

    from dau_driver.capability_decode import read_register_block_snapshot

    stamped = 0xCBBE0C39CA6857CF
    words = {
        RegisterOffset.REGISTER_MAP_VERSION: 0x0002,
        RegisterOffset.BUILD_ID_LOW: stamped & 0xFFFFFFFF,
        RegisterOffset.BUILD_ID_HIGH: stamped >> 32,
    }
    snapshot = read_register_block_snapshot(lambda offset: words.get(offset, 0))
    assert snapshot.build_id == stamped


def test_a_dead_aperture_reads_back_as_unstamped_not_as_an_identity() -> None:
    from dau_driver.capability_decode import read_register_block_snapshot

    snapshot = read_register_block_snapshot(lambda offset: 0xFFFFFFFF)
    assert snapshot.build_id == 0


def test_the_command_reports_a_missing_or_malformed_manifest_in_one_line(tmp_path: Path, capsys) -> None:
    from dau_driver.verify_advertisement import main

    assert main(["--manifest", str(tmp_path / "absent.yaml"), "--device-root", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("cannot verify: ")
    (tmp_path / "bare.yaml").write_text("name: nothing\n")
    assert main(["--manifest", str(tmp_path / "bare.yaml"), "--device-root", str(tmp_path)]) == 1
    assert "carries no capability_contract" in capsys.readouterr().err
    (tmp_path / "list.yaml").write_text("- not\n- a mapping\n")
    assert main(["--manifest", str(tmp_path / "list.yaml"), "--device-root", str(tmp_path)]) == 1
    assert "not a manifest mapping" in capsys.readouterr().err
