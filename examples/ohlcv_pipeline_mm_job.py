"""On-FPGA OHLCV bars through the composed-pipeline MM job shell.

Requires the dau_mm_ohlcv_job bitstream (proven dpv1 MM XDMA personality;
keyed-pair staging at the stream-job contract addresses; identity + job +
pipeline registers at window 0x1000). Flow per batch: pack (time, price)
rows -> H2C to 0x0 -> write pipeline config + INPUT_LENGTH -> JOB_CONTROL
start -> poll JOB_STATUS -> read RESULT_LENGTH -> C2H from 0x100000 ->
decode 5-word group records -> golden compare against
dau_core.operators (filter_rows -> map_rows -> grouped_aggregate_rows).

    sudo PYTHONPATH=.../dau-core:.../dau-driver \
        python3 examples/ohlcv_pipeline_mm_job.py [rows]
"""

from __future__ import annotations

import random
import struct
import sys
import time

from dau_core.operators import (
    FilterPredicate,
    GroupAggregate,
    MapConfig,
    MapOp,
    PredicateOp,
    filter_rows,
    grouped_aggregate_rows,
    map_rows,
)
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset

from dau_driver import discover_devices

INPUT_ADDRESS = 0x0000_0000
OUTPUT_ADDRESS = 0x0010_0000
GROUP_RECORD_BYTES = 40
DMA_ALIGN = 64

PREDICATE = FilterPredicate(op0=PredicateOp.GT, constant0=0)
BUCKET = MapConfig(op=MapOp.MULSHR, constant=139811, shift=23)  # seconds -> minute


def _golden(trades: list[tuple[int, int]]) -> list[GroupAggregate]:
    eligible = filter_rows(trades, PREDICATE)
    keyed = map_rows(eligible, BUCKET, on_key=True)
    return grouped_aggregate_rows(keyed)


def _synthetic_trades(rows: int) -> list[tuple[int, int]]:
    rng = random.Random(20260508)
    trades = []
    second = 34200  # 09:30
    price = 10_000
    for _ in range(rows):
        second += rng.choice((0, 0, 1, 1, 2, 5))
        price = max(1, price + rng.randint(-25, 25))
        # ~5% ineligible rows (zero price) exercise the filter on hardware
        trades.append((second, 0 if rng.random() < 0.05 else price))
    return trades


def _pack_rows(rows: list[tuple[int, int]]) -> bytes:
    payload = b"".join(struct.pack("<iI", key, value & 0xFFFFFFFF) for key, value in rows)
    pad = (-len(payload)) % DMA_ALIGN
    return payload + b"\x00" * pad  # DMA grid pad; INPUT_LENGTH stays rows*8


def _decode_groups(data: bytes, count: int) -> list[GroupAggregate]:
    groups = []
    for offset in range(0, count * GROUP_RECORD_BYTES, GROUP_RECORD_BYTES):
        key, first, last, minimum, maximum, _pad, total, rows = struct.unpack_from("<iiiiiiqQ", data, offset)
        groups.append(GroupAggregate(key=key, first=first, last=last, minimum=minimum, maximum=maximum, total=total, count=rows))
    return groups


def main() -> int:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    devices = discover_devices()
    if not devices:
        print("no DAU device found", file=sys.stderr)
        return 1
    device = devices[0]
    print(f"device: {device.name} window=0x{device.register_window_offset:x}")

    trades = _synthetic_trades(rows)
    golden = _golden(trades)
    payload = _pack_rows(trades)

    filter_op = int(PREDICATE.op0) | (int(PREDICATE.combine) << 4) | (int(PREDICATE.op1) << 8)
    map_op = int(BUCKET.op) | (BUCKET.shift << 8) | (1 << 16)  # map the key lane

    t0 = time.time()
    device.write_dma(device_address=INPUT_ADDRESS, data=payload)
    h2c_seconds = time.time() - t0

    device.write_register(RegisterOffset.PIPELINE_FILTER_OP, filter_op)
    device.write_register(RegisterOffset.PIPELINE_FILTER_CONSTANT0, PREDICATE.constant0 & 0xFFFFFFFF)
    device.write_register(RegisterOffset.PIPELINE_FILTER_CONSTANT1, PREDICATE.constant1 & 0xFFFFFFFF)
    device.write_register(RegisterOffset.PIPELINE_MAP_OP, map_op)
    device.write_register(RegisterOffset.PIPELINE_MAP_CONSTANT, BUCKET.constant & 0xFFFFFFFF)
    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, len(trades) * 8)
    t0 = time.time()
    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))

    deadline = time.time() + 5.0
    while True:
        status = device.read_register(RegisterOffset.JOB_STATUS)
        if status & int(JobStatusFlag.DONE):
            break
        if time.time() > deadline:
            print(f"timed out waiting for job (status=0x{status:x})", file=sys.stderr)
            return 1
    compute_seconds = time.time() - t0
    if status & int(JobStatusFlag.ERROR):
        last_error = device.read_register(RegisterOffset.LAST_ERROR)
        print(f"job errored: status=0x{status:x} last_error={last_error}", file=sys.stderr)
        return 1

    result_length = device.read_register(RegisterOffset.RESULT_LENGTH_LOW)
    selected = device.read_register(RegisterOffset.PIPELINE_SELECTED_COUNT_LOW)
    group_count = device.read_register(RegisterOffset.PIPELINE_GROUP_COUNT_LOW)
    if result_length != group_count * GROUP_RECORD_BYTES:
        print(f"result length {result_length} inconsistent with group count {group_count}", file=sys.stderr)
        return 1

    t0 = time.time()
    read_bytes = ((result_length + DMA_ALIGN - 1) // DMA_ALIGN) * DMA_ALIGN
    raw = device.read_dma(device_address=OUTPUT_ADDRESS, byte_count=max(read_bytes, DMA_ALIGN))
    c2h_seconds = time.time() - t0
    bars = _decode_groups(raw, group_count)

    ok = bars == golden and selected == len(filter_rows(trades, PREDICATE))
    print(
        f"rows={len(trades)} selected={selected} bars={group_count} "
        f"(h2c {len(payload)} B {h2c_seconds * 1e3:.1f} ms, compute {compute_seconds * 1e3:.2f} ms, "
        f"c2h {read_bytes} B {c2h_seconds * 1e3:.1f} ms) [{'ok' if ok else 'MISMATCH'}]"
    )
    if not ok:
        for index, (got, want) in enumerate(zip(bars, golden)):
            if got != want:
                print(f"  first mismatch at bar {index}: got {got}, want {want}", file=sys.stderr)
                break
        return 1
    print("OHLCV_PIPELINE_MM_JOB_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
