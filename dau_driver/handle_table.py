"""The host end of the shell's handle table: the binding between the
``StorageAllocator``'s bookkeeping and the device aperture that enforces it.

:mod:`dau_driver.storage` hands out generation-checked handles, and
:func:`dau_driver.scan_execution.run_resident_scan` runs jobs over rows already
on the device -- but by RAW ADDRESS, which is the exposure the table exists to
close. A raw address survives its allocation: free the region, hand the space
to someone else, and the old address still reads perfectly. It just reads the
new tenant's bytes, and no layer above can tell that from an answer.

A handle cannot do that. The allocator's generation is programmed into the
slot, a job names ``(id, generation)`` instead of an address, and a resolution
that disagrees REFUSES the job -- the reader never issues a beat. That refusal
arrives here as :class:`HandleFaultError`, never as results, so a caller cannot
mistake it for a job that ran and summed nothing.

TWO AUTHORITIES, deliberately different:

* ``install`` trusts the HOST. The allocator's live record is what a slot is
  programmed from, and a handle it will not resolve is a host bug -- installing
  it would teach the device a grant the host does not believe in.
* ``arm`` trusts NOTHING. A stale handle must reach the device, or the table
  is never the thing that refuses it and the refusal is a host-side assertion
  wearing hardware's name.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from dau_core.registers import (
    HandleFaultCode,
    HandleTableControlFlag,
    HandleTableRegisterOffset,
    JobControlFlag,
    RegisterOffset,
)

from .device import write_address
from .errors import DauDriverError
from .pipeline_execution import DMA_ALIGN_BYTES, PipelineJobError, _wait_for_job_done
from .scan_execution import ScanBatch
from .storage import StorageAllocator, StorageHandle

__all__ = ["HandleFaultError", "HandleTable", "HandleTableError"]


def _yielding(seconds: float | None) -> Callable[[], None] | None:
    """The poll callback that sleeps ``seconds`` between status reads, or
    None for the spinning default."""
    if seconds is None:
        return None
    return lambda: time.sleep(seconds)


class HandleTableError(DauDriverError):
    """A handle-table operation the host refused to perform."""


class HandleFaultError(PipelineJobError):
    """The shell REFUSED to start a job on the handle it was armed with.

    A :class:`PipelineJobError` because it is one -- a caller that catches
    every job failure still catches this -- but its own type because the whole
    value of the table is that this case is distinguishable. The job did not
    run: the reader was never started, so nothing was read, nothing was
    written, and no result register moved. ``code`` says which of the three
    refusals it was."""

    def __init__(self, code: int, *, handle_id: int, generation: int, label: str) -> None:
        super().__init__(
            f"{label}: the shell refused handle {handle_id} generation {generation} "
            f"({HandleFaultCode(code).name}, 0x{code:02X}): {_FAULT_REASONS[code]}. "
            "The job did not run -- nothing was read.",
            last_error=code,
        )
        self.code = HandleFaultCode(code)
        self.handle_id = handle_id
        self.generation = generation


# the codes a REFUSAL can carry, and what each one means to a caller. Doubles
# as the membership test that separates a refused job from every other job
# error: a code outside this set came from the datapath, and the datapath ran.
_FAULT_REASONS = {
    int(HandleFaultCode.BAD_ID): "the table has no such slot; capacity is planned at build time, not grown at run time",
    int(HandleFaultCode.STALE): "the slot is freed or live at another generation -- that region belongs to someone else now",
    int(HandleFaultCode.OUT_OF_BOUNDS): "the job asks for more bytes than the grant",
}


class HandleTable:
    """One allocator's slots, mirrored into one device's handle table.

    ``capacity`` is the built shell's ``handle_table_capacity``. It is not read
    off the device: a table is hardware, planned into the design and priced
    with it, and there is no register that advertises it -- an id past the end
    is a fault (``BAD_ID``), not a discovery. Passing the design's number here
    turns that fault into a refusal on the host, where the mistake still costs
    nothing.
    """

    def __init__(self, device, allocator: StorageAllocator, *, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive; a shell with no table has nothing to bind, got {capacity}")
        self.device = device
        self.allocator = allocator
        self.capacity = capacity

    def install(self, handle: StorageHandle) -> None:
        """Program ``handle``'s grant into slot ``handle.id``.

        The allocator resolves the handle first and that resolution is the
        authority: base, length and generation are taken from the LIVE record,
        so a handle the host no longer believes in never becomes a grant the
        device does. Raises :class:`~dau_driver.storage.StaleHandleError` if it
        will not resolve -- that is a host bug, and papering over it would
        install exactly the stale grant the table exists to refuse.
        """
        base = self.allocator.resolve(handle)  # raises on stale, forged, or foreign
        self._check_slot(handle.id)
        if base % DMA_ALIGN_BYTES:
            # the same refusal run_resident_scan makes about a raw address, at
            # the point a base becomes a grant: an unaligned region is a
            # silent-wrong-bytes failure on silicon, and an allocator built at
            # a finer alignment reaches it without ever writing an address down
            raise HandleTableError(f"handle {handle.id} is based at 0x{base:X}, which is not {DMA_ALIGN_BYTES}-byte aligned")
        self.device.write_register(HandleTableRegisterOffset.INDEX, handle.id)
        # both halves, always: a slot left holding the previous entry's high
        # half grants a region in the wrong 4 GiB
        write_address(self.device, HandleTableRegisterOffset.BASE_LOW, HandleTableRegisterOffset.BASE_HIGH, base)
        self.device.write_register(HandleTableRegisterOffset.LENGTH, handle.capacity)
        self.device.write_register(HandleTableRegisterOffset.GENERATION, handle.generation)
        self.device.write_register(HandleTableRegisterOffset.CONTROL, int(HandleTableControlFlag.INSTALL))

    def free(self, handle: StorageHandle) -> None:
        """Drop slot ``handle.id`` out of liveness, so nothing resolves through
        it until it is installed again.

        Deliberately unconditional on the allocator: freeing a slot can only
        make the table refuse MORE, so it is safe whether or not the host has
        released the span yet. A version that demanded a live handle would
        refuse the call from a caller who freed host-side first, and leave the
        device answering for a region that has already been handed on.
        """
        self._check_slot(handle.id)
        self.device.write_register(HandleTableRegisterOffset.INDEX, handle.id)
        self.device.write_register(HandleTableRegisterOffset.CONTROL, int(HandleTableControlFlag.FREE))

    def arm(self, handle: StorageHandle) -> None:
        """Name the handle the NEXT job resolves, in place of an input address.

        Checked against nothing on purpose. A stale handle has to reach the
        table or the table is never what refused it -- and a host that
        pre-validates here proves only that the host agrees with itself.
        """
        self.device.write_register(HandleTableRegisterOffset.JOB_HANDLE_ID, handle.id)
        self.device.write_register(HandleTableRegisterOffset.JOB_HANDLE_GENERATION, handle.generation)

    def run_resident_scan(
        self,
        handle: StorageHandle,
        *,
        row_count: int,
        row_bytes: int,
        collect,
        repeats: int = 1,
        timeout_seconds: float = 60.0,
        poll_yield_seconds: float | None = None,
    ) -> list:
        """:func:`~dau_driver.scan_execution.run_resident_scan` with the raw
        address replaced by a handle: arm once, then run the job ``repeats``
        times over rows already on the device, uploading nothing.

        ``poll_yield_seconds`` sleeps that long between JOB_STATUS polls
        instead of spinning. Measured on dpv1 (2026-09-27) it is the wrong
        default for a Python caller: any sleep, ``0`` included, hands the
        GIL to another thread and the poll waits out a full switch interval
        per iteration, milliseconds on a job that runs for less; the
        contention it was meant to
        relieve was elsewhere. It stays for a caller whose other threads do
        not hold the GIL.

        Returns one ``collect(device)`` per repeat. Raises
        :class:`HandleFaultError` the moment the shell refuses -- results for
        the repeats that did run are dropped with it, because a run whose
        dataset stopped being the caller's part way through is not a partial
        answer.
        """
        if row_count <= 0:
            raise ValueError(f"row_count must be positive, got {row_count}")
        if row_bytes <= 0:
            raise ValueError(f"row_bytes must be positive, got {row_bytes}")
        if repeats <= 0:
            raise ValueError(f"repeats must be positive, got {repeats}")
        length = row_count * row_bytes
        if length % DMA_ALIGN_BYTES:
            # the shell's own 0xFE length gate fires before the table is even
            # asked, so an off-grid length would arrive as a fault that says
            # nothing about the handle
            raise ValueError(f"{length} bytes is not a multiple of the {DMA_ALIGN_BYTES}-byte DMA grid")

        self.arm(handle)
        results = []
        for index in range(repeats):
            self.device.write_register(RegisterOffset.INPUT_LENGTH_LOW, length)
            self.device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))
            label = f"handle scan job {index}"
            try:
                _wait_for_job_done(self.device, timeout_seconds=timeout_seconds, label=label, poll=_yielding(poll_yield_seconds))
            except PipelineJobError as exc:
                if exc.last_error in _FAULT_REASONS:
                    raise HandleFaultError(exc.last_error, handle_id=handle.id, generation=handle.generation, label=label) from exc
                raise
            results.append(collect(self.device))
        return results

    def run_scan_batches(
        self,
        batches: Iterable[ScanBatch],
        handles: tuple[StorageHandle, StorageHandle],
        *,
        row_bytes: int,
        collect: Callable[[Any], Any],
        timeout_seconds: float = 60.0,
        progress: Callable[[int], None] | None = None,
        poll_yield_seconds: float | None = None,
    ) -> list[Any]:
        """:func:`~dau_driver.scan_execution.run_scan_batches` with the two
        raw input regions replaced by two installed handles: batch ``i + 1``
        is staged into the alternate handle's grant (and, for a lazy
        iterable, produced) while job ``i`` runs against the other. Returns
        one ``collect(device)`` per batch, in batch order.

        On a handle-table shell ``INPUT_ADDRESS`` feeds nothing, so this is
        the only way streamed rows reach a job, and every batch goes through
        the allocator's ``stage`` -- a grant that stopped being the caller's
        refuses the bytes before they move, and the device refuses the job
        if the slot moved under it. A batch's length must sit on the DMA
        grid: the shell's length gate fires before the table is asked, so an
        off-grid batch is refused here, where the message can say so.

        A batch that fails to stage (a grant released mid-stream, a payload
        past its grant) surfaces from the running job's poll, after that job
        has been allowed to finish: the grants are the caller's to free once
        this returns or raises, and a job still reading one must not be.
        """
        first, second = handles
        if first.id == second.id:
            raise ValueError(f"the two handles must name two slots; both are slot {first.id}")
        if row_bytes <= 0:
            raise ValueError(f"row_bytes must be positive, got {row_bytes}")
        iterator = iter(batches)
        pair = (first, second)

        def stage(index: int) -> tuple[StorageHandle, int] | None:
            try:
                batch = next(iterator)
            except StopIteration:
                return None
            if batch.row_count <= 0:
                raise ValueError(f"batch {index}: zero rows; a scan job needs a non-empty batch")
            length = batch.row_count * row_bytes
            if len(batch.payload) != length:
                raise ValueError(f"batch {index}: payload of {len(batch.payload)} bytes is not row_count {batch.row_count} * row_bytes {row_bytes}")
            if length % DMA_ALIGN_BYTES:
                raise ValueError(f"batch {index}: {length} bytes is not a multiple of the {DMA_ALIGN_BYTES}-byte DMA grid")
            handle = pair[index % 2]
            self.allocator.stage(self.device, handle, batch.payload)
            return handle, batch.row_count

        results: list[Any] = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(stage, 0)
            index = 0
            while True:
                staged = pending.result()
                if staged is None:
                    break
                handle, row_count = staged
                self.arm(handle)
                self.device.write_register(RegisterOffset.INPUT_LENGTH_LOW, row_count * row_bytes)
                self.device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))
                pending = pool.submit(stage, index + 1)

                def surface_staging_failure(future=pending):
                    failure = future.exception() if future.done() else None
                    if failure is not None:
                        raise failure
                    if poll_yield_seconds is not None:
                        time.sleep(poll_yield_seconds)

                label = f"handle scan job {index}"
                try:
                    _wait_for_job_done(self.device, timeout_seconds=timeout_seconds, label=label, poll=surface_staging_failure)
                except PipelineJobError as exc:
                    if exc.last_error in _FAULT_REASONS:
                        raise HandleFaultError(exc.last_error, handle_id=handle.id, generation=handle.generation, label=label) from exc
                    raise
                except Exception:
                    # the staging worker failed while job `index` was still
                    # reading its grant. Let that job finish before the
                    # failure escapes, or a caller freeing the grants on the
                    # way out hands the device's live read to the next tenant
                    _wait_for_job_done(self.device, timeout_seconds=timeout_seconds, label=f"{label} (draining after a staging failure)")
                    raise
                results.append(collect(self.device))
                if progress is not None:
                    progress(index + 1)
                index += 1
        return results

    def _check_slot(self, handle_id: int) -> None:
        if not 0 <= handle_id < self.capacity:
            raise HandleTableError(
                f"handle {handle_id} names no slot of a {self.capacity}-slot table; "
                "a table is built at a capacity and refuses past it, so size the design for the "
                "concurrently live allocations the personality must hold"
            )
