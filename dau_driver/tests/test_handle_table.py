"""The host end of the handle table, against a transport that models the
shell's.

``dau_build.tests.test_handle_table_sim`` asserts these same four claims from
inside the RTL, driving the table's ports directly. This asserts them from the
other end of the wire: the only thing the host may do is write the aperture,
and a live handle has to resolve, a freed one has to be refused, a recycled
slot has to refuse the previous generation, and an unarmed job has to be
refused -- all of it produced by the register sequence the driver actually
emits, not by a host-side check standing in for the device.
"""

from __future__ import annotations

import pytest
from dau_core.registers import (
    HandleFaultCode,
    HandleTableControlFlag,
    HandleTableRegisterOffset,
    JobControlFlag,
    JobStatusFlag,
    RegisterOffset,
)

from dau_driver.handle_table import HandleFaultError, HandleTable, HandleTableError
from dau_driver.pipeline_execution import PipelineJobError
from dau_driver.storage import StaleHandleError, StorageAllocator
from dau_driver.testing import FakeTransport, fake_device

CAPACITY = 4
ROW_BYTES = 8
ROWS = 8


class HandleTableTransport(FakeTransport):
    """``dau_shell_handle_table`` plus the shell's job close-out, in Python.

    Entries live exactly as the RTL's do -- a live bit, a base, a length, a
    generation -- and a job start resolves ``JOB_HANDLE_*`` against them before
    anything else happens. A refused job sets ERROR with the fault code in
    LAST_ERROR and records NO run, which is how a test can tell a refusal from
    a job that ran and produced nothing.
    """

    def __init__(self, capacity: int = CAPACITY, *, datapath_error: int = 0) -> None:
        super().__init__()
        self.capacity = capacity
        self.entries: dict[int, tuple[int, int, int]] = {}  # slot -> (base, length, generation)
        self.runs: list[tuple[int, int]] = []  # (base, length) of every job that actually read
        # a tile's own error code, reported by a job that RESOLVED and ran: the
        # shell's error mux ranks a refusal above it, so it can only be seen
        # once the handle was good
        self.datapath_error = datapath_error

    def write_u32(self, offset: int, value: int) -> None:
        super().write_u32(offset, value)
        if offset == int(HandleTableRegisterOffset.CONTROL):
            self._commit(value)
        elif offset == int(RegisterOffset.JOB_CONTROL) and value & int(JobControlFlag.START):
            self._start()

    def read_u32(self, offset: int) -> int:
        if offset == int(RegisterOffset.JOB_STATUS):
            status = int(JobStatusFlag.DONE)
            return status | int(JobStatusFlag.ERROR) if self.registers.get(int(RegisterOffset.LAST_ERROR)) else status
        return super().read_u32(offset)

    def _staged(self) -> tuple[int, int, int, int]:
        base = self.registers.get(int(HandleTableRegisterOffset.BASE_LOW), 0) | (
            self.registers.get(int(HandleTableRegisterOffset.BASE_HIGH), 0) << 32
        )
        return (
            self.registers.get(int(HandleTableRegisterOffset.INDEX), 0),
            base,
            self.registers.get(int(HandleTableRegisterOffset.LENGTH), 0),
            self.registers.get(int(HandleTableRegisterOffset.GENERATION), 0),
        )

    def _commit(self, control: int) -> None:
        index, base, length, generation = self._staged()
        if index >= self.capacity:
            return  # out of range: the RTL programs nothing
        if control & int(HandleTableControlFlag.FREE):  # free outranks install
            self.entries.pop(index, None)
        elif control & int(HandleTableControlFlag.INSTALL):
            self.entries[index] = (base, length, generation)

    def _start(self) -> None:
        handle_id = self.registers.get(int(HandleTableRegisterOffset.JOB_HANDLE_ID), 0)
        generation = self.registers.get(int(HandleTableRegisterOffset.JOB_HANDLE_GENERATION), 0)
        length = self.registers.get(int(RegisterOffset.INPUT_LENGTH_LOW), 0)
        entry = self.entries.get(handle_id)
        if handle_id >= self.capacity:
            fault = HandleFaultCode.BAD_ID
        elif entry is None or entry[2] != generation:
            fault = HandleFaultCode.STALE
        elif length > entry[1]:
            fault = HandleFaultCode.OUT_OF_BOUNDS
        else:
            self.registers[int(RegisterOffset.LAST_ERROR)] = self.datapath_error
            self.runs.append((entry[0], length))
            return
        self.registers[int(RegisterOffset.LAST_ERROR)] = int(fault)


def _table(tmp_path, transport=None, *, capacity=CAPACITY, tier_bytes=1 << 20):
    transport = transport if transport is not None else HandleTableTransport(capacity)
    device = fake_device(tmp_path, transport)
    allocator = StorageAllocator(tier="ddr-onboard", capacity_bytes=tier_bytes)
    return HandleTable(device, allocator, capacity=capacity), allocator, transport


def _run(table, handle, **kwargs):
    return table.run_resident_scan(handle, row_count=ROWS, row_bytes=ROW_BYTES, collect=lambda _d: "collected", **kwargs)


def test_a_live_handle_resolves_to_its_region(tmp_path) -> None:
    """The baseline the refusals are measured against: the allocator's grant
    is programmed into the slot, the job names the handle instead of an
    address, and the read lands on the region the allocator handed out."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(ROWS * ROW_BYTES)
    table.install(handle)

    assert _run(table, handle) == ["collected"]
    assert transport.runs == [(handle.base, ROWS * ROW_BYTES)]
    assert transport.h2c_writes == [], "a resident run stages nothing"
    # the job named a handle, not an address: INPUT_ADDRESS was never written
    assert int(RegisterOffset.INPUT_ADDRESS_LOW) not in transport.registers


def test_a_freed_handle_is_refused(tmp_path) -> None:
    """Freeing the slot stops it resolving even though the memory is untouched
    and the base the handle used to mean still reads fine."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(ROWS * ROW_BYTES)
    table.install(handle)
    table.free(handle)

    with pytest.raises(HandleFaultError) as raised:
        _run(table, handle)
    assert raised.value.code is HandleFaultCode.STALE
    assert transport.runs == [], "a refused job must not have read anything"


def test_a_recycled_slot_refuses_the_previous_generation(tmp_path) -> None:
    """THE CASE THE TABLE EXISTS FOR. The allocator recycles the id, so the
    second allocation lands in the same SLOT over a different region. The first
    caller's handle is byte-for-byte what it always was and its base still
    reads -- the generation is the only thing that can refuse it. It does, and
    the reissued handle reaches the new tenant's region, proving the slot was
    really reused underneath."""
    table, allocator, transport = _table(tmp_path)
    first = allocator.allocate(ROWS * ROW_BYTES)
    table.install(first)
    table.free(first)
    allocator.free(first)
    second = allocator.allocate(ROWS * ROW_BYTES)
    assert second.id == first.id and second.generation != first.generation
    table.install(second)

    with pytest.raises(HandleFaultError) as raised:
        _run(table, first)
    assert raised.value.code is HandleFaultCode.STALE
    assert transport.runs == []

    assert _run(table, second) == ["collected"]
    assert transport.runs == [(second.base, ROWS * ROW_BYTES)]


def test_an_unarmed_job_handle_is_refused(tmp_path) -> None:
    """Generation zero is one the allocator never issues, so a host that
    starts a job without arming a handle is refused rather than quietly
    reading slot zero -- which is live here, and would otherwise answer."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(ROWS * ROW_BYTES)
    table.install(handle)
    assert handle.id == 0 and transport.entries[0][2] == handle.generation

    device = table.device
    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, ROWS * ROW_BYTES)
    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))
    assert transport.registers[int(RegisterOffset.LAST_ERROR)] == int(HandleFaultCode.STALE)
    assert transport.runs == []


def test_a_job_longer_than_the_grant_is_refused(tmp_path) -> None:
    """The grant bounds the read: asking past it would walk into whatever
    follows the region, the same wrong answer by a different route."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(ROWS * ROW_BYTES)
    table.install(handle)

    with pytest.raises(HandleFaultError) as raised:
        table.run_resident_scan(handle, row_count=ROWS * 2, row_bytes=ROW_BYTES, collect=lambda _d: None)
    assert raised.value.code is HandleFaultCode.OUT_OF_BOUNDS
    assert transport.runs == []


def test_an_id_past_the_table_is_refused_before_the_device_sees_it(tmp_path) -> None:
    """A slot the built table does not have is a fault on silicon (BAD_ID) and
    a refusal here, because capacity is a design fact the host was told, not
    one it discovers -- and the cheapest place to learn a personality is too
    small is before the run."""
    table, allocator, _ = _table(tmp_path, capacity=2, tier_bytes=1 << 20)
    for _ in range(3):
        handle = allocator.allocate(64)
    assert handle.id >= 2
    with pytest.raises(HandleTableError, match="names no slot"):
        table.install(handle)


def test_installing_a_stale_handle_is_a_host_bug_not_a_device_refusal(tmp_path) -> None:
    """The host allocator's generation is the authority for what a slot is
    programmed from. Installing a handle it will not resolve would teach the
    device a grant the host does not believe in -- the one direction the
    generation check cannot catch, since the device would then agree."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(4096)
    allocator.free(handle)
    with pytest.raises(StaleHandleError):
        table.install(handle)
    assert transport.entries == {}, "nothing may be programmed from a handle the allocator refuses"


def test_freeing_a_slot_works_after_the_allocator_released_the_span(tmp_path) -> None:
    """Freeing a slot can only make the table refuse more, so it must not
    depend on the allocator still holding the handle: a caller who released
    host-side first would otherwise be unable to close the device-side hole at
    all, which is the exposure inverted."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(4096)
    table.install(handle)
    allocator.free(handle)
    table.free(handle)
    assert transport.entries == {}


def test_the_install_sequence_stages_the_whole_grant_before_committing(tmp_path) -> None:
    """A commit is a pulse over whatever is staged, so every field has to be
    written first. A half-staged entry commits a grant made of one allocation's
    base and another's length."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(4096)
    table.install(handle)

    assert transport.registers[int(HandleTableRegisterOffset.INDEX)] == handle.id
    assert transport.registers[int(HandleTableRegisterOffset.BASE_LOW)] == handle.base & 0xFFFF_FFFF
    assert transport.registers[int(HandleTableRegisterOffset.BASE_HIGH)] == handle.base >> 32
    assert transport.registers[int(HandleTableRegisterOffset.LENGTH)] == handle.capacity
    assert transport.registers[int(HandleTableRegisterOffset.GENERATION)] == handle.generation
    assert transport.entries[handle.id] == (handle.base, handle.capacity, handle.generation)


def test_a_datapath_error_is_not_reported_as_a_handle_fault(tmp_path) -> None:
    """The refusal type is only worth having if it means what it says: a code
    from a tile is a job that RAN, and calling that a stale handle would send
    a caller looking for an allocation bug that is not there."""
    transport = HandleTableTransport(datapath_error=0x03)  # a tile code
    table, allocator, _ = _table(tmp_path, transport)
    handle = allocator.allocate(ROWS * ROW_BYTES)
    table.install(handle)

    with pytest.raises(PipelineJobError) as raised:
        _run(table, handle)
    assert not isinstance(raised.value, HandleFaultError)
    assert raised.value.last_error == 0x03
    assert transport.runs, "a datapath error means the job ran, which is the whole distinction"


def test_repeats_amortize_one_staging_over_many_jobs(tmp_path) -> None:
    """The residency claim itself, through a handle: the rows are staged once
    and the job runs against the same grant repeatedly, arming it once."""
    table, allocator, transport = _table(tmp_path)
    handle = allocator.allocate(ROWS * ROW_BYTES)
    allocator.stage(table.device, handle, bytes(ROWS * ROW_BYTES))
    table.install(handle)

    assert _run(table, handle, repeats=3) == ["collected"] * 3
    assert len(transport.h2c_writes) == 1, "one staging, three jobs"
    assert transport.runs == [(handle.base, ROWS * ROW_BYTES)] * 3


def test_an_off_grid_length_is_refused_before_it_becomes_a_length_gate_fault(tmp_path) -> None:
    """The shell's 0xFE length gate fires before the table is asked, so an
    off-grid length would arrive as a fault that says nothing about the
    handle."""
    table, allocator, _ = _table(tmp_path)
    handle = allocator.allocate(4096)
    table.install(handle)
    with pytest.raises(ValueError, match="DMA grid"):
        table.run_resident_scan(handle, row_count=3, row_bytes=ROW_BYTES, collect=lambda _d: None)


def test_an_unaligned_grant_never_becomes_a_slot(tmp_path) -> None:
    """A finer-grained allocator can hand out a base the DMA path cannot read
    from, and through a handle nobody ever writes that address down to notice.
    Refuse it where the mistake is still cheap, as the raw-address runner
    does."""
    transport = HandleTableTransport()
    device = fake_device(tmp_path, transport)
    allocator = StorageAllocator(tier="ddr-onboard", capacity_bytes=1 << 20, align=8)
    table = HandleTable(device, allocator, capacity=CAPACITY)
    allocator.allocate(8)
    handle = allocator.allocate(64)
    assert handle.base % 64
    with pytest.raises(HandleTableError, match="aligned"):
        table.install(handle)
    assert transport.entries == {}


def test_a_capacity_of_zero_is_not_a_table(tmp_path) -> None:
    _, allocator, transport = _table(tmp_path)
    with pytest.raises(ValueError, match="capacity must be positive"):
        HandleTable(fake_device(tmp_path, transport), allocator, capacity=0)
