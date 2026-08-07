"""Owning tests for the pipeline job runners and their struct decoders.

The runners are driven through the in-memory ``FakeTransport`` (a
dict-backed register map plus DMA payloads) so the wait/read control flow
and the record struct layouts are guarded here directly, not only
indirectly through the dau-polars/cocotb suites. No hardware.
"""

from __future__ import annotations

import struct

import pytest
from types import SimpleNamespace
from dau_core.operators import BarAggregate, GroupAggregate
from dau_core.registers import (
    RegisterMapVersion,
    JobStatusFlag,
    NocLaneRegisterOffset,
    RegisterOffset,
    StreamJobRegisterContract,
    noc_lane_register,
)

from dau_driver.pipeline_execution import (
    BAR_RECORD_BYTES,
    GROUP_RECORD_BYTES,
    PipelineJobError,
    bar_noc_lane_output_address,
    decode_bar_records,
    decode_group_records,
    execute_bar_job,
    execute_bar_noc_packed,
    execute_keyed_pipeline_job,
    finish_bar_ddr_job,
    pack_bar_rows,
    pack_keyed_rows,
)
from dau_driver.testing import FakeTransport, fake_device


# The board's staging regions, which are platform configuration in
# production (dau_build.platforms.DdrStaging). The driver reads attributes
# rather than importing that model, so a test stands in with the same shape.
_STAGING = SimpleNamespace(
    input_addresses=(0x0000_0000, 0x1800_0000),
    input_bytes=0x1800_0000,
    output_address=0x3000_0000,
    output_bytes=0x1000_0000,
    address_align=0x1000,
)
BAR_DDR_OUTPUT_ADDRESS = _STAGING.output_address

def _pad64(payload: bytes) -> bytes:
    return payload + b"\x00" * ((-len(payload)) % 64)


def _pack_group_records(groups: list[GroupAggregate]) -> bytes:
    """Golden encoder mirroring the documented 40-byte group record layout
    (``<iiiiiiqQ``: key, first, last, min, max, pad, total, count)."""
    return b"".join(struct.pack("<iiiiiiqQ", g.key, g.first, g.last, g.minimum, g.maximum, 0, g.total, g.count) for g in groups)


def _pack_bar_records(bars: list[BarAggregate]) -> bytes:
    """Golden encoder mirroring the documented 48-byte bar record layout
    (``<iiiiiiqqQ``: key, open, close, low, high, pad, shares, frac,
    count)."""
    return b"".join(struct.pack("<iiiiiiqqQ", b.key, b.open, b.close, b.low, b.high, 0, b.shares, b.frac, b.count) for b in bars)


def _stage_done(transport: FakeTransport, *, result_length: int, count: int, selected: int | None = None) -> None:
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    transport.registers[int(RegisterOffset.RESULT_LENGTH_LOW)] = result_length
    transport.registers[int(RegisterOffset.PIPELINE_GROUP_COUNT_LOW)] = count
    if selected is not None:
        transport.registers[int(RegisterOffset.PIPELINE_SELECTED_COUNT_LOW)] = selected


GROUPS = [
    GroupAggregate(key=1, first=10, last=40, minimum=-5, maximum=99, total=144, count=4),
    GroupAggregate(key=-2, first=7, last=7, minimum=7, maximum=7, total=7, count=1),
]
BARS = [
    BarAggregate(key=1, open=10, close=40, low=-5, high=99, shares=-1000, frac=250, count=4),
    BarAggregate(key=-2, open=7, close=7, low=7, high=7, shares=3, frac=0, count=1),
]


def test_group_record_round_trip():
    raw = _pack_group_records(GROUPS)
    assert len(raw) == len(GROUPS) * GROUP_RECORD_BYTES
    assert decode_group_records(raw, len(GROUPS)) == GROUPS


def test_bar_record_round_trip():
    raw = _pack_bar_records(BARS)
    assert len(raw) == len(BARS) * BAR_RECORD_BYTES
    assert decode_bar_records(raw, len(BARS)) == BARS


def test_execute_bar_job_done(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    contract = StreamJobRegisterContract()
    transport.c2h_payloads[contract.output_buffer_address] = _pad64(_pack_bar_records(BARS))
    _stage_done(transport, result_length=len(BARS) * BAR_RECORD_BYTES, count=len(BARS))

    rows = [(1, 10, 100, 250), (-2, 7, 3, 0)]
    result = execute_bar_job(device, rows)

    assert result == BARS
    # input was staged verbatim through the packer at the input buffer
    assert transport.h2c_writes == [(contract.input_buffer_address, pack_bar_rows(rows))]


def test_execute_bar_job_empty(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    assert execute_bar_job(device, []) == []
    assert transport.h2c_writes == []


def test_execute_bar_job_no_records(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _stage_done(transport, result_length=0, count=0)
    # no C2H payload staged: a zero-record job must not issue a read
    assert execute_bar_job(device, [(1, 2, 3, 4)]) == []
    assert transport.c2h_reads == []


def test_execute_bar_job_error(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE | JobStatusFlag.ERROR)
    transport.registers[int(RegisterOffset.LAST_ERROR)] = 0xDEAD
    with pytest.raises(PipelineJobError) as exc:
        execute_bar_job(device, [(1, 2, 3, 4)])
    assert str(exc.value) == "bar job failed: status=0xc last_error=57005"


def test_execute_bar_job_result_length_mismatch(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _stage_done(transport, result_length=BAR_RECORD_BYTES - 1, count=1)
    with pytest.raises(PipelineJobError) as exc:
        execute_bar_job(device, [(1, 2, 3, 4)])
    assert str(exc.value) == "result length 47 inconsistent with bar count 1"
    assert transport.c2h_reads == []


def test_execute_bar_job_timeout(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.BUSY)
    with pytest.raises(PipelineJobError) as exc:
        execute_bar_job(device, [(1, 2, 3, 4)], timeout_seconds=-1.0)
    assert str(exc.value) == "bar job timed out (status=0x2)"


def test_execute_keyed_pipeline_job_done(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    contract = StreamJobRegisterContract()
    transport.c2h_payloads[contract.output_buffer_address] = _pad64(_pack_group_records(GROUPS))
    _stage_done(transport, result_length=len(GROUPS) * GROUP_RECORD_BYTES, count=len(GROUPS), selected=42)

    rows = [(1, 10), (-2, 7)]
    groups, selected = execute_keyed_pipeline_job(device, rows)

    assert groups == GROUPS
    assert selected == 42
    assert transport.h2c_writes == [(contract.input_buffer_address, pack_keyed_rows(rows))]
    # register-read order: poll JOB_STATUS, then result length, selected, group count
    assert transport.register_reads == [
        int(RegisterOffset.JOB_STATUS),
        int(RegisterOffset.RESULT_LENGTH_LOW),
        int(RegisterOffset.PIPELINE_SELECTED_COUNT_LOW),
        int(RegisterOffset.PIPELINE_GROUP_COUNT_LOW),
    ]


def test_execute_keyed_pipeline_job_empty(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    assert execute_keyed_pipeline_job(device, []) == ([], 0)


def test_execute_keyed_pipeline_job_no_groups(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _stage_done(transport, result_length=0, count=0, selected=9)
    groups, selected = execute_keyed_pipeline_job(device, [(1, 10)])
    assert groups == []
    assert selected == 9
    assert transport.c2h_reads == []


def test_execute_keyed_pipeline_job_error(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE | JobStatusFlag.ERROR)
    transport.registers[int(RegisterOffset.LAST_ERROR)] = 3
    with pytest.raises(PipelineJobError) as exc:
        execute_keyed_pipeline_job(device, [(1, 10)])
    assert str(exc.value) == "pipeline job failed: status=0xc last_error=3"
    # error path reads JOB_STATUS then LAST_ERROR, and no result registers
    assert transport.register_reads == [int(RegisterOffset.JOB_STATUS), int(RegisterOffset.LAST_ERROR)]


def test_execute_keyed_pipeline_job_result_length_mismatch(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _stage_done(transport, result_length=GROUP_RECORD_BYTES + 1, count=1, selected=1)
    with pytest.raises(PipelineJobError) as exc:
        execute_keyed_pipeline_job(device, [(1, 10)])
    assert str(exc.value) == "result length 41 inconsistent with group count 1"


def test_finish_bar_ddr_job_done(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    transport.c2h_payloads[BAR_DDR_OUTPUT_ADDRESS] = _pad64(_pack_bar_records(BARS))
    _stage_done(transport, result_length=len(BARS) * BAR_RECORD_BYTES, count=len(BARS))

    raw, count = finish_bar_ddr_job(device, staging=_STAGING)

    assert count == len(BARS)
    assert decode_bar_records(raw, count) == BARS
    assert transport.c2h_reads == [(BAR_DDR_OUTPUT_ADDRESS, 128)]


def test_finish_bar_ddr_job_empty(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _stage_done(transport, result_length=0, count=0)
    assert finish_bar_ddr_job(device, staging=_STAGING) == (b"", 0)
    assert transport.c2h_reads == []


def test_finish_bar_ddr_job_result_length_mismatch(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _stage_done(transport, result_length=1, count=1)
    with pytest.raises(PipelineJobError) as exc:
        finish_bar_ddr_job(device, staging=_STAGING)
    assert str(exc.value) == "result length 1 inconsistent with bar count 1"


def _advertise_lane_count(transport: FakeTransport, lanes: int) -> None:
    """A bar-NoC shell publishes its lane count; the host reads it rather
    than assuming one, so a fake standing in for that shell must too."""
    transport.registers[int(RegisterOffset.REGISTER_MAP_VERSION)] = RegisterMapVersion(0, 2).to_u32()
    transport.registers[int(RegisterOffset.LANE_COUNT)] = lanes


def _stage_noc_lane(transport: FakeTransport, lane: int, *, records: bytes, count: int, record_bytes: int, lanes: int = 4) -> None:
    transport.registers[noc_lane_register(lane, NocLaneRegisterOffset.RESULT_LENGTH_LOW)] = count * record_bytes
    transport.registers[noc_lane_register(lane, NocLaneRegisterOffset.RECORD_COUNT_LOW)] = count
    if count:
        transport.c2h_payloads[bar_noc_lane_output_address(_STAGING, lane, lanes)] = _pad64(records)


def test_execute_bar_noc_packed_done(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _advertise_lane_count(transport, 4)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    # lane 0 gets both bars, lanes 1-3 are empty partitions
    _stage_noc_lane(transport, 0, records=_pack_bar_records(BARS), count=len(BARS), record_bytes=BAR_RECORD_BYTES)
    for lane in (1, 2, 3):
        _stage_noc_lane(transport, lane, records=b"", count=0, record_bytes=BAR_RECORD_BYTES)

    rows = [(1, 10, 100, 250), (-2, 7, 3, 0)]
    results = execute_bar_noc_packed(device, pack_bar_rows(rows), len(rows), staging=_STAGING)

    assert len(results) == 4
    assert decode_bar_records(results[0][0], results[0][1]) == BARS
    assert results[1:] == [(b"", 0), (b"", 0), (b"", 0)]


def test_execute_bar_noc_packed_scan_shell_reuse(tmp_path):
    """The runner is reused for a key-partitioned scan shell: 2 lanes and a
    40-byte group record, decoded with the group decoder."""
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    _stage_noc_lane(transport, 0, records=_pack_group_records(GROUPS[:1]), count=1, record_bytes=GROUP_RECORD_BYTES, lanes=2)
    _stage_noc_lane(transport, 1, records=_pack_group_records(GROUPS[1:]), count=1, record_bytes=GROUP_RECORD_BYTES, lanes=2)

    rows = [(1, 10, 100, 250)]
    results = execute_bar_noc_packed(device, pack_bar_rows(rows), len(rows), staging=_STAGING, lanes=2, record_bytes=GROUP_RECORD_BYTES)

    assert len(results) == 2
    assert decode_group_records(results[0][0], results[0][1]) == GROUPS[:1]
    assert decode_group_records(results[1][0], results[1][1]) == GROUPS[1:]


def test_execute_bar_noc_packed_q6_aggregate_lanes(tmp_path):
    """q6's aggregate shell latches an input row count in RECORD_COUNT_LOW
    and emits one fixed 16-byte [sum, count] record per live lane, so the
    ``result_length == count * record_bytes`` invariant does not hold; the
    runner reuses this shell with ``validate_record_count=False``."""
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _advertise_lane_count(transport, 4)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    record0 = struct.pack("<qq", 123_456, 3)  # [sum, aggregated_count]
    record3 = struct.pack("<qq", -50, 5)
    # lane 0 aggregated 3 rows, lane 3 aggregated 5, lanes 1-2 selected none
    transport.registers[noc_lane_register(0, NocLaneRegisterOffset.RESULT_LENGTH_LOW)] = 16
    transport.registers[noc_lane_register(0, NocLaneRegisterOffset.RECORD_COUNT_LOW)] = 3
    transport.c2h_payloads[bar_noc_lane_output_address(_STAGING, 0, 4)] = _pad64(record0)
    transport.registers[noc_lane_register(3, NocLaneRegisterOffset.RESULT_LENGTH_LOW)] = 16
    transport.registers[noc_lane_register(3, NocLaneRegisterOffset.RECORD_COUNT_LOW)] = 5
    transport.c2h_payloads[bar_noc_lane_output_address(_STAGING, 3, 4)] = _pad64(record3)

    results = execute_bar_noc_packed(device, pack_bar_rows([(1, 2, 3, 4)]), 1, staging=_STAGING, record_bytes=16, validate_record_count=False)

    assert len(results) == 4
    assert results[0][0][:16] == record0
    assert results[0][1] == 3
    assert results[1:3] == [(b"", 0), (b"", 0)]
    assert results[3][0][:16] == record3
    assert results[3][1] == 5


def test_execute_bar_noc_packed_q6_shape_rejected_by_default_validation(tmp_path):
    """The default (bar/q1) validation must reject the q6 count/length
    shape, proving ``validate_record_count`` is load-bearing."""
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _advertise_lane_count(transport, 4)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    transport.registers[noc_lane_register(0, NocLaneRegisterOffset.RESULT_LENGTH_LOW)] = 16
    transport.registers[noc_lane_register(0, NocLaneRegisterOffset.RECORD_COUNT_LOW)] = 3
    with pytest.raises(PipelineJobError) as exc:
        execute_bar_noc_packed(device, pack_bar_rows([(1, 2, 3, 4)]), 1, staging=_STAGING, record_bytes=16)
    assert str(exc.value) == "lane 0: result length 16 inconsistent with record count 3"


def test_execute_bar_noc_packed_empty(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _advertise_lane_count(transport, 4)
    assert execute_bar_noc_packed(device, b"", 0, staging=_STAGING) == [(b"", 0)] * 4
    assert execute_bar_noc_packed(device, b"", 0, staging=_STAGING, lanes=2) == [(b"", 0)] * 2
    assert transport.h2c_writes == []


def test_execute_bar_noc_packed_lane_mismatch(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _advertise_lane_count(transport, 4)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE)
    transport.registers[noc_lane_register(0, NocLaneRegisterOffset.RESULT_LENGTH_LOW)] = BAR_RECORD_BYTES + 1
    transport.registers[noc_lane_register(0, NocLaneRegisterOffset.RECORD_COUNT_LOW)] = 1
    with pytest.raises(PipelineJobError) as exc:
        execute_bar_noc_packed(device, pack_bar_rows([(1, 2, 3, 4)]), 1, staging=_STAGING)
    assert str(exc.value) == "lane 0: result length 49 inconsistent with record count 1"


def test_execute_bar_noc_packed_error(tmp_path):
    transport = FakeTransport()
    device = fake_device(tmp_path, transport)
    _advertise_lane_count(transport, 4)
    transport.registers[int(RegisterOffset.JOB_STATUS)] = int(JobStatusFlag.DONE | JobStatusFlag.ERROR)
    transport.registers[int(RegisterOffset.LAST_ERROR)] = 1
    with pytest.raises(PipelineJobError) as exc:
        execute_bar_noc_packed(device, pack_bar_rows([(1, 2, 3, 4)]), 1, staging=_STAGING)
    assert str(exc.value) == "bar NoC job failed: status=0xc last_error=1"


def test_the_bar_layout_has_one_declaration() -> None:
    """dau-polars builds numpy dtypes over these same bytes. When the layout
    was declared in both places, a padding or field-order change could make
    the two halves read one bitstream differently — so the derived formats
    must reproduce the wire shapes exactly, and the byte sizes the job
    contract advertises must follow from the fields rather than sit beside
    them."""
    import struct as _struct

    from dau_driver.pipeline_execution import (
        BAR_RECORD_BYTES,
        BAR_RECORD_FIELDS,
        BAR_ROW_BYTES,
        BAR_ROW_FIELDS,
        numpy_dtype_fields,
        struct_format,
    )

    assert struct_format(BAR_ROW_FIELDS) == "<iiii"
    assert struct_format(BAR_RECORD_FIELDS) == "<iiiiiiqqQ"
    assert _struct.calcsize(struct_format(BAR_ROW_FIELDS)) == BAR_ROW_BYTES
    assert _struct.calcsize(struct_format(BAR_RECORD_FIELDS)) == BAR_RECORD_BYTES

    # the vectorized view must describe the identical bytes
    numpy = pytest.importorskip("numpy")
    assert numpy.dtype(numpy_dtype_fields(BAR_RECORD_FIELDS)).itemsize == BAR_RECORD_BYTES
    assert [name for name, _ in BAR_RECORD_FIELDS] == ["key", "open", "close", "low", "high", "pad", "shares", "frac", "count"]


def test_addresses_are_programmed_across_both_halves() -> None:
    """A wide design keeps the high half across a LOW write, so a host that
    wrote only LOW would inherit whatever the previous job left there and
    address the wrong region. Both halves go every time, zero included."""
    from dau_core.registers import NocLaneRegisterOffset, RegisterOffset, noc_lane_register

    from dau_driver.device import DauDevice
    from dau_driver.errors import RegisterAccessError

    writes: list[tuple[int, int]] = []

    class _Recorder(DauDevice):
        def write_register(self, offset, value):  # type: ignore[override]
            writes.append((int(offset), value))

    device = _Recorder.__new__(_Recorder)

    # a low address still writes an explicit ZERO high half
    DauDevice.write_address_register(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 0x1000)
    assert writes == [(int(RegisterOffset.INPUT_ADDRESS_LOW), 0x1000), (int(RegisterOffset.INPUT_ADDRESS_HIGH), 0)]

    # an address above 4 GiB splits correctly
    writes.clear()
    DauDevice.write_address_register(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 0x1_2345_6789)
    assert writes == [(int(RegisterOffset.INPUT_ADDRESS_LOW), 0x2345_6789), (int(RegisterOffset.INPUT_ADDRESS_HIGH), 0x1)]

    # a LANE's halves are NOT adjacent — 0x04 is its result length, so a
    # low+4 assumption would program a length register with address bits
    writes.clear()
    low = noc_lane_register(2, NocLaneRegisterOffset.OUTPUT_ADDRESS_LOW)
    high = noc_lane_register(2, NocLaneRegisterOffset.OUTPUT_ADDRESS_HIGH)
    assert high != low + 4
    DauDevice.write_address_register(device, low, high, 0x2_0000_0000)
    assert writes == [(low, 0), (high, 2)]

    with pytest.raises(RegisterAccessError):
        DauDevice.write_address_register(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 1 << 64)


def test_an_address_past_the_end_of_memory_is_refused() -> None:
    """The reader and writer advance modulo the master's width, so a
    transfer starting near the end of the mapped region WRAPS to the bottom
    and silently reads or overwrites unrelated memory. Hardware does not
    bound it — the length check only tests alignment — so the host is the
    only place this can be caught."""
    from dau_core.registers import RegisterOffset

    from dau_driver.device import DauDevice
    from dau_driver.errors import RegisterAccessError

    class _Recorder(DauDevice):
        def write_register(self, offset, value):  # type: ignore[override]
            pass

    bounded = _Recorder.__new__(_Recorder)
    object.__setattr__(bounded, "memory_bytes", 8 * 2**30)

    # inside the region, including above 4 GiB, is fine
    DauDevice.write_address_register(bounded, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 6 * 2**30)

    with pytest.raises(RegisterAccessError, match="past the device"):
        DauDevice.write_address_register(bounded, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 9 * 2**30)

    # a device that does not know its size keeps the previous behaviour
    # rather than inventing a bound that would refuse valid addresses
    unbounded = _Recorder.__new__(_Recorder)
    object.__setattr__(unbounded, "memory_bytes", None)
    DauDevice.write_address_register(unbounded, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 9 * 2**30)


def test_a_duck_typed_device_can_program_addresses() -> None:
    """Devices here are DUCK-TYPED: test fakes and alternative transports
    implement `write_register` without subclassing DauDevice. Making the
    paired address write a method silently broke every one of them — the
    dau-polars emulated devices among them — so the callable path must need
    nothing but `write_register`."""
    from dau_core.registers import RegisterOffset

    from dau_driver.device import write_address

    class _Fake:  # no DauDevice inheritance, exactly like the emulated shells
        def __init__(self):
            self.writes = []

        def write_register(self, offset, value):
            self.writes.append((int(offset), value))

    fake = _Fake()
    write_address(fake, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, 0x1_0000_0000)
    assert fake.writes == [(int(RegisterOffset.INPUT_ADDRESS_LOW), 0), (int(RegisterOffset.INPUT_ADDRESS_HIGH), 1)]
