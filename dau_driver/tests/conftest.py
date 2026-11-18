from __future__ import annotations

import sys
from pathlib import Path

import pytest

SLOP_ROOT = Path(__file__).resolve().parents[3]
DAU_CORE_SOURCE = SLOP_ROOT / "dau-core"

if DAU_CORE_SOURCE.exists():
    sys.path.insert(0, str(DAU_CORE_SOURCE))


POSIX_ONLY_REASON = "device I/O is os.pread/os.pwrite over Linux devnodes and sysfs; dau-driver has no Windows backend"


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", f"posix_only: needs the POSIX device path ({POSIX_ONLY_REASON})")


def pytest_collection_modifyitems(config, items) -> None:
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(reason=POSIX_ONLY_REASON)
    for item in items:
        if item.get_closest_marker("posix_only") is not None:
            item.add_marker(skip)
