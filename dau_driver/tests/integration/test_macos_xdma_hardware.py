from __future__ import annotations

import os
import sys

import pytest
from dau_core.registers import DAU_MAGIC_WORD, RegisterOffset

from dau_driver.macos_xdma import discover_devices

pytestmark = pytest.mark.skipif(
    os.environ.get("DAU_XDMA_MACOS") != "1" or sys.platform != "darwin",
    reason="set DAU_XDMA_MACOS=1 on a macOS host with the XDMA DriverKit extension loaded to run hardware integration tests",
)


def test_macos_xdma_hardware_discovers_dau_identity() -> None:
    devices = discover_devices()

    assert devices, "expected at least one DAU XDMA DriverKit device"
    device = devices[0]
    assert device.backend == "macos-xdma"
    assert device.read_register(RegisterOffset.MAGIC) == DAU_MAGIC_WORD
    assert device.read_register_block_snapshot().platform_id
