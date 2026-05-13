from __future__ import annotations

import os
from pathlib import Path

import pytest
from dau_core.registers import DAU_MAGIC_WORD, RegisterOffset

from dau_driver import discover_devices

pytestmark = pytest.mark.skipif(
    os.environ.get("DAU_XDMA_HARDWARE") != "1",
    reason="set DAU_XDMA_HARDWARE=1 on a Linux XDMA host to run hardware integration tests",
)


def test_linux_xdma_hardware_discovers_dau_identity() -> None:
    devices = discover_devices(Path(os.environ.get("DAU_XDMA_DEV_ROOT", "/dev")))

    assert devices, "expected at least one DAU XDMA device"
    device = devices[0]
    assert device.read_register(RegisterOffset.MAGIC) == DAU_MAGIC_WORD
    assert device.read_register_block_snapshot().platform_id
