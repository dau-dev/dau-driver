"""Streaming scan-job runner: ping-pong staging over two DDR input
regions so the next batch's H2C DMA overlaps the current job's compute —
the OHLCV bar-DDR overlap pattern generalized to the scan-composition
register contract (INPUT_ADDRESS/INPUT_LENGTH + JOB_CONTROL/JOB_STATUS).

The caller owns everything shell-specific: lane output addresses and
any static configuration are written before the run, and ``collect``
reads each job's results (lane registers + output regions, which are
disjoint from input staging, so collection also overlaps the next DMA).

Overlap caveat: H2C writes and the job's burst reads share one DDR
controller on dpv1, so the measured win is bounded by DDR bandwidth
headroom — the silicon A/B decides the batch split, not this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import groupby
from typing import Any

from dau_core.registers import JobControlFlag, RegisterOffset

from .device import write_address
from .pipeline_execution import DMA_ALIGN_BYTES, _wait_for_job_done


@dataclass(frozen=True)
class ScanBatch:
    """One staged scan batch: the packed row payload (``row_count *
    row_bytes`` bytes; the runner pads the DMA to the 64-byte grid while
    INPUT_LENGTH stays exact) and its row count."""

    payload: bytes
    row_count: int


class _KeyPartitionedScanBatches[Record](Iterator[ScanBatch]):
    def __init__(
        self,
        records: Iterable[Record],
        *,
        batch_count: int,
        key: Callable[[Record], Hashable],
        encode: Callable[[Iterable[Record]], bytes],
    ) -> None:
        self._records = records
        self._batch_count = batch_count
        self._key = key
        self._encode = encode
        self._batches = self._build_batches()

    def __next__(self) -> ScanBatch:
        return next(self._batches)

    def _build_batches(self) -> Iterator[ScanBatch]:
        groups: list[tuple[int, tuple[Record, ...]]] = []
        seen: set[Hashable] = set()
        for group_index, (group_key, records) in enumerate(groupby(self._records, key=self._key)):
            if group_key in seen:
                raise ValueError(f"records must be ordered by key; key {group_key!r} appears in more than one group")
            seen.add(group_key)
            groups.append((group_index, tuple(records)))

        if not groups:
            return

        assignments: list[list[tuple[int, tuple[Record, ...]]]] = [[] for _ in range(min(self._batch_count, len(groups)))]
        loads = [0] * len(assignments)
        for group in sorted(groups, key=lambda item: len(item[1]), reverse=True):
            batch_index = min(range(len(assignments)), key=loads.__getitem__)
            assignments[batch_index].append(group)
            loads[batch_index] += len(group[1])

        for assignment in assignments:
            assignment.sort(key=lambda item: item[0])
            batch_records = tuple(record for _, records in assignment for record in records)
            yield ScanBatch(payload=self._encode(batch_records), row_count=len(batch_records))


def key_partitioned_scan_batches[Record](
    records: Iterable[Record],
    *,
    batch_count: int,
    key: Callable[[Record], Hashable],
    encode: Callable[[Iterable[Record]], bytes],
) -> Iterator[ScanBatch]:
    """Build key-confined batches using longest-processing-time-first
    greedy assignment: largest key group goes into the currently-smallest
    batch. Records must be ordered by key, and record order within each key
    is preserved.

    LPT requires every key-group size before assignment, so first iteration
    consumes one complete grouping pass. Creating this iterable consumes
    nothing, and each batch's flattened records and encoded payload are
    constructed only when that batch is requested. Since key groups are
    indivisible, the largest group bounds the difference between batch
    sizes; one hot key can therefore dominate a batch.
    """
    if batch_count <= 0:
        raise ValueError(f"batch_count must be positive, got {batch_count}")
    return _KeyPartitionedScanBatches(records, batch_count=batch_count, key=key, encode=encode)


class _KeyRangeScanBatches[Record]:
    """Lazily materialized capacity-bounded batches (see
    :func:`key_range_scan_batches`)."""

    def __init__(
        self,
        records: Iterable[Record],
        *,
        key_capacity: int,
        key: Callable[[Record], Hashable],
        encode: Callable[[Iterable[Record]], bytes],
    ) -> None:
        self._records = tuple(records)
        self._key_capacity = key_capacity
        self._key = key
        self._encode = encode

    def __iter__(self) -> Iterator[ScanBatch]:
        keys = {self._key(record) for record in self._records}
        if not keys:
            return
        ordered = sorted(keys)
        # each key lands in exactly ONE range, so per-key state never
        # spans batches and merging the partials is a concatenation
        batch_of_key = {key: index // self._key_capacity for index, key in enumerate(ordered)}
        batch_count = (len(ordered) + self._key_capacity - 1) // self._key_capacity
        for batch_index in range(batch_count):
            # arrival order is preserved WITHIN the batch: the hash-mode
            # table needs no key ordering, so this splits by key without
            # imposing one
            batch_records = tuple(record for record in self._records if batch_of_key[self._key(record)] == batch_index)
            yield ScanBatch(payload=self._encode(batch_records), row_count=len(batch_records))


def key_range_scan_batches[Record](
    records: Iterable[Record],
    *,
    key_capacity: int,
    key: Callable[[Record], Hashable],
    encode: Callable[[Iterable[Record]], bytes],
) -> Iterator[ScanBatch]:
    """Split records into batches each holding at most ``key_capacity``
    DISTINCT keys — the host half of the hash-mode grouped tile's overflow
    contract.

    The tile's table is a hardware fact: a batch presenting more distinct
    keys than it holds closes out ``GROUP_CAPACITY_EXCEEDED`` and is
    invalidated. Rather than react to that, size the batches so it cannot
    happen: partition the key space into ranges of ``key_capacity`` keys
    and emit one batch per range. Each key therefore lands in exactly ONE
    batch, so no per-key state spans a batch boundary and combining the
    per-batch group records is a concatenation — a stronger property than
    the general additive merge
    (``dau_core.operators.merge_hash_grouped``), which remains correct and
    is what a caller should use when batches were produced some other way.

    Unlike :func:`key_partitioned_scan_batches`, records need NOT arrive in
    key order and arrival order is preserved within each batch: the
    hash-mode table needs no key ordering, and imposing one here would give
    back the staging freedom the tile exists to provide. The trade is a
    counting pass over every key before the first batch is produced, and
    batches whose ROW counts may be uneven — key ranges are equal in keys,
    not in rows. The device's error close-out remains the safety net for a
    caller whose ``key_capacity`` does not match the built design.
    """
    if key_capacity <= 0:
        raise ValueError(f"key_capacity must be positive, got {key_capacity}")
    return _KeyRangeScanBatches(records, key_capacity=key_capacity, key=key, encode=encode)


def run_scan_batches(
    device,
    batches: Iterable[ScanBatch],
    *,
    per_key_state: bool,
    input_regions: tuple[int, int],
    row_bytes: int,
    collect: Callable[[Any], Any],
    timeout_seconds: float = 60.0,
    progress=None,
    poll_yield_seconds: float | None = None,
) -> list[Any]:
    """Run scan jobs over ``batches`` with ping-pong input staging: batch
    ``i + 1`` DMAs into the alternate region (and, for a lazy iterable,
    is produced) while job ``i`` computes. Returns ``collect(device)``
    results in batch order. ``batches`` may be lazy — encoding work in
    the iterator overlaps compute too. ``per_key_state`` is required so a
    stateful composition cannot silently run position-sliced batches; such
    jobs accept only the result of :func:`key_partitioned_scan_batches`."""
    region_a, region_b = input_regions
    if region_a == region_b:
        raise ValueError("input_regions must be two distinct device addresses")
    if row_bytes <= 0:
        raise ValueError(f"row_bytes must be positive, got {row_bytes}")
    if per_key_state and not isinstance(batches, _KeyPartitionedScanBatches):
        raise ValueError("scan composition has cross-batch per-key state; batches must be key-partitioned with key_partitioned_scan_batches")

    iterator = iter(batches)
    regions = (region_a, region_b)

    def stage(index: int) -> tuple[int, int] | None:
        try:
            batch = next(iterator)
        except StopIteration:
            return None
        if batch.row_count <= 0:
            raise ValueError(f"batch {index}: zero rows; a scan job needs a non-empty batch")
        if len(batch.payload) != batch.row_count * row_bytes:
            raise ValueError(f"batch {index}: payload of {len(batch.payload)} bytes is not row_count {batch.row_count} * row_bytes {row_bytes}")
        payload = batch.payload
        if len(payload) % DMA_ALIGN_BYTES:
            payload = payload + b"\x00" * (-len(payload) % DMA_ALIGN_BYTES)
        address = regions[index % 2]
        device.write_dma(device_address=address, data=payload)
        return address, batch.row_count

    results: list[Any] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(stage, 0)
        index = 0
        while True:
            staged = pending.result()
            if staged is None:
                break
            address, row_count = staged
            write_address(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, address)
            device.write_register(RegisterOffset.INPUT_LENGTH_LOW, row_count * row_bytes)
            device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))
            pending = pool.submit(stage, index + 1)

            def surface_staging_failure(future=pending):
                if future.done() and future.exception() is not None:
                    raise future.exception()
                if poll_yield_seconds is not None:
                    time.sleep(poll_yield_seconds)

            _wait_for_job_done(device, timeout_seconds=timeout_seconds, label=f"scan job {index}", poll=surface_staging_failure)
            results.append(collect(device))
            if progress is not None:
                progress(index + 1)
            index += 1
    return results
