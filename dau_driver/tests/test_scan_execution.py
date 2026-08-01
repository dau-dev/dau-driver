"""The streaming scan runner against the in-memory FakeTransport: the
ping-pong protocol (region alternation, exact INPUT_LENGTH, padded DMA),
the overlap guarantee (a job only completes once the next batch's DMA
landed), and the error paths."""

from __future__ import annotations

import pytest
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset

from dau_driver.pipeline_execution import PipelineJobError
from dau_driver.scan_execution import ScanBatch, key_partitioned_scan_batches, run_scan_batches
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
        per_key_state=False,
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
        per_key_state=False,
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
            per_key_state=False,
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
            per_key_state=False,
            input_regions=(REGION_A, REGION_B),
            row_bytes=16,
            collect=lambda dev: None,
        )


def test_run_scan_batches_validates_configuration_and_empty(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)

    with pytest.raises(ValueError, match="distinct device addresses"):
        run_scan_batches(device, [], per_key_state=False, input_regions=(REGION_A, REGION_A), row_bytes=16, collect=lambda dev: None)
    with pytest.raises(ValueError, match="row_bytes"):
        run_scan_batches(device, [], per_key_state=False, input_regions=(REGION_A, REGION_B), row_bytes=0, collect=lambda dev: None)
    assert run_scan_batches(device, [], per_key_state=False, input_regions=(REGION_A, REGION_B), row_bytes=16, collect=lambda dev: None) == []
    assert transport.h2c_writes == []


def test_run_scan_batches_surfaces_staging_failures_promptly(tmp_path):
    # job 0 can never complete (its fake requires batch 1 staged), and
    # batch 1 is invalid: the staging error must surface instead of the
    # job-timeout PipelineJobError
    transport = OverlapTransport(total_batches=2)
    device = fake_device(tmp_path, transport)
    batches = [ScanBatch(payload=bytes(2 * 16), row_count=2), ScanBatch(payload=bytes(8), row_count=2)]

    with pytest.raises(ValueError, match="batch 1: payload of 8 bytes"):
        run_scan_batches(
            device,
            batches,
            per_key_state=False,
            input_regions=(REGION_A, REGION_B),
            row_bytes=16,
            collect=lambda dev: None,
            timeout_seconds=5.0,
        )


def test_run_scan_batches_rejects_zero_row_batches(tmp_path):
    transport = FakeTransport()
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    device = fake_device(tmp_path, transport)

    with pytest.raises(ValueError, match="batch 0: zero rows"):
        run_scan_batches(
            device,
            [ScanBatch(payload=b"", row_count=0)],
            per_key_state=False,
            input_regions=(REGION_A, REGION_B),
            row_bytes=16,
            collect=lambda dev: None,
        )


def _record_batches(records, *, batch_count):
    return key_partitioned_scan_batches(
        records,
        batch_count=batch_count,
        key=lambda record: record[0],
        encode=lambda batch_records: b"".join(record[1].to_bytes(2, "little") for record in batch_records),
    )


def _decoded_records(batch):
    return [int.from_bytes(batch.payload[offset : offset + 2], "little") for offset in range(0, len(batch.payload), 2)]


def test_key_partitioned_batches_confine_skewed_keys_and_balance_within_largest_group():
    group_sizes = {"hot": 10, "a": 6, "b": 5, "c": 4, "d": 1}
    keyed_records = []
    record_keys = {}
    record_id = 0
    for key, size in group_sizes.items():
        for _ in range(size):
            keyed_records.append((key, record_id))
            record_keys[record_id] = key
            record_id += 1

    batches = list(_record_batches(keyed_records, batch_count=3))
    key_locations = {}
    for batch_index, batch in enumerate(batches):
        for encoded_id in _decoded_records(batch):
            key_locations.setdefault(record_keys[encoded_id], set()).add(batch_index)

    assert all(len(locations) == 1 for locations in key_locations.values())
    assert sorted(batch.row_count for batch in batches) == [7, 9, 10]
    assert max(batch.row_count for batch in batches) - min(batch.row_count for batch in batches) <= max(group_sizes.values())


def test_key_partitioned_batches_degenerate_cases():
    assert list(_record_batches([], batch_count=3)) == []

    one_key = [("only", index) for index in range(5)]
    one_key_batches = list(_record_batches(one_key, batch_count=4))
    assert [batch.row_count for batch in one_key_batches] == [5]
    assert _decoded_records(one_key_batches[0]) == list(range(5))

    one_batch = list(_record_batches([("a", 1), ("b", 2), ("b", 3)], batch_count=1))
    assert [batch.row_count for batch in one_batch] == [3]
    assert set(_decoded_records(one_batch[0])) == {1, 2, 3}


def test_key_partitioned_batches_delay_input_consumption_and_encode_payloads_one_at_a_time():
    consumed = []
    encoded = []

    def records():
        for record in [("a", 1), ("a", 2), ("b", 3), ("c", 4)]:
            consumed.append(record)
            yield record

    def encode(batch_records):
        batch_records = tuple(batch_records)
        encoded.append(batch_records)
        return b"".join(record[1].to_bytes(2, "little") for record in batch_records)

    batches = key_partitioned_scan_batches(records(), batch_count=2, key=lambda record: record[0], encode=encode)
    iterator = iter(batches)
    assert iterator is batches
    assert consumed == []
    assert encoded == []

    first = next(iterator)
    assert consumed == [("a", 1), ("a", 2), ("b", 3), ("c", 4)]
    assert len(encoded) == 1
    assert first.row_count == len(encoded[0])

    next(iterator)
    assert len(encoded) == 2


def test_key_partitioning_avoids_positional_split_state_hazard():
    records = [("A", 1), ("A", 2), ("A", 3), ("B", 4), ("B", 5), ("C", 6)]
    positional_batches = [records[:2], records[2:4], records[4:]]
    assert sum("A" in {key for key, _ in batch} for batch in positional_batches) == 2

    record_keys = {record_id: key for key, record_id in records}
    key_partitioned = list(_record_batches(records, batch_count=3))
    assert sum("A" in {record_keys[record_id] for record_id in _decoded_records(batch)} for batch in key_partitioned) == 1


def test_run_scan_batches_refuses_positional_batches_for_per_key_state(tmp_path):
    device = fake_device(tmp_path, FakeTransport())

    with pytest.raises(ValueError, match="per-key state.*key-partitioned"):
        run_scan_batches(
            device,
            _batches([2, 2]),
            per_key_state=True,
            input_regions=(REGION_A, REGION_B),
            row_bytes=16,
            collect=lambda dev: None,
        )


def test_run_scan_batches_accepts_key_partitioned_batches_for_per_key_state(tmp_path):
    transport = FakeTransport()
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    device = fake_device(tmp_path, transport)

    results = run_scan_batches(
        device,
        _record_batches([("a", 1), ("a", 2), ("b", 3)], batch_count=2),
        per_key_state=True,
        input_regions=(REGION_A, REGION_B),
        row_bytes=2,
        collect=lambda dev: transport.registers[int(RegisterOffset.INPUT_LENGTH_LOW)],
    )

    assert sorted(results) == [2, 4]


def test_batch_balance_is_bounded_by_the_largest_key() -> None:
    """A key cannot be split without resetting the per-key state the
    partitioning exists to protect, so one dominant key caps how evenly
    batches can balance — and therefore how much overlap can hide. Pinned
    because the balancing heuristic could otherwise be read as a promise
    it cannot keep under skew."""
    from dau_driver.scan_execution import key_partitioned_scan_batches

    records = [(0, index) for index in range(500)] + [(key, index) for key in range(1, 64) for index in range(5)]
    records.sort()
    batches = list(
        key_partitioned_scan_batches(
            records,
            batch_count=8,
            key=lambda record: record[0],
            encode=lambda rows: bytes(16 * len(list(rows))),
        )
    )
    sizes = [batch.row_count for batch in batches]
    assert sum(sizes) == len(records)
    # the hot key lands whole in one batch; that batch cannot be smaller
    assert max(sizes) >= 500
    # every other batch is far smaller — the skew is structural, not a bug
    assert sorted(sizes)[-2] < 100
