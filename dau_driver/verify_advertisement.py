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

BUILD_ID is exact. A shell stamps the build id of its composition's spec on
its platform (``dau_core.composition.build_id``) into its identity block, the
manifest records the spec hash and the platform, and they either agree or the
board is running something else.

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
import sys
from collections.abc import Sequence
from pathlib import Path

from dau_core.capability_contract import CapabilityContract
from dau_core.registers import RegisterBlockSnapshot

__all__ = ("advertised_words", "advertisement_mismatches", "build_id_mismatch", "contract_from_manifest_path", "device_name_for_endpoint", "main")


def advertised_words(source: RegisterBlockSnapshot | CapabilityContract) -> dict[str, int]:
    """The identity-register words a shell advertises, by the name the
    register snapshot and the capability contract both use. The two sides
    must agree on all of them for the device to be running the manifest's
    design."""
    return {
        "lane_count": source.lane_count,
        "operator_bitmap": source.operator_bitmap,
        "host_opcode_bitmap": source.host_opcode_bitmap,
        "sort_capacity": source.sort_capacity,
    }


def contract_from_manifest_path(path: Path) -> tuple[CapabilityContract, str | None, str | None]:
    """The capability contract a shell-build manifest records, with the
    spec hash and platform beside it, which together make the build id the
    shell stamps.

    Reads the manifest as YAML rather than importing the build package:
    this runs on a bench host where only the driver may be installed.
    """
    import yaml

    from .capability_decode import contract_from_manifest_metadata

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise TypeError(f"{path} is not a manifest mapping")
    metadata = document.get("metadata") or {}
    contract = contract_from_manifest_metadata(metadata)
    if contract is None:
        raise ValueError(f"{path} carries no capability_contract; nothing to verify against")
    return contract, metadata.get("spec_hash"), metadata.get("platform")


def build_id_mismatch(snapshot: RegisterBlockSnapshot, spec_hash: str | None, platform: str | None, *, required: bool = False) -> str | None:
    """The EXACT check, when both sides carry it: does the shell's BUILD_ID
    match the build id of the design the manifest describes, its spec on the
    platform it was built for?

    This is what the advertised capability words cannot do. They are a
    coarse projection — most designs share a word tuple with several others,
    and q19-scan and q19-packed-wide are identical across all four — so a
    word-level match is consistent with the board running a different
    design. A BUILD_ID match is not.

    Returns None when the check does not apply: no spec hash or platform in
    the manifest, or a shell that advertises BUILD_ID 0 (unstamped, which
    every shell built before stamping existed does). Absence is reported by the
    caller rather than treated as agreement — an unstamped shell is a weaker
    verification, not a passing one.
    """
    if not spec_hash or not platform:
        return None
    from dau_core.composition import build_id

    advertised = snapshot.build_id
    if not advertised:  # 0: not stamped
        if required:
            # The caller knows what it just programmed, and everything built
            # since stamping exists carries an identity. An unstamped read
            # therefore means the program did not take, the board is not
            # answering, or it is running something older -- none of which
            # should pass. All-zero is also what a dead aperture reads.
            return "build_id: device advertises none, but the manifest's design stamps one; the program did not take or the board is not answering"
        return None
    expected = build_id(spec_hash, platform)
    if advertised != expected:
        return f"build_id: device advertises 0x{advertised:016x}, the manifest's spec on {platform} gives 0x{expected:016x}"
    return None


def advertisement_mismatches(snapshot: RegisterBlockSnapshot, contract: CapabilityContract) -> tuple[str, ...]:
    """Every advertised word where the device and the contract disagree.

    Raises when the contract advertises no NON-ZERO word. All-zero is what a
    dead or undecoded register aperture reads back as — the driver
    normalizes all-ones to zero precisely so a dead word advertises nothing
    — so a contract of all zeros would match a board that is not answering
    at all. Such a contract cannot identify a design, and saying so is the
    only safe answer; silently passing is the failure this module exists to
    prevent.
    """
    expected = advertised_words(contract)
    advertised = advertised_words(snapshot)
    if not any(expected.values()):
        raise ValueError(
            "capability contract advertises no non-zero word "
            f"({', '.join(f'{word}={value}' for word, value in expected.items())}); "
            "it cannot identify a design, and a dead register aperture reads back as all zeros"
        )
    mismatches = []
    for word, value in expected.items():
        actual = advertised[word]
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
        if actual != value:
            mismatches.append(f"{word}: device advertises {actual} (0x{actual:x}), manifest expects {value} (0x{value:x})")
    return tuple(mismatches)


def device_name_for_endpoint(bdf: str, *, sysfs_root: Path | None = None) -> str:
    """The xdma instance an endpoint owns, e.g. ``0000:04:00.0`` ->
    ``xdma0``. The devnodes hang off the PCI device in sysfs, which is the
    only link between the BDF a programming plan knows and the device the
    driver enumerates."""
    from .linux_xdma import DEFAULT_SYSFS_PCI_ROOT

    xdma_root = (DEFAULT_SYSFS_PCI_ROOT if sysfs_root is None else sysfs_root) / bdf / "xdma"
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

    from dau_driver.errors import DauDriverError

    try:
        return _verify(args)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError, DauDriverError) as error:
        # the expected failures: no manifest, a malformed one, a device that
        # cannot be read -- one line each, not a traceback
        print(f"cannot verify: {error}", file=sys.stderr)
        return 1


def _verify(args: argparse.Namespace) -> int:
    from dau_driver.discovery import discover_devices

    contract, spec_hash, platform = contract_from_manifest_path(args.manifest)
    # register reads only, so no memory bound is needed
    devices = discover_devices(memory_bytes=None) if args.device_root is None else discover_devices(args.device_root, memory_bytes=None)
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
    exact = build_id_mismatch(snapshot, spec_hash, platform, required=args.require_build_id)
    if exact is not None:
        mismatches.append(exact)
    mismatches = tuple(mismatches)
    if mismatches:
        print(f"device is NOT running {args.manifest}:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        print("the endpoint enumerating does not mean the program took: an FPGA falls back to its flash-resident design", file=sys.stderr)
        return 1
    checked = list(advertised_words(contract))
    stamped = bool(spec_hash) and bool(platform) and bool(snapshot.build_id)
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
