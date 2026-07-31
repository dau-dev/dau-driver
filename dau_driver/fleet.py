"""Concurrent scan scheduling across independent DAU devices."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .scan_execution import ScanBatch, run_scan_batches


@dataclass(frozen=True)
class FleetScanResult:
    """One collected batch result with its global index and exact device.

    Keeping the device object, rather than only its display name, makes
    provenance unambiguous even when callers construct devices with duplicate
    names.
    """

    batch_index: int
    device: Any
    value: Any


class FleetScanError(RuntimeError):
    """Failure from one device pipeline, annotated with fleet provenance."""

    def __init__(self, batch_index: int | None, device: Any, cause: Exception) -> None:
        self.batch_index = batch_index
        self.device = device
        identity = getattr(device, "name", repr(device))
        batch = "before pulling a batch" if batch_index is None else f"batch {batch_index}"
        super().__init__(f"fleet scan {batch} on device {identity!r} failed: {cause}")


@dataclass
class _DeviceTracker:
    indices: list[int] = field(default_factory=list)
    staged_count: int = 0
    completed_count: int = 0
    explicit_error_index: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def pulled(self, batch_index: int) -> None:
        with self.lock:
            self.indices.append(batch_index)

    def staged(self) -> None:
        with self.lock:
            self.staged_count += 1

    def completed(self) -> None:
        with self.lock:
            self.completed_count += 1

    def mark_error(self, batch_index: int) -> None:
        with self.lock:
            self.explicit_error_index = batch_index

    def result_indices(self) -> list[int]:
        with self.lock:
            return self.indices[: self.completed_count]

    def failure_index(self) -> int | None:
        with self.lock:
            if self.explicit_error_index is not None:
                return self.explicit_error_index
            if self.staged_count < len(self.indices):
                return self.indices[self.staged_count]
            if self.completed_count < len(self.indices):
                return self.indices[self.completed_count]
            return self.indices[-1] if self.indices else None


class _TrackedDevice:
    def __init__(self, device: Any, tracker: _DeviceTracker) -> None:
        self._device = device
        self._tracker = tracker

    def __getattr__(self, name: str) -> Any:
        return getattr(self._device, name)

    def write_dma(self, *, device_address: int, data: bytes | bytearray | memoryview):
        result = self._device.write_dma(device_address=device_address, data=data)
        self._tracker.staged()
        return result


class _BatchIterationError(Exception):
    def __init__(self, batch_index: int, cause: Exception) -> None:
        self.batch_index = batch_index
        self.cause = cause
        super().__init__(str(cause))


class _SharedBatches:
    def __init__(self, batches: Iterable[ScanBatch], device_count: int, weights: Sequence[float] | None) -> None:
        self._iterator = iter(batches)
        self._condition = threading.Condition()
        self._next_index = 0
        self._cancelled = False
        self._exhausted = False
        self._weights = weights
        self._weighted_current = [0.0] * device_count

    def pull(self, device_index: int, tracker: _DeviceTracker) -> ScanBatch:
        with self._condition:
            while not self._cancelled and not self._exhausted and self._preferred_device() not in (None, device_index):
                self._condition.wait()
            if self._cancelled or self._exhausted:
                raise StopIteration

            batch_index = self._next_index
            try:
                batch = next(self._iterator)
            except StopIteration:
                self._exhausted = True
                self._condition.notify_all()
                raise
            except Exception as exc:
                self._cancelled = True
                self._condition.notify_all()
                raise _BatchIterationError(batch_index, exc) from exc

            self._next_index += 1
            tracker.pulled(batch_index)
            self._advance_weighted_schedule(device_index)
            self._condition.notify_all()
            return batch

    def _preferred_device(self) -> int | None:
        if self._weights is None:
            return None
        return max(
            range(len(self._weights)),
            key=lambda index: self._weighted_current[index] + self._weights[index],
        )

    def _advance_weighted_schedule(self, selected: int) -> None:
        if self._weights is None:
            return
        for index, weight in enumerate(self._weights):
            self._weighted_current[index] += weight
        self._weighted_current[selected] -= sum(self._weights)

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()


class _DeviceBatches:
    def __init__(self, source: _SharedBatches, device_index: int, tracker: _DeviceTracker) -> None:
        self._source = source
        self._device_index = device_index
        self._tracker = tracker

    def __iter__(self) -> _DeviceBatches:
        return self

    def __next__(self) -> ScanBatch:
        return self._source.pull(self._device_index, self._tracker)


class _Progress:
    def __init__(self, callback: Callable[[int], Any] | None) -> None:
        self._callback = callback
        self._completed = 0
        self._lock = threading.Lock()

    def report(self) -> None:
        if self._callback is None:
            return
        with self._lock:
            self._completed += 1
            self._callback(self._completed)


class _WorkerError(Exception):
    def __init__(self, batch_index: int | None, device: Any, cause: Exception) -> None:
        self.batch_index = batch_index
        self.device = device
        self.cause = cause
        super().__init__(str(cause))


def run_scan_batches_fleet(
    devices,
    batches: Iterable[ScanBatch],
    *,
    input_regions: tuple[int, int],
    row_bytes: int,
    collect: Callable[[Any], Any],
    timeout_seconds: float = 60.0,
    progress=None,
    weights: Sequence[float] | None = None,
    poll_yield_seconds: float | None = 50e-6,
) -> list[FleetScanResult]:
    """Run one independent scan pipeline per device over a shared lazy input.

    Each batch is pulled exactly once by whichever device pipeline demands it.
    With ``weights=None`` this is pure demand-driven scheduling and naturally
    self-balances by device throughput. Weights use weighted round-robin only
    to pre-bias each shared pull toward the requested relative device shares;
    batches remain lazy and are never partitioned or queued ahead.

    Results are returned in global batch order as frozen
    :class:`FleetScanResult` records. Each record retains the exact device
    object so the caller can recover which card produced its collect value.
    ``input_regions`` is reused as device-local addresses, never shared memory.

    ``poll_yield_seconds`` (default 50 us) sleeps between JOB_STATUS reads
    inside each pipeline. Concurrent pipelines are Python threads, so a
    pure spin would hold the GIL and serialize them — self-balancing then
    degrades toward an even split regardless of device speed. Measured on
    fakes with a 15x device speed gap over 20 batches: spinning gives a
    13/7 split in 210 ms, yielding gives 17/3 in 90 ms. Pass ``None`` to
    restore the tight spin (a single-device fleet, or a device whose
    register read already releases the GIL).

    An empty batch iterable returns ``[]``. An empty device sequence raises
    ``ValueError`` because no pipeline can consume even a potentially lazy
    iterable. A single device invokes the same ``run_scan_batches`` pipeline as
    the multi-device workers and adds only ordered provenance records.
    """
    resolved_devices = list(devices)
    if not resolved_devices:
        raise ValueError("devices must contain at least one device")
    _validate_configuration(input_regions, row_bytes)
    resolved_weights = _validate_weights(weights, len(resolved_devices))

    source = _SharedBatches(batches, len(resolved_devices), resolved_weights)
    fleet_progress = _Progress(progress)

    def run_device(device_index: int) -> list[FleetScanResult]:
        device = resolved_devices[device_index]
        tracker = _DeviceTracker()
        tracked_device = _TrackedDevice(device, tracker)

        def tracked_collect(_tracked_device):
            value = collect(device)
            tracker.completed()
            return value

        def tracked_progress(local_count: int) -> None:
            result_indices = tracker.result_indices()
            try:
                fleet_progress.report()
            except Exception:
                if result_indices:
                    tracker.mark_error(result_indices[local_count - 1])
                raise

        try:
            values = run_scan_batches(
                tracked_device,
                _DeviceBatches(source, device_index, tracker),
                input_regions=input_regions,
                row_bytes=row_bytes,
                collect=tracked_collect,
                timeout_seconds=timeout_seconds,
                poll_yield_seconds=poll_yield_seconds,
                progress=tracked_progress if progress is not None else None,
            )
        except _BatchIterationError as exc:
            source.cancel()
            raise _WorkerError(exc.batch_index, device, exc.cause) from exc.cause
        except Exception as exc:
            source.cancel()
            raise _WorkerError(tracker.failure_index(), device, exc) from exc

        return [FleetScanResult(batch_index, device, value) for batch_index, value in zip(tracker.result_indices(), values, strict=True)]

    if len(resolved_devices) == 1:
        try:
            return run_device(0)
        except _WorkerError as exc:
            raise FleetScanError(exc.batch_index, exc.device, exc.cause) from exc.cause

    results: list[FleetScanResult] = []
    with ThreadPoolExecutor(max_workers=len(resolved_devices)) as pool:
        futures: list[Future[list[FleetScanResult]]] = [pool.submit(run_device, index) for index in range(len(resolved_devices))]
        try:
            for future in as_completed(futures):
                results.extend(future.result())
        except _WorkerError as exc:
            source.cancel()
            for future in futures:
                future.cancel()
            raise FleetScanError(exc.batch_index, exc.device, exc.cause) from exc.cause

    results.sort(key=lambda result: result.batch_index)
    return results


def _validate_configuration(input_regions: tuple[int, int], row_bytes: int) -> None:
    region_a, region_b = input_regions
    if region_a == region_b:
        raise ValueError("input_regions must be two distinct device addresses")
    if row_bytes <= 0:
        raise ValueError(f"row_bytes must be positive, got {row_bytes}")


def _validate_weights(weights: Sequence[float] | None, device_count: int) -> tuple[float, ...] | None:
    if weights is None:
        return None
    if len(weights) != device_count:
        raise ValueError(f"weights must contain one value per device, got {len(weights)} for {device_count} devices")
    resolved = tuple(float(weight) for weight in weights)
    if any(not math.isfinite(weight) or weight <= 0 for weight in resolved):
        raise ValueError("weights must all be finite and positive")
    return resolved
