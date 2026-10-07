"""Check that a device is running the design a build manifest describes.

A programming ladder that only checks the endpoint reappeared cannot tell a
successful reprogram from a failed one. The FPGA falls back to its
flash-resident design when a volatile program does not take, so the endpoint
comes back either way, with the same vendor and device id, at the same
address. On 2026-08-06 a dpv1 SRAM program failed silently for exactly this
reason: the programmer exited zero without writing, the plan saw an
endpoint, disarmed its deadman, and reported success while the board kept
running the previous design.

Two things distinguish them, and they are not equally strong.

BUILD_ID is exact. A shell stamps the leading 64 bits of its composition's
spec hash into its identity block, the manifest carries the same hash, and
they either agree or the board is running something else.

The composed capability words — lane count, operator bitmap, host-opcode
bitmap, sort capacity — are a coarse projection and must not be mistaken
for an identity. Most designs share a word tuple with several others:
q19-scan and q19-packed-wide are identical across all four, which is the
incident's own pair. They are still worth comparing, because a shell built
before stamping existed advertises BUILD_ID 0 and the words are then all
there is — but a pass on words alone is reported as the weaker result it is.

This is a verification, not a decode: it deliberately reports every
mismatch rather than the first, because "lane count differs" and "operator
bitmap differs" have different causes and a caller staring at a failed flash
wants both.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = ("ADVERTISED_WORDS", "advertisement_mismatches", "build_id_mismatch", "contract_from_manifest_path", "device_name_for_endpoint", "main")

# the identity-register words a shell advertises, mapped to the key the
# capability contract records them under. Both sides must agree on all of
# them for the device to be running the manifest's design.
ADVERTISED_WORDS: tuple[tuple[str, str], ...] = (
    ("lane_count", "lane_count"),
    ("operator_bitmap", "operator_bitmap"),
    ("host_opcode_bitmap", "host_opcode_bitmap"),
    ("sort_capacity", "sort_capacity"),
)


# a word the contract does not record at all, versus one it records in a
# form this cannot read. They must not be conflated: the first is fine to
# skip, the second is a mismatch. Reading an unparseable word as "absent"
# is how a check silently stops checking.
_ABSENT = object()
_UNREADABLE = object()


def _as_int(value: Any) -> Any:
    """A contract word as an int, or ``_ABSENT`` / ``_UNREADABLE``.

    Words are serialized as ints or as strings that ``int(base=0)`` accepts.
    Anything else — a float, a list, an empty string, a leading-zero decimal
    — is UNREADABLE rather than absent, and never raises: this runs inside a
    plan step that sits before the deadman disarm, so an uncaught exception
    here reboots the host.
    """
    if value is None:
        return _ABSENT
    if isinstance(value, bool):
        # a JSON true is not a capability word, and int(True) == 1 would
        # quietly satisfy a device advertising one lane
        return _UNREADABLE
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            return _UNREADABLE
    return _UNREADABLE


def contract_from_manifest_path(path: Path, *, with_spec_hash: bool = False) -> Any:
    """The capability contract a shell-build manifest carries, as plain data,
    and optionally the spec hash beside it.

    Reads the manifest as text and pulls the embedded JSON rather than
    importing the build package: this runs on a bench host where only the
    driver may be installed.
    """
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    metadata = document.get("metadata") or {}
    serialized = metadata.get("capability_contract")
    if not serialized:
        raise ValueError(f"{path} carries no capability_contract; nothing to verify against")
    contract = json.loads(serialized)
    if with_spec_hash:
        return contract, metadata.get("spec_hash")
    return contract


def build_id_mismatch(snapshot, spec_hash: str | None, *, required: bool = False) -> str | None:
    """The EXACT check, when both sides carry it: does the shell's BUILD_ID
    match the spec hash of the design the manifest describes?

    This is what the advertised capability words cannot do. They are a
    coarse projection — most designs share a word tuple with several others,
    and q19-scan and q19-packed-wide are identical across all four — so a
    word-level match is consistent with the board running a different
    design. A BUILD_ID match is not.

    Returns None when the check does not apply: no spec hash in the
    manifest, or a shell that advertises BUILD_ID 0 (unstamped, which every
    shell built before stamping existed does). Absence is reported by the
    caller rather than treated as agreement — an unstamped shell is a weaker
    verification, not a passing one.
    """
    if not spec_hash:
        return None
    from dau_core.composition import build_id_from_spec_hash

    advertised = getattr(snapshot, "build_id", None)
    if not advertised:  # 0 or absent: not stamped
        if required:
            # The caller knows what it just programmed, and everything built
            # since stamping exists carries an identity. An unstamped read
            # therefore means the program did not take, the board is not
            # answering, or it is running something older -- none of which
            # should pass. All-zero is also what a dead aperture reads.
            return "build_id: device advertises none, but the manifest's design stamps one; the program did not take or the board is not answering"
        return None
    expected = build_id_from_spec_hash(spec_hash)
    if advertised != expected:
        return f"build_id: device advertises 0x{advertised:016x}, manifest's spec hash gives 0x{expected:016x}"
    return None


def advertisement_mismatches(snapshot, contract: Mapping[str, Any]) -> tuple[str, ...]:
    """Every advertised word where the device and the contract disagree.

    A word the contract does not record is SKIPPED rather than treated as
    zero: an older manifest that predates a word should not fail a device
    that advertises it. A word the contract records and the device does not
    match is a mismatch, which is the case that matters.

    Raises when the contract records no NON-ZERO word. All-zero is what a
    dead or undecoded register aperture reads back as — the driver
    normalizes all-ones to zero precisely so a dead word advertises nothing
    — so a contract of all zeros would match a board that is not answering
    at all. Such a contract cannot identify a design, and saying so is the
    only safe answer; silently passing is the failure this module exists to
    prevent.
    """
    recorded = {key: _as_int(contract.get(key)) for _, key in ADVERTISED_WORDS}
    # an unreadable word is reported as a mismatch below; it must not first
    # be swallowed by the all-zero guard, which would raise instead and lose
    # the specific reason
    if not any(value is _UNREADABLE for value in recorded.values()) and not any(value for value in recorded.values() if isinstance(value, int)):
        raise ValueError(
            "capability contract advertises no non-zero word "
            f"({', '.join(f'{key}={contract.get(key)!r}' for key in recorded)}); "
            "it cannot identify a design, and a dead register aperture reads back as all zeros"
        )
    mismatches = []
    for register_field, contract_key in ADVERTISED_WORDS:
        expected = recorded[contract_key]
        if expected is _ABSENT:
            continue
        if expected is _UNREADABLE:
            mismatches.append(f"{register_field}: manifest records {contract.get(contract_key)!r}, which is not a capability word")
            continue
        actual = getattr(snapshot, register_field, None)
        if not actual:
            # A device word of 0 means NOT ADVERTISED, not "zero". Fixed
            # binding tops place no NoC lane block and leave LANE_COUNT at
            # 0 while their manifest records 1, and dau's build_tasks states
            # the merge rule outright: let the manifest win over a 0 word,
            # never the reverse. Treating this as a mismatch failed a
            # correctly programmed board, and because the verify step runs
            # before the deadman disarm, that rebooted the host.
            #
            # This is safe ONLY because a board that answers nothing at all
            # is caught by the build identity instead -- see
            # build_id_mismatch(required=True), which the flash path uses.
            continue
        if int(actual) != expected:
            mismatches.append(f"{register_field}: device advertises {actual} (0x{int(actual):x}), manifest expects {expected} (0x{expected:x})")
    return tuple(mismatches)


def device_name_for_endpoint(bdf: str, *, sysfs_root: Path = Path("/sys/bus/pci/devices")) -> str:
    """The xdma instance an endpoint owns, e.g. ``0000:04:00.0`` ->
    ``xdma0``. The devnodes hang off the PCI device in sysfs, which is the
    only link between the BDF a programming plan knows and the device the
    driver enumerates."""
    xdma_root = sysfs_root / bdf / "xdma"
    if not xdma_root.is_dir():
        raise ValueError(f"{bdf} owns no xdma devnodes ({xdma_root} is not a directory); it may not have re-enumerated")
    names = {entry.name.split("_", 1)[0] for entry in xdma_root.iterdir() if "_" in entry.name}
    if len(names) != 1:
        raise ValueError(f"expected exactly one xdma instance under {xdma_root}, found {sorted(names) or 'none'}")
    return names.pop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a device is running the design a build manifest describes")
    parser.add_argument("--manifest", required=True, type=Path, help="shell-build artifacts manifest for the programmed design")
    parser.add_argument("--device-root", default=None, help="devnode search root passed to discovery")
    parser.add_argument("--endpoint-bdf", default=None, help="verify the device this PCI endpoint owns, e.g. 0000:04:00.0")
    parser.add_argument(
        "--require-build-id",
        action="store_true",
        help="fail when the device advertises no build identity (use after programming a design that stamps one)",
    )
    args = parser.parse_args(argv)

    import yaml

    from dau_driver.discovery import discover_devices
    from dau_driver.errors import DauDriverError

    try:
        contract, spec_hash = contract_from_manifest_path(args.manifest, with_spec_hash=True)
        devices = discover_devices() if args.device_root is None else discover_devices(args.device_root)
    except (OSError, ValueError, yaml.YAMLError, DauDriverError) as error:
        print(f"cannot verify: {error}", file=sys.stderr)
        return 1
    if not devices:
        print("no DAU device found to verify", file=sys.stderr)
        return 1

    if args.endpoint_bdf is not None:
        wanted = device_name_for_endpoint(args.endpoint_bdf)
        matched = [device for device in devices if device.name == wanted]
        if not matched:
            print(
                f"endpoint {args.endpoint_bdf} owns {wanted}, which is not among the discovered devices {[d.name for d in devices]}", file=sys.stderr
            )
            return 1
        device = matched[0]
    elif len(devices) > 1:
        # never pick one: verifying the wrong board is the same failure this
        # tool exists to catch, one level up
        print(f"{len(devices)} devices found ({[d.name for d in devices]}); pass --endpoint-bdf to say which one was programmed", file=sys.stderr)
        return 1
    else:
        device = devices[0]
    snapshot = device.read_register_block_snapshot()

    mismatches = list(advertisement_mismatches(snapshot, contract))
    exact = build_id_mismatch(snapshot, spec_hash, required=args.require_build_id)
    if exact is not None:
        mismatches.append(exact)
    mismatches = tuple(mismatches)
    if mismatches:
        print(f"device is NOT running {args.manifest}:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        print("the endpoint enumerating does not mean the program took: an FPGA falls back to its flash-resident design", file=sys.stderr)
        return 1
    checked = [field for field, key in ADVERTISED_WORDS if isinstance(_as_int(contract.get(key)), int)]
    stamped = bool(spec_hash) and bool(getattr(snapshot, "build_id", 0))
    if stamped:
        checked.append("build_id")
    print(f"device advertisement matches {args.manifest} ({', '.join(checked)})")
    if not stamped:
        # say it out loud: the words alone do not identify a design, so this
        # pass is weaker than it looks
        print(
            "note: this shell advertises no BUILD_ID, so the check compared only capability words -- distinct designs can share all of them",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
