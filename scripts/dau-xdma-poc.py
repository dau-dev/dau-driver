#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _add_repo_packages_to_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    for package_dir in (repo_root / "dau-core", repo_root / "dau-driver"):
        if package_dir.exists():
            sys.path.insert(0, str(package_dir))


_add_repo_packages_to_path()

from dau_core.registers import RegisterOffset  # noqa: E402

from dau_driver import DauDevice, identify_device  # noqa: E402

DEFAULT_USER_PATH = "/dev/xdma0_user"
DEFAULT_SIZES_MIB = "1,4,16,64,256"
# window-relative registers every stream-job top maps; composition tops
# decode only addr[11:0] and return all-ones for anything unmapped, so the
# health probe must stay on contract registers inside the 4 KiB window
HEALTH_REGISTER_OFFSETS = (
    RegisterOffset.MAGIC,
    RegisterOffset.STREAM_PROTOCOL_VERSION,
    RegisterOffset.OPERATOR_BITMAP,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="DAU/XDMA H2C-C2H loopback POC using dau-driver")
    parser.add_argument("--user-path", default=DEFAULT_USER_PATH, help="XDMA user BAR character device")
    parser.add_argument("--device-address", default=0, type=parse_int, help="DDR device address for the loopback")
    parser.add_argument("--sizes-mib", default=DEFAULT_SIZES_MIB, help="Comma-separated transfer sizes in MiB")
    parser.add_argument("--iterations", default=1, type=int, help="Number of full size sweeps to run")
    parser.add_argument("--skip-bar-check", action="store_true", help="Do not check bounded DAU/XADC BAR health after each transfer")
    args = parser.parse_args()

    if args.iterations <= 0:
        parser.error("--iterations must be positive")

    device = identify_device(args.user_path)
    print(
        "DEVICE",
        device.name,
        device.backend,
        "window",
        hex(device.register_window_offset),
        "read_chunk",
        device.read_chunk_bytes,
        flush=True,
    )

    sizes = transfer_sizes(device, parse_sizes_mib(args.sizes_mib))
    for iteration in range(args.iterations):
        for byte_count in sizes:
            if not run_transfer(device, args.user_path, args.device_address, byte_count, iteration, args.skip_bar_check):
                return 1
    return 0


def parse_int(value: str) -> int:
    return int(value, 0)


def parse_sizes_mib(value: str) -> list[int]:
    sizes: list[int] = []
    for item in value.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        size_mib = int(stripped, 0)
        if size_mib <= 0:
            raise argparse.ArgumentTypeError("sizes must be positive")
        sizes.append(size_mib * (1 << 20))
    if not sizes:
        raise argparse.ArgumentTypeError("at least one size is required")
    return sizes


def transfer_sizes(device: DauDevice, requested_sizes: list[int]) -> list[int]:
    constraints = device.capabilities.dma
    sizes: list[int] = []
    for requested_size in requested_sizes:
        byte_count = requested_size
        if byte_count > constraints.max_transfer_bytes:
            byte_count = constraints.max_aligned_transfer_bytes
            print("ADJUST", requested_size, "to", byte_count, "max_aligned", flush=True)
        sizes.append(byte_count)
    return sizes


def run_transfer(
    device: DauDevice,
    user_path: str,
    device_address: int,
    byte_count: int,
    iteration: int,
    skip_bar_check: bool,
) -> bool:
    print("START", iteration, byte_count, flush=True)
    payload_start = time.perf_counter()
    payload = payload_for(byte_count, salt=(iteration * 37 + (byte_count >> 20)) & 0xFF)
    payload_seconds = time.perf_counter() - payload_start

    write_start = time.perf_counter()
    device.write_dma(device_address=device_address, data=payload, timeout_seconds=30.0)
    write_seconds = time.perf_counter() - write_start

    read_start = time.perf_counter()
    readback = device.read_dma(device_address=device_address, byte_count=byte_count, timeout_seconds=30.0)
    read_seconds = time.perf_counter() - read_start

    compare_start = time.perf_counter()
    mismatch = first_mismatch(payload, readback)
    compare_seconds = time.perf_counter() - compare_start
    health = {} if skip_bar_check else bar_health(user_path, device.register_window_offset)

    print(
        "POC_RESULT",
        byte_count,
        "payload_s",
        format_seconds(payload_seconds),
        "write_s",
        format_seconds(write_seconds),
        "read_s",
        format_seconds(read_seconds),
        "compare_s",
        format_seconds(compare_seconds),
        "write_mib_s",
        format_mib_per_second(byte_count, write_seconds),
        "read_mib_s",
        format_mib_per_second(byte_count, read_seconds),
        "match",
        mismatch is None,
        "mismatch",
        mismatch,
        "bar",
        {hex(offset): hex(value) for offset, value in health.items()},
        flush=True,
    )
    return mismatch is None and all(value != 0xFFFFFFFF for value in health.values())


def payload_for(byte_count: int, *, salt: int) -> bytes:
    pattern = bytes((index * 17 + salt) & 0xFF for index in range(256))
    full_patterns, remainder = divmod(byte_count, len(pattern))
    return pattern * full_patterns + pattern[:remainder]


def first_mismatch(expected: bytes, actual: bytes) -> tuple[int, int | None, int | None] | None:
    if expected == actual:
        return None
    for index, (expected_byte, actual_byte) in enumerate(zip(expected, actual)):
        if expected_byte != actual_byte:
            return index, expected_byte, actual_byte
    return min(len(expected), len(actual)), None, None


def bar_health(user_path: str, register_window_offset: int) -> dict[int, int]:
    return {register_window_offset + int(offset): read_u32(user_path, register_window_offset + int(offset)) for offset in HEALTH_REGISTER_OFFSETS}


def read_u32(user_path: str, offset: int) -> int:
    file_descriptor = os.open(user_path, os.O_RDONLY)
    try:
        data = os.pread(file_descriptor, 4, offset)
    finally:
        os.close(file_descriptor)
    if len(data) != 4:
        raise RuntimeError(f"short BAR read at {offset:#x}: {len(data)}")
    return int.from_bytes(data, "little")


def format_seconds(seconds: float) -> str:
    return f"{seconds:.6f}"


def format_mib_per_second(byte_count: int, seconds: float) -> str:
    return f"{byte_count / seconds / (1 << 20):.3f}"


if __name__ == "__main__":
    raise SystemExit(main())