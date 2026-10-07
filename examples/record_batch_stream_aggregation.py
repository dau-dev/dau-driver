"""On-FPGA record-batch aggregation over XDMA AXI-Stream.

Requires the DAU stream bitstream (XDMA in AXI-Stream mode with the
record-batch aggregation tile between H2C0 and C2H0). For each operation, the
host encodes a multi-column record batch carrying the operation descriptor,
writes it to the H2C stream device, reads the 136-byte scalar result stream
from C2H, decodes it, and compares against the dau-core golden.

    python3 examples/record_batch_stream_aggregation.py [rows]

Stops on short I/O or a result mismatch.
"""

from __future__ import annotations

import os
import struct
import sys
import time

from dau_core.operators import aggregate_values
from dau_core.stream import LogicalType, OperationCode, OperationDescriptor, output_type_for

from dau_driver.stream_codec import RecordBatchColumn, StreamDecoder, StreamEncoder

H2C_PATH = "/dev/xdma0_h2c_0"
C2H_PATH = "/dev/xdma0_c2h_0"
RESULT_BYTES = 192  # 136-byte scalar result stream padded to the 64-byte grid


def _run_operation(values: list[int], opcode: OperationCode, decoy: list[int]) -> None:
    operation = OperationDescriptor(
        operation_id=0,
        opcode=opcode,
        input_column_id=1,  # aggregate the second column; column 0 is a decoy
        output_field_id=0,
        output_type=output_type_for(opcode),
        accumulator_width_bits=64,
    )
    columns = [
        RecordBatchColumn(field_id=0, logical_type=LogicalType.INT32, payload=struct.pack(f"<{len(decoy)}i", *decoy)),
        RecordBatchColumn(field_id=1, logical_type=LogicalType.INT32, payload=struct.pack(f"<{len(values)}i", *values)),
    ]
    encoded = StreamEncoder.encode_record_batch(columns, row_count=len(values), operations=(operation,))

    h2c = os.open(H2C_PATH, os.O_WRONLY)
    try:
        t0 = time.time()
        written = os.write(h2c, encoded)
        h2c_seconds = time.time() - t0
    finally:
        os.close(h2c)
    if written != len(encoded):
        raise SystemExit(f"short H2C write: {written} of {len(encoded)}")

    c2h = os.open(C2H_PATH, os.O_RDONLY)
    try:
        t0 = time.time()
        result_bytes = os.read(c2h, RESULT_BYTES)
        c2h_seconds = time.time() - t0
    finally:
        os.close(c2h)
    if len(result_bytes) < 136:
        raise SystemExit(f"short C2H read: {len(result_bytes)} bytes")

    result = StreamDecoder.decode_scalar_result(result_bytes[:192])
    golden = int(aggregate_values(opcode, values, LogicalType.INT32))
    status = "ok" if result.value == golden else "MISMATCH"
    print(
        f"{opcode.name.lower():>5}: fpga={result.value} golden={golden} [{status}] "
        f"(h2c {len(encoded)} B in {h2c_seconds * 1e3:.1f} ms, c2h {len(result_bytes)} B in {c2h_seconds * 1e3:.1f} ms)"
    )
    if result.value != golden:
        raise SystemExit(1)


def main() -> None:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    # deterministic pseudo-data with negatives and repeats
    values = [((index * 2_654_435_761) % 200_001) - 100_000 for index in range(rows)]
    decoy = [7] * rows

    for opcode in (OperationCode.SUM, OperationCode.MIN, OperationCode.MAX, OperationCode.COUNT):
        _run_operation(values, opcode, decoy)
    print("RECORD_BATCH_STREAM_AGGREGATION_OK")


if __name__ == "__main__":
    main()
