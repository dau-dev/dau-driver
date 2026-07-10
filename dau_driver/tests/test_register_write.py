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
