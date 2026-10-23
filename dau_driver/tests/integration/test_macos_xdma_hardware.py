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


def _scratch_addresses() -> tuple[int, int]:
    """Two device addresses this test may overwrite. Which memory is free to
    clobber is board geometry, so it comes from the bench, not from here."""
    raw = os.environ.get("DAU_XDMA_SCRATCH_ADDRESSES", "")
    addresses = tuple(int(part, 0) for part in raw.split(",") if part.strip())
    if len(addresses) != 2:
        pytest.skip("set DAU_XDMA_SCRATCH_ADDRESSES=<a>,<b> to two device addresses the test may overwrite")
    return addresses[0], addresses[1]


def test_concurrent_transfers_on_both_engines_round_trip() -> None:
    """The H2C and C2H engines share the dext's one descriptor ring. Two
    threads each write a pattern and read it back, so their transfers
    interleave across both engines; any transfer that ran on a chain the
    other thread rewrote returns the wrong bytes or fails."""
    from concurrent.futures import ThreadPoolExecutor

    device = discover_devices()[0]
    first, second = _scratch_addresses()
    length = 1 << 20

    def round_trips(address: int, seed: int) -> int:
        for iteration in range(64):
            pattern = bytes((seed + iteration + index) & 0xFF for index in range(length))
            device.write_dma(device_address=address, data=pattern)
            assert device.read_dma(device_address=address, byte_count=length) == pattern, f"iteration {iteration} at {address:#x}"
        return address

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(round_trips, (first, second), (0x11, 0x77))) == sorted((first, second))
