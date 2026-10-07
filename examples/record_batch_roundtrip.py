"""Record-batch DDR round-trip on the primary Linux hardware host.

Loads a pre-encoded multi-column DAU record batch, writes it H2C into DDR,
reads it back C2H (respecting the 64-byte chunk quirk via normal driver APIs),
byte-compares, decodes, and checks column aggregates against the expectations
JSON produced at encode time.

    python3 examples/record_batch_roundtrip.py <batch.bin> [device_address]

Stops immediately on unhealthy BAR reads, short I/O, or mismatch (per
hardware guidelines).
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import time
from pathlib import Path

from dau_driver import discover_devices
from dau_driver.stream_codec import StreamDecoder

_STRUCT_CODES = {12: "i", 13: "q", 22: "I", 23: "Q"}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    batch_path = Path(sys.argv[1])
    device_address = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x0

    encoded = batch_path.read_bytes()
    expectations = json.loads(batch_path.with_suffix(".json").read_text())
    assert len(encoded) == expectations["byte_length"], "batch file length mismatch"
    assert hashlib.sha256(encoded).hexdigest() == expectations["sha256"], "batch file digest mismatch"

    devices = discover_devices()
    if not devices:
        raise SystemExit("no DAU/XDMA device found; program the FPGA and bind XDMA first")
    device = devices[0]
    print(f"device: {device.name} backend={device.backend}")
    device.check_bar_health()
    print("BAR health: ok")

    t0 = time.time()
    device.write_dma(device_address=device_address, data=encoded)
    h2c_seconds = time.time() - t0
    print(f"H2C: {len(encoded)} bytes in {h2c_seconds:.3f}s ({len(encoded) / h2c_seconds / 1e6:.1f} MB/s)")

    t0 = time.time()
    readback = device.read_dma(device_address=device_address, byte_count=len(encoded))
    c2h_seconds = time.time() - t0
    print(f"C2H: {len(readback)} bytes in {c2h_seconds:.3f}s ({len(readback) / c2h_seconds / 1e6:.3f} MB/s)")

    if readback != encoded:
        first_diff = next(index for index, (a, b) in enumerate(zip(encoded, readback)) if a != b)
        raise SystemExit(f"MISMATCH at byte {first_diff}: wrote 0x{encoded[first_diff]:02x} read 0x{readback[first_diff]:02x}")
    print("byte compare: ok")

    decoded = StreamDecoder.decode_record_batch(readback)
    print(f"decoded: {decoded.header.column_count} columns x {decoded.header.row_count} rows")
    assert decoded.header.row_count == expectations["row_count"]

    values = {
        column.field_id: struct.unpack(f"<{column.row_count}{_STRUCT_CODES[int(column.logical_type)]}", payload)
        for column, payload in zip(decoded.columns, decoded.payloads)
    }
    checks = {
        "price_micro_sum": sum(values[2]),
        "price_micro_min": min(values[2]),
        "price_micro_max": max(values[2]),
        "size_micro_sum": sum(values[3]),
        "bucket_min": min(values[1]),
        "bucket_max": max(values[1]),
    }
    for name, actual in checks.items():
        expected = expectations[name]
        if actual != expected:
            raise SystemExit(f"MISMATCH {name}: expected {expected}, got {actual}")
        print(f"{name}: {actual} ok")

    device.check_bar_health()
    print("BAR health after DMA: ok")
    print("RECORD_BATCH_ROUNDTRIP_OK")


if __name__ == "__main__":
    main()
