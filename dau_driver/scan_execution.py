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

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from dau_core.registers import JobControlFlag, RegisterOffset

from .pipeline_execution import DMA_ALIGN_BYTES, _wait_for_job_done


@dataclass(frozen=True)
class ScanBatch:
    """One staged scan batch: the packed row payload (``row_count *
    row_bytes`` bytes; the runner pads the DMA to the 64-byte grid while
    INPUT_LENGTH stays exact) and its row count."""

    payload: bytes
    row_count: int


def run_scan_batches(
    device,
    batches: Iterable[ScanBatch],
    *,
    input_regions: tuple[int, int],
    row_bytes: int,
    collect: Callable[[Any], Any],
    timeout_seconds: float = 60.0,
    progress=None,
) -> list[Any]:
    """Run scan jobs over ``batches`` with ping-pong input staging: batch
    ``i + 1`` DMAs into the alternate region (and, for a lazy iterable,
    is produced) while job ``i`` computes. Returns ``collect(device)``
    results in batch order. ``batches`` may be lazy — encoding work in
    the iterator overlaps compute too."""
    region_a, region_b = input_regions
    if region_a == region_b:
        raise ValueError("input_regions must be two distinct device addresses")
    if row_bytes <= 0:
        raise ValueError(f"row_bytes must be positive, got {row_bytes}")

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
            device.write_register(RegisterOffset.INPUT_ADDRESS_LOW, address)
            device.write_register(RegisterOffset.INPUT_LENGTH_LOW, row_count * row_bytes)
            device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))
            pending = pool.submit(stage, index + 1)

            def surface_staging_failure(future=pending):
                if future.done() and future.exception() is not None:
                    raise future.exception()

            _wait_for_job_done(device, timeout_seconds=timeout_seconds, label=f"scan job {index}", poll=surface_staging_failure)
            results.append(collect(device))
            if progress is not None:
                progress(index + 1)
            index += 1
    return results
