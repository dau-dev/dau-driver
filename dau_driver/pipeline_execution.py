"""Keyed composed-pipeline job execution against the dau_mm_ohlcv_job shell.

The shell runs keyed {value, key} INT32 pair rows through
filter -> map -> ordered grouped aggregation and stages 40-byte group
records in the output BRAM (see dau_build.shell_tops
generate_mm_ohlcv_job_top_sv and the PIPELINE_* register contract).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from dau_core.operators import FilterPredicate, GroupAggregate, MapConfig
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset, StreamJobRegisterContract

from .errors import RegisterAccessError

GROUP_RECORD_BYTES = 40
DMA_ALIGN_BYTES = 64
MAX_ROWS_PER_JOB = 1 << 14  # input BRAM: 16384 pair rows (128 KiB)
MAX_GROUPS_PER_JOB = (1 << 12) * 8 // GROUP_RECORD_BYTES  # output BRAM: 819 records


class PipelineJobError(RegisterAccessError):
    pass


@dataclass(frozen=True)
class KeyedPipelineConfig:
    """Host-side mirror of the PIPELINE_* configuration registers."""

    predicate: FilterPredicate
    filter_on_key: bool = False
    map_config: MapConfig | None = None
    map_on_key: bool = False

    def filter_op_word(self) -> int:
        return int(self.predicate.op0) | (int(self.predicate.combine) << 4) | (int(self.predicate.op1) << 8) | (int(self.filter_on_key) << 16)

    def map_op_word(self) -> int:
        if self.map_config is None:
            # ADD 0 on the value lane: identity
            return 1
        return int(self.map_config.op) | (self.map_config.shift << 8) | (int(self.map_on_key) << 16)

    def map_constant_word(self) -> int:
        return (self.map_config.constant if self.map_config is not None else 0) & 0xFFFFFFFF


def pack_keyed_rows(rows: list[tuple[int, int]]) -> bytes:
    """{value, key} pair rows on the 64-byte DMA grid; the logical length
    stays ``len(rows) * 8``."""
    payload = b"".join(struct.pack("<iI", key, value & 0xFFFFFFFF) for key, value in rows)
    return payload + b"\x00" * ((-len(payload)) % DMA_ALIGN_BYTES)


def decode_group_records(data: bytes, count: int) -> list[GroupAggregate]:
    groups = []
    for offset in range(0, count * GROUP_RECORD_BYTES, GROUP_RECORD_BYTES):
        key, first, last, minimum, maximum, _pad, total, rows = struct.unpack_from("<iiiiiiqQ", data, offset)
        groups.append(GroupAggregate(key=key, first=first, last=last, minimum=minimum, maximum=maximum, total=total, count=rows))
    return groups


def configure_pipeline(device, config: KeyedPipelineConfig) -> None:
    device.write_register(RegisterOffset.PIPELINE_FILTER_OP, config.filter_op_word())
    device.write_register(RegisterOffset.PIPELINE_FILTER_CONSTANT0, config.predicate.constant0 & 0xFFFFFFFF)
    device.write_register(RegisterOffset.PIPELINE_FILTER_CONSTANT1, config.predicate.constant1 & 0xFFFFFFFF)
    device.write_register(RegisterOffset.PIPELINE_MAP_OP, config.map_op_word())
    device.write_register(RegisterOffset.PIPELINE_MAP_CONSTANT, config.map_constant_word())


def _wait_for_job_done(device, *, timeout_seconds: float, label: str, poll=None) -> None:
    """Poll JOB_STATUS until DONE, raising on timeout or a hardware error
    flag. Shared by every register-polled job runner so the wait/error
    semantics stay identical across shells. ``poll``, when given, runs
    each iteration so concurrent work (e.g. overlap staging) can surface
    its failure instead of hiding behind this wait's timeout."""
    deadline = time.time() + timeout_seconds
    while True:
        if poll is not None:
            poll()
        status = device.read_register(RegisterOffset.JOB_STATUS)
        if status & int(JobStatusFlag.DONE):
            break
        if time.time() > deadline:
            raise PipelineJobError(f"{label} timed out (status=0x{status:x})")
    if status & int(JobStatusFlag.ERROR):
        last_error = device.read_register(RegisterOffset.LAST_ERROR)
        raise PipelineJobError(f"{label} failed: status=0x{status:x} last_error={last_error}")


def _read_aligned_records(
    device,
    *,
    address: int,
    result_length: int,
    record_bytes: int,
    count: int,
    count_label: str,
    prefix: str = "",
    validate_record_count: bool = True,
) -> bytes:
    """Read the 64-byte-aligned record block that RESULT_LENGTH describes.
    Returns ``b""`` (and issues no C2H read) when the lane produced no
    output.

    When ``validate_record_count`` is set (bar/q1: the count register is an
    output-record count), the ``result_length == count * record_bytes``
    invariant is enforced. q6-style aggregate lanes latch an input row
    count in the count register and emit one fixed-width record, so they
    pass ``validate_record_count=False`` and trust RESULT_LENGTH alone. The
    empty test keys off RESULT_LENGTH, which is equivalent to ``count == 0``
    whenever the invariant holds."""
    if validate_record_count and result_length != count * record_bytes:
        raise PipelineJobError(f"{prefix}result length {result_length} inconsistent with {count_label} {count}")
    if result_length == 0:
        return b""
    read_bytes = ((result_length + DMA_ALIGN_BYTES - 1) // DMA_ALIGN_BYTES) * DMA_ALIGN_BYTES
    return device.read_dma(device_address=address, byte_count=read_bytes)


def execute_keyed_pipeline_job(
    device,
    rows: list[tuple[int, int]],
    *,
    timeout_seconds: float = 5.0,
    contract: StreamJobRegisterContract | None = None,
) -> tuple[list[GroupAggregate], int]:
    """Run one batch of (key, value) rows; the pipeline must already be
    configured (``configure_pipeline``). Returns (group records, selected
    row count)."""
    if not rows:
        return [], 0
    if len(rows) > MAX_ROWS_PER_JOB:
        raise PipelineJobError(f"{len(rows)} rows exceed the input staging budget of {MAX_ROWS_PER_JOB}")
    resolved = contract or StreamJobRegisterContract()

    device.write_dma(device_address=resolved.input_buffer_address, data=pack_keyed_rows(rows))
    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, len(rows) * 8)
    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))

    _wait_for_job_done(device, timeout_seconds=timeout_seconds, label="pipeline job")

    result_length = device.read_register(RegisterOffset.RESULT_LENGTH_LOW)
    selected = device.read_register(RegisterOffset.PIPELINE_SELECTED_COUNT_LOW)
    group_count = device.read_register(RegisterOffset.PIPELINE_GROUP_COUNT_LOW)
    raw = _read_aligned_records(
        device,
        address=resolved.output_buffer_address,
        result_length=result_length,
        record_bytes=GROUP_RECORD_BYTES,
        count=group_count,
        count_label="group count",
    )
    if group_count == 0:
        return [], selected
    return decode_group_records(raw, group_count), selected


# THE BAR WIRE LAYOUT — one declaration, two consumers.
#
# The driver marshals these with `struct` and dau-polars builds numpy dtypes
# over the same bytes for its vectorized DDR path. Declaring the layout twice
# meant a padding, signedness or field-order change could make the two halves
# read one bitstream differently, so the shapes live here and both sides
# derive from them.
_STRUCT_CODE = {"i4": "i", "i8": "q", "u8": "Q"}

BAR_ROW_FIELDS: tuple[tuple[str, str], ...] = (("key", "i4"), ("price", "i4"), ("shares", "i4"), ("frac", "i4"))
BAR_RECORD_FIELDS: tuple[tuple[str, str], ...] = (
    ("key", "i4"),
    ("open", "i4"),
    ("close", "i4"),
    ("low", "i4"),
    ("high", "i4"),
    ("pad", "i4"),
    ("shares", "i8"),
    ("frac", "i8"),
    ("count", "u8"),
)


def struct_format(fields: tuple[tuple[str, str], ...]) -> str:
    """The little-endian ``struct`` format for a declared field layout."""
    return "<" + "".join(_STRUCT_CODE[kind] for _name, kind in fields)


def numpy_dtype_fields(fields: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """The same layout as numpy dtype pairs, for vectorized consumers."""
    return [(name, "<" + kind) for name, kind in fields]


BAR_ROW_BYTES = 16
BAR_RECORD_BYTES = 48
BAR_MAX_ROWS_PER_JOB = (1 << 17) // BAR_ROW_BYTES  # input BRAM: 8192 rows
BAR_MAX_RECORDS_PER_JOB = (1 << 12) * 8 // BAR_RECORD_BYTES  # output BRAM: 682 records


def pack_bar_rows(rows: list[tuple[int, int, int, int]]) -> bytes:
    """(key, price, shares, frac) rows for the fused bar tile, on the DMA
    grid; the logical length stays ``len(rows) * 16``."""
    payload = b"".join(struct.pack(struct_format(BAR_ROW_FIELDS), key, price, shares, frac) for key, price, shares, frac in rows)
    return payload + b"\x00" * ((-len(payload)) % DMA_ALIGN_BYTES)


def decode_bar_records(data: bytes, count: int):
    from dau_core.operators import BarAggregate

    bars = []
    for offset in range(0, count * BAR_RECORD_BYTES, BAR_RECORD_BYTES):
        key, price_open, price_close, low, high, _pad, shares, frac, rows = struct.unpack_from(struct_format(BAR_RECORD_FIELDS), data, offset)
        bars.append(BarAggregate(key=key, open=price_open, close=price_close, low=low, high=high, shares=shares, frac=frac, count=rows))
    return bars


def execute_bar_job(
    device,
    rows: list[tuple[int, int, int, int]],
    *,
    timeout_seconds: float = 5.0,
    contract: StreamJobRegisterContract | None = None,
):
    """Run one batch of (key, price, shares, frac) rows through the fused
    bar shell. Returns the decoded bar records."""
    if not rows:
        return []
    if len(rows) > BAR_MAX_ROWS_PER_JOB:
        raise PipelineJobError(f"{len(rows)} rows exceed the bar staging budget of {BAR_MAX_ROWS_PER_JOB}")
    resolved = contract or StreamJobRegisterContract()

    device.write_dma(device_address=resolved.input_buffer_address, data=pack_bar_rows(rows))
    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, len(rows) * BAR_ROW_BYTES)
    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))

    _wait_for_job_done(device, timeout_seconds=timeout_seconds, label="bar job")

    result_length = device.read_register(RegisterOffset.RESULT_LENGTH_LOW)
    count = device.read_register(RegisterOffset.PIPELINE_GROUP_COUNT_LOW)
    raw = _read_aligned_records(
        device,
        address=resolved.output_buffer_address,
        result_length=result_length,
        record_bytes=BAR_RECORD_BYTES,
        count=count,
        count_label="bar count",
    )
    if count == 0:
        return []
    return decode_bar_records(raw, count)


# DDR staging layout for the dau_mm_bar_ddr_job shell: rows in the low
# 768 MiB (split into two ping-pong regions so the host can stage the next
# batch while the current one computes), records in the high 256 MiB. Rows
# per job are bounded by the worst case of one record per row so an
# overrun is impossible by construction (an actual overflow would fault
# the job, not corrupt input). Staging addresses stay 4 KiB-aligned so the
# runner's bursts never cross a 4 KiB boundary.
BAR_DDR_INPUT_ADDRESS = 0x0000_0000
BAR_DDR_INPUT_ADDRESS_ALT = 0x1800_0000
BAR_DDR_INPUT_BYTES = 0x1800_0000
BAR_DDR_OUTPUT_ADDRESS = 0x3000_0000
BAR_DDR_OUTPUT_BYTES = 0x1000_0000
BAR_DDR_ADDRESS_ALIGN = 0x1000
BAR_DDR_MAX_ROWS_PER_JOB = min(BAR_DDR_INPUT_BYTES // BAR_ROW_BYTES, BAR_DDR_OUTPUT_BYTES // BAR_RECORD_BYTES)


def stage_bar_ddr_input(device, payload: bytes, row_count: int, *, input_address: int = BAR_DDR_INPUT_ADDRESS) -> None:
    """DMA one pre-packed batch of bar rows into a DDR input region."""
    if row_count > BAR_DDR_MAX_ROWS_PER_JOB:
        raise PipelineJobError(f"{row_count} rows exceed the DDR staging budget of {BAR_DDR_MAX_ROWS_PER_JOB}")
    if len(payload) < row_count * BAR_ROW_BYTES:
        raise PipelineJobError(f"payload of {len(payload)} bytes is short for {row_count} rows")
    if input_address % BAR_DDR_ADDRESS_ALIGN:
        raise PipelineJobError(f"input address 0x{input_address:x} is not 4 KiB-aligned")
    if len(payload) % DMA_ALIGN_BYTES:
        payload = payload + b"\x00" * ((-len(payload)) % DMA_ALIGN_BYTES)
    device.write_dma(device_address=input_address, data=payload)


def start_bar_ddr_job(device, row_count: int, *, input_address: int = BAR_DDR_INPUT_ADDRESS, output_address: int = BAR_DDR_OUTPUT_ADDRESS) -> None:
    """Kick a job over rows already staged in DDR."""
    if input_address % BAR_DDR_ADDRESS_ALIGN or output_address % BAR_DDR_ADDRESS_ALIGN:
        raise PipelineJobError(f"job addresses 0x{input_address:x}/0x{output_address:x} must be 4 KiB-aligned")
    device.write_register(RegisterOffset.INPUT_ADDRESS_LOW, input_address)
    device.write_register(RegisterOffset.OUTPUT_ADDRESS_LOW, output_address)
    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, row_count * BAR_ROW_BYTES)
    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))


def finish_bar_ddr_job(device, *, output_address: int = BAR_DDR_OUTPUT_ADDRESS, timeout_seconds: float = 60.0) -> tuple[bytes, int]:
    """Wait for the running job and read its records back. Returns the raw
    record bytes and the bar count — decoding stays with the caller so
    hosts can use vectorized decoders."""
    _wait_for_job_done(device, timeout_seconds=timeout_seconds, label="bar DDR job")

    result_length = device.read_register(RegisterOffset.RESULT_LENGTH_LOW)
    count = device.read_register(RegisterOffset.PIPELINE_GROUP_COUNT_LOW)
    raw = _read_aligned_records(
        device,
        address=output_address,
        result_length=result_length,
        record_bytes=BAR_RECORD_BYTES,
        count=count,
        count_label="bar count",
    )
    return raw, count


def execute_bar_ddr_packed(
    device,
    payload: bytes,
    row_count: int,
    *,
    timeout_seconds: float = 60.0,
) -> tuple[bytes, int]:
    """Run one pre-packed batch of bar rows through the DDR-staged fused
    bar shell (dau_mm_bar_ddr_job): stage, start, finish."""
    if row_count == 0:
        return b"", 0
    stage_bar_ddr_input(device, payload, row_count)
    start_bar_ddr_job(device, row_count)
    return finish_bar_ddr_job(device, timeout_seconds=timeout_seconds)


def execute_bar_ddr_job(
    device,
    rows: list[tuple[int, int, int, int]],
    *,
    timeout_seconds: float = 60.0,
):
    """Tuple-row convenience wrapper over ``execute_bar_ddr_packed``.
    Returns the decoded bar records."""
    raw, count = execute_bar_ddr_packed(device, pack_bar_rows(rows), len(rows), timeout_seconds=timeout_seconds)
    return decode_bar_records(raw, count)


# broadcast-NoC bar shell (dau_mm_bar_noc_job): one scan, BAR_NOC_LANES
# key-partitioned lanes, each with its own slice of the output region.
# Rows per job are bounded by the worst case of every record landing in
# one lane, so no partition can overrun its slice.
BAR_NOC_LANES = 4
BAR_NOC_LANE_OUTPUT_BYTES = BAR_DDR_OUTPUT_BYTES // BAR_NOC_LANES
BAR_NOC_MAX_ROWS_PER_JOB = min(BAR_DDR_INPUT_BYTES // BAR_ROW_BYTES, BAR_NOC_LANE_OUTPUT_BYTES // BAR_RECORD_BYTES)


def bar_noc_lane_output_address(lane: int) -> int:
    return BAR_DDR_OUTPUT_ADDRESS + lane * BAR_NOC_LANE_OUTPUT_BYTES


def configure_bar_noc_lanes(device, *, lanes: int = BAR_NOC_LANES) -> None:
    """Point every lane's writer at its slice of the output region."""
    from dau_core.registers import NocLaneRegisterOffset, noc_lane_register

    for lane in range(lanes):
        device.write_register(noc_lane_register(lane, NocLaneRegisterOffset.OUTPUT_ADDRESS_LOW), bar_noc_lane_output_address(lane))


def execute_bar_noc_packed(
    device,
    payload: bytes,
    row_count: int,
    *,
    lanes: int = BAR_NOC_LANES,
    record_bytes: int = BAR_RECORD_BYTES,
    validate_record_count: bool = True,
    timeout_seconds: float = 60.0,
) -> list[tuple[bytes, int]]:
    """Run one pre-packed batch through a broadcast-NoC shell: one scan
    computes every lane's partition concurrently. The lanes must already
    be configured (``configure_bar_noc_lanes``). Returns raw record bytes
    and each lane's RECORD_COUNT_LOW value.

    ``lanes`` and ``record_bytes`` default to the fused bar shell; the
    key-partitioned scan shells reuse this runner by passing their own lane
    count and per-record width. q1's grouped shell keeps the default
    ``validate_record_count`` (its count register is an output group-record
    count). q6's aggregate shell latches an input row count and emits one
    fixed-width [sum, count] record per lane, so it passes
    ``validate_record_count=False`` and relies on RESULT_LENGTH."""
    from dau_core.registers import NocLaneRegisterOffset, noc_lane_register

    if row_count == 0:
        return [(b"", 0)] * lanes
    if row_count > BAR_NOC_MAX_ROWS_PER_JOB:
        raise PipelineJobError(f"{row_count} rows exceed the NoC staging budget of {BAR_NOC_MAX_ROWS_PER_JOB}")
    stage_bar_ddr_input(device, payload, row_count)
    start_bar_ddr_job(device, row_count)

    _wait_for_job_done(device, timeout_seconds=timeout_seconds, label="bar NoC job")

    results = []
    for lane in range(lanes):
        result_length = device.read_register(noc_lane_register(lane, NocLaneRegisterOffset.RESULT_LENGTH_LOW))
        count = device.read_register(noc_lane_register(lane, NocLaneRegisterOffset.RECORD_COUNT_LOW))
        raw = _read_aligned_records(
            device,
            address=bar_noc_lane_output_address(lane),
            result_length=result_length,
            record_bytes=record_bytes,
            count=count,
            count_label="record count",
            prefix=f"lane {lane}: ",
            validate_record_count=validate_record_count,
        )
        results.append((raw, count))
    return results
