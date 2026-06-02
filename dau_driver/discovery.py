from __future__ import annotations

import sys
from pathlib import Path

from . import linux_xdma
from .device import DauDevice


def discover_devices(root: str | Path = linux_xdma.DEFAULT_DEV_ROOT) -> list[DauDevice]:
    root_path = Path(root)
    linux_devices = linux_xdma.discover_devices(root_path)
    if linux_devices or root_path != linux_xdma.DEFAULT_DEV_ROOT or sys.platform != "darwin":
        return linux_devices
    return _discover_macos_devices()


def identify_device(user_path: str | Path, *, h2c_path: str | Path | None = None, c2h_path: str | Path | None = None) -> DauDevice:
    return linux_xdma.identify_device(user_path, h2c_path=h2c_path, c2h_path=c2h_path)


def _discover_macos_devices() -> list[DauDevice]:
    from . import macos_xdma

    return macos_xdma.discover_devices()
