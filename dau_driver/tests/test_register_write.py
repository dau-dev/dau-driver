from __future__ import annotations

from pathlib import Path

import pytest
from dau_core.registers import JobControlFlag, RegisterOffset

from dau_driver.errors import RegisterAccessError
from dau_driver.testing import FakeTransport, fake_device


def test_write_register_applies_window_offset(tmp_path: Path) -> None:
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)

    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))

    assert transport.registers[device.register_window_offset + int(RegisterOffset.JOB_CONTROL)] == 1


def test_write_register_round_trips_through_read(tmp_path: Path) -> None:
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)

    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, 960_320)

    assert device.read_register(RegisterOffset.INPUT_LENGTH_LOW) == 960_320


def test_write_register_rejects_out_of_range_values(tmp_path: Path) -> None:
    device = fake_device(tmp_path, FakeTransport())

    with pytest.raises(RegisterAccessError, match="32 bits"):
        device.write_register(RegisterOffset.JOB_CONTROL, 1 << 32)
    with pytest.raises(RegisterAccessError, match="32 bits"):
        device.write_register(RegisterOffset.JOB_CONTROL, -1)


def test_write_address_bounds_against_the_devices_memory_by_default(tmp_path: Path) -> None:
    """The guard must cover the plain FUNCTION, not only the method: the
    scan and pipeline execution paths call `write_address(device, ...)`
    directly, so a bound that is off unless a caller opts in does not
    guard the paths that program job addresses."""
    import dataclasses

    from dau_driver.device import write_address

    transport = FakeTransport()
    device = dataclasses.replace(fake_device(tmp_path, transport), memory_bytes=1 << 20)

    with pytest.raises(RegisterAccessError, match="past the device"):
        write_address(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 1 << 21)

    write_address(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 0x1000)
    assert device.read_register(RegisterOffset.INPUT_ADDRESS_LOW) == 0x1000


def test_write_address_stays_unbounded_for_a_device_declaring_no_memory() -> None:
    """Duck-typed on purpose: devices are not required to subclass
    DauDevice, and one that declares no memory behaves as it did before
    rather than failing."""
    from dau_driver.device import write_address

    class MinimalDevice:
        def __init__(self) -> None:
            self.written: dict[int, int] = {}

        def write_register(self, offset, value) -> None:
            self.written[int(offset)] = value

    device = MinimalDevice()
    write_address(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 1 << 40)
    assert len(device.written) == 2
