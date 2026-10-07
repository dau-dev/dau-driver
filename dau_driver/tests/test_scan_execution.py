"""The streaming scan runner against the in-memory FakeTransport: the
ping-pong protocol (region alternation, exact INPUT_LENGTH, padded DMA),
the overlap guarantee (a job only completes once the next batch's DMA
landed), and the error paths."""

from __future__ import annotations

from typing import Any, cast

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


def _encode_pairs(records) -> bytes:
    return b"".join(int(key).to_bytes(4, "little") + int(value).to_bytes(4, "little", signed=True) for key, value in records)


def test_key_range_batches_bound_the_distinct_keys_per_batch() -> None:
    from dau_driver.scan_execution import key_range_scan_batches

    # keys deliberately UNSORTED on arrival: the hash-mode tile needs no
    # key ordering, so neither may this splitter
    records = [((seed * 7) % 10, seed) for seed in range(40)]
    batches = list(key_range_scan_batches(records, key_capacity=3, key=lambda record: record[0], encode=_encode_pairs))
    assert len(batches) == 4  # 10 distinct keys / 3 per batch
    assert sum(batch.row_count for batch in batches) == len(records)


def test_key_range_batches_put_each_key_in_exactly_one_batch() -> None:
    """The property that makes combining per-batch group records a
    concatenation rather than a summation."""
    from dau_driver.scan_execution import key_range_scan_batches

    records = [((seed * 7) % 10, seed) for seed in range(40)]
    seen: list[set[int]] = []
    for batch in key_range_scan_batches(records, key_capacity=3, key=lambda record: record[0], encode=_encode_pairs):
        keys = {int.from_bytes(batch.payload[offset : offset + 4], "little") for offset in range(0, len(batch.payload), 8)}
        assert len(keys) <= 3
        seen.append(keys)
    for index, keys in enumerate(seen):
        for other in seen[index + 1 :]:
            assert not (keys & other), "a key appeared in two batches"
    assert set().union(*seen) == {record[0] for record in records}


def test_key_range_batches_preserve_arrival_order_within_a_batch() -> None:
    from dau_driver.scan_execution import key_range_scan_batches

    records = [(9, 0), (1, 1), (9, 2), (1, 3), (5, 4)]
    batches = list(key_range_scan_batches(records, key_capacity=3, key=lambda record: record[0], encode=_encode_pairs))
    assert len(batches) == 1
    values = [int.from_bytes(batches[0].payload[offset + 4 : offset + 8], "little", signed=True) for offset in range(0, len(batches[0].payload), 8)]
    assert values == [0, 1, 2, 3, 4]  # not re-ordered by key


def test_key_range_batches_handle_the_boundaries() -> None:
    from dau_driver.scan_execution import key_range_scan_batches

    encode = _encode_pairs
    assert list(key_range_scan_batches([], key_capacity=4, key=lambda record: record[0], encode=encode)) == []
    exact = [(key, 0) for key in range(8)]
    assert len(list(key_range_scan_batches(exact, key_capacity=4, key=lambda record: record[0], encode=encode))) == 2
    assert len(list(key_range_scan_batches(exact, key_capacity=8, key=lambda record: record[0], encode=encode))) == 1
    with pytest.raises(ValueError, match="key_capacity must be positive"):
        list(key_range_scan_batches(exact, key_capacity=0, key=lambda record: record[0], encode=encode))


def test_key_range_batches_merge_back_to_the_one_shot_answer() -> None:
    """End to end against the tile golden: capacity-bounded batches run
    through the hash-mode model and merge to what one unbounded pass
    would have produced."""
    from dau_core.operators import HashGroupedErrorCode, hash_grouped_field_sum_rows, merge_hash_grouped

    from dau_driver.scan_execution import key_range_scan_batches

    rows = [((seed * 7) % 30, (seed * 13) % 97 - 40, 0, 0) for seed in range(300)]
    one_shot, error = hash_grouped_field_sum_rows(rows, field_mask=0b0010, capacity=64)
    assert error is HashGroupedErrorCode.OK
    # a table too small for the batch: the split is what makes it fit
    assert hash_grouped_field_sum_rows(rows, field_mask=0b0010, capacity=8)[1] is HashGroupedErrorCode.GROUP_CAPACITY_EXCEEDED

    partials = []
    # the fake payload IS the rows: the golden consumes them where a device would consume bytes
    for batch in key_range_scan_batches(rows, key_capacity=8, key=lambda row: row[0], encode=lambda records: cast(bytes, tuple(records))):
        groups, code = hash_grouped_field_sum_rows(cast(Any, batch.payload), field_mask=0b0010, capacity=8)
        assert code is HashGroupedErrorCode.OK, "capacity-bounded batches must not overflow"
        partials.append(groups)
    merged = merge_hash_grouped(partials)
    assert [(g.key, g.sums, g.count) for g in merged] == [(g.key, g.sums, g.count) for g in one_shot]


class ResidentTransport(FakeTransport):
    """DONE as soon as a job starts; records what was written."""

    def __init__(self) -> None:
        super().__init__()
        self.starts = 0

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset == int(RegisterOffset.JOB_CONTROL) and value & int(JobControlFlag.START):
            self.starts += 1

    def read_u32(self, offset: int) -> int:
        if offset == int(RegisterOffset.JOB_STATUS):
            return int(JobStatusFlag.DONE) if self.starts else 0
        return super().read_u32(offset)


def test_a_resident_run_uploads_nothing(tmp_path) -> None:
    """The whole point: rows already on the device are queried without a DMA.
    On the measured platform the feed is the larger term of the device path
    (40.0 ms total against a ~10.4 ms job), so removing it from the loop is
    what amortization means."""
    from dau_driver.scan_execution import run_resident_scan

    transport = ResidentTransport()
    device = fake_device(tmp_path, transport)
    results = run_resident_scan(device, address=REGION_A, row_count=6_001_215, row_bytes=8, collect=lambda _d: transport.starts, repeats=3)
    assert transport.h2c_writes == [], "a resident run must not stage anything"
    assert transport.starts == 3, "each repeat runs the job again"
    assert results == [1, 2, 3]


def test_a_resident_run_still_programs_the_exact_input_length(tmp_path) -> None:
    """Address and length are the contract; getting either wrong reads the
    wrong bytes and would be indistinguishable from a bad answer."""
    from dau_driver.scan_execution import run_resident_scan

    transport = ResidentTransport()
    run_resident_scan(fake_device(tmp_path, transport), address=REGION_B, row_count=1000, row_bytes=16, collect=lambda _d: None)
    assert transport.read_u32(int(RegisterOffset.INPUT_LENGTH_LOW)) == 16_000
    assert transport.read_u32(int(RegisterOffset.INPUT_ADDRESS_LOW)) == REGION_B & 0xFFFF_FFFF


def test_a_misaligned_resident_address_is_refused(tmp_path) -> None:
    """An unaligned DMA region is a silent-wrong-bytes failure on silicon,
    so it is refused where the mistake is still cheap."""
    from dau_driver.pipeline_execution import DMA_ALIGN_BYTES
    from dau_driver.scan_execution import run_resident_scan

    with pytest.raises(ValueError, match="aligned"):
        run_resident_scan(
            fake_device(tmp_path, ResidentTransport()),
            address=DMA_ALIGN_BYTES + 1,
            row_count=8,
            row_bytes=8,
            collect=lambda _d: None,
        )
