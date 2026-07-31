from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset

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


def test_concurrent_pipelines_yield_the_gil_so_demand_tracks_device_speed() -> None:
    """The fleet's self-balancing promise is only real if co-running
    pipelines actually interleave: a pure Python spin on JOB_STATUS holds
    the GIL, so a fast device cannot claim work while a slow one polls.
    With the yield, batch shares track device speed."""
    import time as _time

    from dau_core.registers import JobStatusFlag, RegisterOffset

    class _TimedDevice:
        def __init__(self, job_seconds: float) -> None:
            self.job_seconds = job_seconds
            self.jobs = 0
            self._started = None

        def write_dma(self, *, device_address, data):
            return None

        def write_register(self, offset, value):
            if int(offset) == int(RegisterOffset.JOB_CONTROL) and value & 1:
                self._started = _time.perf_counter()
                self.jobs += 1

        def read_register(self, offset):
            if int(offset) == int(RegisterOffset.JOB_STATUS):
                elapsed = _time.perf_counter() - self._started
                return int(JobStatusFlag.DONE) if elapsed >= self.job_seconds else 0
            return 0

        def read_dma(self, **kwargs):
            return b"\x00" * 64

    fast, slow = _TimedDevice(0.002), _TimedDevice(0.030)
    batches = [ScanBatch(payload=b"\x00" * 16, row_count=1) for _ in range(20)]
    results = run_scan_batches_fleet(
        [fast, slow],
        batches,
        input_regions=(0x0, 0x1000),
        row_bytes=16,
        collect=lambda device: device,
    )
    assert [result.batch_index for result in results] == list(range(20))
    assert fast.jobs + slow.jobs == 20
    # the 15x faster device must take a clear majority; an even split is the
    # GIL-serialized signature this default exists to prevent
    assert fast.jobs >= 2 * slow.jobs, f"fast={fast.jobs} slow={slow.jobs}: pipelines are not interleaving"
