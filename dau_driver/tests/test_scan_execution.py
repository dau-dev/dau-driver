"""The streaming scan runner against the in-memory FakeTransport: the
ping-pong protocol (region alternation, exact INPUT_LENGTH, padded DMA),
the overlap guarantee (a job only completes once the next batch's DMA
landed), and the error paths."""

from __future__ import annotations

import pytest
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset

from dau_driver.pipeline_execution import PipelineJobError
from dau_driver.scan_execution import ScanBatch, run_scan_batches
from dau_driver.testing import FakeTransport, fake_device

REGION_A = 0x0000_0000
REGION_B = 0x0800_0000


class OverlapTransport(FakeTransport):
    """JOB_STATUS reports DONE only once the *next* batch's H2C write has
    landed (the last job completes unconditionally): a serial
    stage-after-finish runner deadlocks into the timeout here, so a green
    run proves staging overlaps the running job."""

    def __init__(self, total_batches: int) -> None:
        super().__init__()
        self.total = total_batches
        self.started = 0

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset == int(RegisterOffset.JOB_CONTROL) and value & int(JobControlFlag.START):
            self.started += 1

    def read_u32(self, offset: int) -> int:
        if offset == int(RegisterOffset.JOB_STATUS):
            required = min(self.started + 1, self.total)
            return int(JobStatusFlag.DONE) if len(self.h2c_writes) >= required else 0
        return super().read_u32(offset)


def _batches(rows_per_batch: list[int], row_bytes: int = 16) -> list[ScanBatch]:
    return [ScanBatch(payload=bytes(rows * row_bytes), row_count=rows) for rows in rows_per_batch]


def test_run_scan_batches_alternates_regions_and_overlaps_staging(tmp_path):
    transport = OverlapTransport(total_batches=3)
    device = fake_device(tmp_path, transport)
    snapshots = []

    def collect(dev):
        snapshots.append((transport.registers[int(RegisterOffset.INPUT_ADDRESS_LOW)], transport.registers[int(RegisterOffset.INPUT_LENGTH_LOW)]))
        return len(snapshots) - 1

    results = run_scan_batches(
        device,
        _batches([4, 7, 2]),
        input_regions=(REGION_A, REGION_B),
        row_bytes=16,
        collect=collect,
        timeout_seconds=5.0,
    )

    assert results == [0, 1, 2]
    assert [address for address, _ in transport.h2c_writes] == [REGION_A, REGION_B, REGION_A]
    assert all(len(payload) % 64 == 0 for _, payload in transport.h2c_writes)
    assert snapshots == [(REGION_A, 4 * 16), (REGION_B, 7 * 16), (REGION_A, 2 * 16)]


def test_run_scan_batches_accepts_a_lazy_iterator(tmp_path):
    transport = OverlapTransport(total_batches=2)
    device = fake_device(tmp_path, transport)
    produced = []

    def lazy():
        for rows in (3, 5):
            produced.append(rows)
            yield ScanBatch(payload=bytes(rows * 8), row_count=rows)

    results = run_scan_batches(
        device,
        lazy(),
        input_regions=(REGION_A, REGION_B),
        row_bytes=8,
        collect=lambda dev: transport.registers[int(RegisterOffset.INPUT_LENGTH_LOW)],
        timeout_seconds=5.0,
    )

    assert results == [24, 40]
    assert produced == [3, 5]


def test_run_scan_batches_rejects_payload_row_mismatch(tmp_path):
    transport = FakeTransport()
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    device = fake_device(tmp_path, transport)

    with pytest.raises(ValueError, match="payload of 8 bytes is not row_count 3"):
        run_scan_batches(
            device,
            [ScanBatch(payload=bytes(8), row_count=3)],
            input_regions=(REGION_A, REGION_B),
            row_bytes=16,
            collect=lambda dev: None,
        )


def test_run_scan_batches_propagates_job_errors(tmp_path):
    transport = FakeTransport()
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE) | int(JobStatusFlag.ERROR)
    transport.registers[int(RegisterOffset.LAST_ERROR)] = 0xFE
    device = fake_device(tmp_path, transport)

    with pytest.raises(PipelineJobError, match="scan job 0 failed"):
        run_scan_batches(
            device,
            _batches([2]),
            input_regions=(REGION_A, REGION_B),
            row_bytes=16,
            collect=lambda dev: None,
        )


def test_run_scan_batches_validates_configuration_and_empty(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)

    with pytest.raises(ValueError, match="distinct device addresses"):
        run_scan_batches(device, [], input_regions=(REGION_A, REGION_A), row_bytes=16, collect=lambda dev: None)
    with pytest.raises(ValueError, match="row_bytes"):
        run_scan_batches(device, [], input_regions=(REGION_A, REGION_B), row_bytes=0, collect=lambda dev: None)
    assert run_scan_batches(device, [], input_regions=(REGION_A, REGION_B), row_bytes=16, collect=lambda dev: None) == []
    assert transport.h2c_writes == []
