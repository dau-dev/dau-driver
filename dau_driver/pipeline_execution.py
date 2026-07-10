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

    deadline = time.time() + timeout_seconds
    while True:
        status = device.read_register(RegisterOffset.JOB_STATUS)
        if status & int(JobStatusFlag.DONE):
            break
        if time.time() > deadline:
            raise PipelineJobError(f"pipeline job timed out (status=0x{status:x})")
    if status & int(JobStatusFlag.ERROR):
        last_error = device.read_register(RegisterOffset.LAST_ERROR)
        raise PipelineJobError(f"pipeline job failed: status=0x{status:x} last_error={last_error}")

    result_length = device.read_register(RegisterOffset.RESULT_LENGTH_LOW)
    selected = device.read_register(RegisterOffset.PIPELINE_SELECTED_COUNT_LOW)
    group_count = device.read_register(RegisterOffset.PIPELINE_GROUP_COUNT_LOW)
    if result_length != group_count * GROUP_RECORD_BYTES:
        raise PipelineJobError(f"result length {result_length} inconsistent with group count {group_count}")
    if group_count == 0:
        return [], selected

    read_bytes = ((result_length + DMA_ALIGN_BYTES - 1) // DMA_ALIGN_BYTES) * DMA_ALIGN_BYTES
    raw = device.read_dma(device_address=resolved.output_buffer_address, byte_count=read_bytes)
    return decode_group_records(raw, group_count), selected
