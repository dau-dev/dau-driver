from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest import mock

import pytest
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset

from dau_driver import scan_execution
from dau_driver.fleet import FleetScanError, run_scan_batches_fleet
from dau_driver.scan_execution import ScanBatch, run_scan_batches
from dau_driver.testing import FakeTransport, fake_device

REGIONS = (0, 0x0800_0000)
ROW_BYTES = 64


class TimedTransport(FakeTransport):
    def __init__(self, delay: float = 0.0, *, fail_write: bool = False) -> None:
        super().__init__()
        self.delay = delay
        self.fail_write = fail_write

    def write_dma(self, device_address: int, data: bytes | bytearray | memoryview) -> int:
        if self.fail_write:
            raise OSError(f"failed payload {bytes(data)[0]}")
        return super().write_dma(device_address, data)

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset == int(RegisterOffset.JOB_CONTROL) and value & int(JobControlFlag.START):
            time.sleep(self.delay)

    def read_u32(self, offset: int) -> int:
        if offset == int(RegisterOffset.JOB_STATUS):
            return int(JobStatusFlag.DONE)
        return super().read_u32(offset)


class BlockingTransport(TimedTransport):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset == int(RegisterOffset.JOB_CONTROL) and value & int(JobControlFlag.START):
            self.started.set()

    def read_u32(self, offset: int) -> int:
        if offset == int(RegisterOffset.JOB_STATUS):
            return int(JobStatusFlag.DONE) if self.release.is_set() else 0
        return super().read_u32(offset)


def _device(tmp_path, name: str, transport: FakeTransport):
    return replace(fake_device(tmp_path, transport), name=name)


def _batches(count: int):
    for index in range(count):
        yield ScanBatch(payload=bytes([index]) * ROW_BYTES, row_count=1)


def _collect(device):
    address = device.read_register(RegisterOffset.INPUT_ADDRESS_LOW)
    payloads = dict(device.resolved_transport.h2c_writes)
    return payloads[address][0]


def test_fleet_preserves_order_and_fast_device_takes_more_batches(tmp_path):
    slow = _device(tmp_path, "slow", TimedTransport(0.02))
    fast = _device(tmp_path, "fast", TimedTransport(0.001))

    results = run_scan_batches_fleet([slow, fast], _batches(12), input_regions=REGIONS, row_bytes=ROW_BYTES, collect=_collect)

    assert [result.batch_index for result in results] == list(range(12))
    assert [result.value for result in results] == list(range(12))
    assert sum(result.device is fast for result in results) > sum(result.device is slow for result in results)


def test_single_device_matches_scan_runner(tmp_path):
    direct_device = _device(tmp_path, "direct", TimedTransport())
    fleet_device = _device(tmp_path, "fleet", TimedTransport())

    direct = run_scan_batches(direct_device, _batches(4), input_regions=REGIONS, row_bytes=ROW_BYTES, collect=_collect)
    fleet = run_scan_batches_fleet([fleet_device], _batches(4), input_regions=REGIONS, row_bytes=ROW_BYTES, collect=_collect)

    assert [result.value for result in fleet] == direct
    assert [result.batch_index for result in fleet] == list(range(4))
    assert all(result.device is fleet_device for result in fleet)
    assert fleet_device.resolved_transport.h2c_writes == direct_device.resolved_transport.h2c_writes


def test_fleet_consumes_batches_only_as_device_pipelines_demand_them(tmp_path):
    releases = [threading.Event(), threading.Event()]
    starts = [threading.Event(), threading.Event()]
    devices = [_device(tmp_path, f"card-{index}", BlockingTransport(starts[index], releases[index])) for index in range(2)]
    pulled = 0
    pulled_lock = threading.Lock()

    def lazy_batches():
        nonlocal pulled
        for batch in _batches(20):
            with pulled_lock:
                pulled += 1
            yield batch

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_scan_batches_fleet,
            devices,
            lazy_batches(),
            input_regions=REGIONS,
            row_bytes=ROW_BYTES,
            collect=_collect,
            timeout_seconds=5.0,
        )
        assert all(start.wait(2.0) for start in starts)
        with pulled_lock:
            assert pulled <= 4
        for release in releases:
            release.set()
        assert len(future.result(timeout=5.0)) == 20


def test_fleet_failure_includes_global_batch_and_device(tmp_path):
    failed = _device(tmp_path, "broken", TimedTransport(fail_write=True))
    healthy = _device(tmp_path, "healthy", TimedTransport(0.001))

    with pytest.raises(FleetScanError) as raised:
        run_scan_batches_fleet([failed, healthy], _batches(8), input_regions=REGIONS, row_bytes=ROW_BYTES, collect=_collect)

    error = raised.value
    assert error.device is failed
    assert error.batch_index is not None
    assert f"failed payload {error.batch_index}" in str(error.__cause__)
    assert "broken" in str(error)


def test_weights_pre_bias_batch_assignment(tmp_path):
    dpv1 = _device(tmp_path, "dpv1", TimedTransport(0.002))
    dpv2 = _device(tmp_path, "dpv2", TimedTransport(0.002))

    results = run_scan_batches_fleet(
        [dpv1, dpv2],
        _batches(12),
        input_regions=REGIONS,
        row_bytes=ROW_BYTES,
        collect=_collect,
        weights=[1, 2],
    )

    assert sum(result.device is dpv2 for result in results) > sum(result.device is dpv1 for result in results)


def test_fleet_defines_empty_inputs(tmp_path):
    device = _device(tmp_path, "card", TimedTransport())

    assert run_scan_batches_fleet([device], [], input_regions=REGIONS, row_bytes=ROW_BYTES, collect=_collect) == []
    with pytest.raises(ValueError, match="devices"):
        run_scan_batches_fleet([], [], input_regions=REGIONS, row_bytes=ROW_BYTES, collect=_collect)


def test_poll_yield_reaches_every_pipeline_status_wait(tmp_path) -> None:
    """``poll_yield_seconds`` exists for pure-Python transports, whose
    register read would otherwise hold the GIL and serialize co-running
    pipelines. The real transports release it (``os.pread`` / ``ctypes``),
    so the default is to spin — assert the plumbing deterministically
    rather than a wall-clock split, which is not a reliable signal."""
    sleeps: list[float] = []
    real_sleep = time.sleep

    def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        real_sleep(0)

    devices = [_device(tmp_path, "a", TimedTransport()), _device(tmp_path, "b", TimedTransport())]
    batches = _batches(4)
    with mock.patch.object(scan_execution.time, "sleep", recording_sleep):
        run_scan_batches_fleet(
            devices,
            batches,
            input_regions=(0x0, 0x1000),
            row_bytes=ROW_BYTES,
            collect=_collect,
            poll_yield_seconds=0.0,
        )
    # 0.0 must be honoured (a plain truthiness check would drop it) and it
    # must reach the wait inside every pipeline
    assert sleeps, "poll_yield_seconds=0.0 never reached the status wait"
    assert set(sleeps) == {0.0}

    sleeps.clear()
    with mock.patch.object(scan_execution.time, "sleep", recording_sleep):
        run_scan_batches_fleet(
            devices,
            batches,
            input_regions=(0x0, 0x1000),
            row_bytes=ROW_BYTES,
            collect=_collect,
        )
    assert not sleeps, "the default must spin: the real transports already release the GIL"
