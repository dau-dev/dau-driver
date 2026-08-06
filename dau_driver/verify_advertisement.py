"""Check that a device is running the design a build manifest describes.

A programming ladder that only checks the endpoint reappeared cannot tell a
successful reprogram from a failed one. The FPGA falls back to its
flash-resident design when a volatile program does not take, so the endpoint
comes back either way, with the same vendor and device id, at the same
address. On 2026-08-06 a dpv1 SRAM program failed silently for exactly this
reason: the programmer exited zero without writing, the plan saw an
endpoint, disarmed its deadman, and reported success while the board kept
running the previous design.

What distinguishes them is the COMPOSED ADVERTISEMENT — the lane count,
operator bitmap, host-opcode bitmap and sort capacity the shell publishes in
its identity registers. A build manifest carries the same four values in its
capability contract. Comparing them answers the question the endpoint check
cannot: is the silicon running THIS build?

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

__all__ = ("ADVERTISED_WORDS", "advertisement_mismatches", "contract_from_manifest_path", "main")

# the identity-register words a shell advertises, mapped to the key the
# capability contract records them under. Both sides must agree on all of
# them for the device to be running the manifest's design.
ADVERTISED_WORDS: tuple[tuple[str, str], ...] = (
    ("lane_count", "lane_count"),
    ("operator_bitmap", "operator_bitmap"),
    ("host_opcode_bitmap", "host_opcode_bitmap"),
    ("sort_capacity", "sort_capacity"),
)


def _as_int(value: Any) -> int | None:
    """Contract words are serialized as ints or as hex strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    return None


def contract_from_manifest_path(path: Path) -> Mapping[str, Any]:
    """The capability contract a shell-build manifest carries, as plain data.

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
    return json.loads(serialized)


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
    if not any(value for value in recorded.values()):
        raise ValueError(
            "capability contract advertises no non-zero word "
            f"({', '.join(f'{key}={value!r}' for key, value in recorded.items())}); "
            "it cannot identify a design, and a dead register aperture reads back as all zeros"
        )
    mismatches = []
    for register_field, contract_key in ADVERTISED_WORDS:
        expected = _as_int(contract.get(contract_key))
        if expected is None:
            continue
        actual = getattr(snapshot, register_field, None)
        if actual is None:
            mismatches.append(f"{register_field}: device does not advertise it, manifest expects {expected}")
        elif int(actual) != expected:
            mismatches.append(f"{register_field}: device advertises {actual} (0x{int(actual):x}), manifest expects {expected} (0x{expected:x})")
    return tuple(mismatches)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a device is running the design a build manifest describes")
    parser.add_argument("--manifest", required=True, type=Path, help="shell-build artifacts manifest for the programmed design")
    parser.add_argument("--device-root", default=None, help="devnode search root passed to discovery")
    args = parser.parse_args(argv)

    from dau_driver.discovery import discover_devices

    contract = contract_from_manifest_path(args.manifest)
    devices = discover_devices() if args.device_root is None else discover_devices(args.device_root)
    if not devices:
        print("no DAU device found to verify", file=sys.stderr)
        return 1
    device = devices[0]
    snapshot = device.read_register_block_snapshot()

    mismatches = advertisement_mismatches(snapshot, contract)
    if mismatches:
        print(f"device is NOT running {args.manifest}:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        print("the endpoint enumerating does not mean the program took: an FPGA falls back to its flash-resident design", file=sys.stderr)
        return 1
    checked = ", ".join(field for field, key in ADVERTISED_WORDS if _as_int(contract.get(key)) is not None)
    print(f"device advertisement matches {args.manifest} ({checked})")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
