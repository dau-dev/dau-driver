"""Board geometry does not live in this package.

The rule the docs state repeatedly: hardware access is board/host
configuration, never code defaults. A module constant naming an address, a
region size, or a device's capacity is a second source of truth for
something the platform already declares, and the two drift the first time a
board differs — which is exactly how a host ended up assuming four lanes on
a shell that advertises its own count, and how one board's DDR layout ended
up applied to another behind a `memory_bytes > 1 << 32` conditional.

Wire and record shapes are NOT board geometry and stay: how many bytes a
bar record occupies is a property of the format both sides compile against,
not of the board it runs on. The two are easy to confuse, so this pins the
distinction explicitly rather than leaving it to a reviewer's judgement.

Asserted EXACTLY and in both directions: remove a constant without
tightening this list and the test fails; add one without justifying it here
and the test fails. The list is expected to shrink.
"""

from __future__ import annotations

import re
from pathlib import Path

import dau_driver

# Every module-level constant in the device-execution modules, and why it is
# allowed to be a constant. A name here is a claim that it describes a WIRE
# or RECORD shape -- fixed by the format, identical on every board.
WIRE_SHAPE_CONSTANTS = {
    "DMA_ALIGN_BYTES": "the DMA engine's alignment, a property of the transfer format",
    "GROUP_RECORD_BYTES": "bytes per grouped-aggregation record on the wire",
    "BAR_ROW_BYTES": "bytes per bar input row on the wire",
    "BAR_RECORD_BYTES": "bytes per bar output record on the wire",
}

# Constants that DO describe board or shell geometry and have not moved to
# configuration yet. Each is a place a second board would be priced or
# batched against this one's numbers. Expected to empty.
UNMIGRATED_GEOMETRY_CONSTANTS = {
    "MAX_ROWS_PER_JOB": "input BRAM depth of the stream job shell",
    "MAX_GROUPS_PER_JOB": "output BRAM depth of the stream job shell",
    "BAR_MAX_ROWS_PER_JOB": "input BRAM depth of the fused bar shell",
    "BAR_MAX_RECORDS_PER_JOB": "output BRAM depth of the fused bar shell",
}

_MODULES = ("pipeline_execution",)


def _module_constants(module_name: str) -> set[str]:
    source = (Path(dau_driver.__path__[0]) / f"{module_name}.py").read_text(encoding="utf-8")
    return {match.group(1) for match in re.finditer(r"^([A-Z][A-Z0-9_]*) = ", source, re.M)}


def test_every_module_constant_is_a_wire_shape_or_a_declared_holdout() -> None:
    found: set[str] = set()
    for module_name in _MODULES:
        found |= _module_constants(module_name)

    accounted = set(WIRE_SHAPE_CONSTANTS) | set(UNMIGRATED_GEOMETRY_CONSTANTS)

    unexplained = found - accounted
    assert not unexplained, (
        f"new module constants in {_MODULES}: {sorted(unexplained)}. "
        "If one describes a wire/record shape, add it to WIRE_SHAPE_CONSTANTS with its reason. "
        "If it describes a board or shell -- an address, a region size, a capacity, a lane count -- "
        "it belongs on the platform, because a constant here is a second source of truth for a fact the board states"
    )

    stale = accounted - found
    assert not stale, (
        f"these constants no longer exist: {sorted(stale)}. Remove them from the list above -- "
        "a holdout list that outlives its holdouts stops measuring anything"
    )


def test_no_address_or_capacity_constant_survives_unlisted() -> None:
    """The specific shape that caused the incidents: a hex literal bound to
    a module-level name. Addresses and region sizes read this way; wire
    shapes are small decimals."""
    for module_name in _MODULES:
        source = (Path(dau_driver.__path__[0]) / f"{module_name}.py").read_text(encoding="utf-8")
        hex_constants = {match.group(1) for match in re.finditer(r"^([A-Z][A-Z0-9_]*) = 0x[0-9A-Fa-f_]+\s*$", source, re.M)}
        assert not hex_constants, (
            f"{module_name} binds hex literals to module names: {sorted(hex_constants)}. "
            "An address or region size is board configuration and belongs on the platform"
        )
