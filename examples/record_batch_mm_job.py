"""On-FPGA record-batch aggregation through the MM job shell registers.

Requires the dau_mm_job bitstream (proven MM XDMA personality; BRAM staging at
the stream-job contract addresses; identity+job registers at window 0x1000).
For each operation: encode batch -> H2C to 0x0 -> write INPUT_LENGTH ->
JOB_CONTROL start -> poll JOB_STATUS -> read RESULT_LENGTH -> C2H from
0x100000 -> decode scalar -> golden compare.

    python3 examples/record_batch_mm_job.py [rows]
"""

from __future__ import annotations

import struct
import sys
import time

from dau_core.operators import aggregate_values
from dau_core.registers import JobControlFlag, JobStatusFlag, RegisterOffset
from dau_core.stream import LogicalType, OperationCode, OperationDescriptor, output_type_for

from dau_driver import discover_devices
from dau_driver.stream_codec import RecordBatchColumn, StreamDecoder, StreamEncoder

INPUT_ADDRESS = 0x0000_0000
OUTPUT_ADDRESS = 0x0010_0000
RESULT_READ_BYTES = 192  # 136-byte result stream, padded to the 64-byte grid


def _run_operation(device, values: list[int], opcode: OperationCode, decoy: list[int]) -> None:
    operation = OperationDescriptor(
        operation_id=0,
        opcode=opcode,
        input_column_id=1,
        output_field_id=0,
        output_type=output_type_for(opcode),
        accumulator_width_bits=64,
    )
    columns = [
        RecordBatchColumn(field_id=0, logical_type=LogicalType.INT32, payload=struct.pack(f"<{len(decoy)}i", *decoy)),
        RecordBatchColumn(field_id=1, logical_type=LogicalType.INT32, payload=struct.pack(f"<{len(values)}i", *values)),
    ]
    encoded = StreamEncoder.encode_record_batch(columns, row_count=len(values), operations=(operation,))

    t0 = time.time()
    device.write_dma(device_address=INPUT_ADDRESS, data=encoded)
    h2c_seconds = time.time() - t0

    device.write_register(RegisterOffset.INPUT_LENGTH_LOW, len(encoded))
    device.write_register(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START))

    t0 = time.time()
    for _ in range(10_000):
        status = device.read_register(RegisterOffset.JOB_STATUS)
        if status & int(JobStatusFlag.DONE):
            break
    else:
        raise SystemExit("timed out polling JOB_STATUS")
    compute_seconds = time.time() - t0

    if status & int(JobStatusFlag.ERROR):
        code = device.read_register(RegisterOffset.LAST_ERROR)
        raise SystemExit(f"job error: status=0x{status:x} last_error={code}")

    result_length = device.read_register(RegisterOffset.RESULT_LENGTH_LOW)
    if result_length != 136:
        raise SystemExit(f"unexpected result length {result_length}")

    t0 = time.time()
    result_bytes = device.read_dma(device_address=OUTPUT_ADDRESS, byte_count=RESULT_READ_BYTES)
    c2h_seconds = time.time() - t0

    result = StreamDecoder.decode_scalar_result(result_bytes)
    golden = int(aggregate_values(opcode, values, LogicalType.INT32))
    verdict = "ok" if result.value == golden else "MISMATCH"
    print(
        f"{opcode.name.lower():>5}: fpga={result.value} golden={golden} [{verdict}] "
        f"(h2c {len(encoded)} B {h2c_seconds * 1e3:.1f} ms, compute {compute_seconds * 1e3:.2f} ms, c2h {c2h_seconds * 1e3:.1f} ms)"
    )
    if result.value != golden:
        raise SystemExit(1)


def main() -> None:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    values = [((index * 2_654_435_761) % 200_001) - 100_000 for index in range(rows)]
    decoy = [7] * rows

    devices = discover_devices()
    if not devices:
        raise SystemExit("no DAU/XDMA device found")
    device = devices[0]
    print(f"device: {device.name} window=0x{device.register_window_offset:x}")
    device.check_bar_health()

    for opcode in (OperationCode.SUM, OperationCode.MIN, OperationCode.MAX, OperationCode.COUNT):
        _run_operation(device, values, opcode, decoy)
    device.check_bar_health()
    print("RECORD_BATCH_MM_JOB_OK")


if __name__ == "__main__":
    main()
