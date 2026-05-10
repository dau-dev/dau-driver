from __future__ import annotations

import os
from pathlib import Path

from .errors import TransferIoError


def read_exact_at(path: Path, offset: int, byte_count: int) -> bytes:
    file_descriptor = _open(path, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read < byte_count:
            chunk = os.pread(file_descriptor, byte_count - bytes_read, offset + bytes_read)
            if not chunk:
                raise TransferIoError(f"short read from {path}: expected {byte_count} bytes, read {bytes_read}")
            chunks.append(chunk)
            bytes_read += len(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise TransferIoError(f"could not read {byte_count} bytes from {path} at offset 0x{offset:x}") from exc
    finally:
        os.close(file_descriptor)


def write_all_at(path: Path, offset: int, data: bytes | bytearray | memoryview) -> int:
    data_view = memoryview(data)
    file_descriptor = _open(path, os.O_WRONLY)
    try:
        bytes_written = 0
        while bytes_written < len(data_view):
            written = os.pwrite(file_descriptor, data_view[bytes_written:], offset + bytes_written)
            if written == 0:
                raise TransferIoError(f"short write to {path}: expected {len(data_view)} bytes, wrote {bytes_written}")
            bytes_written += written
        return bytes_written
    except OSError as exc:
        raise TransferIoError(f"could not write {len(data_view)} bytes to {path} at offset 0x{offset:x}") from exc
    finally:
        data_view.release()
        os.close(file_descriptor)


def _open(path: Path, flags: int) -> int:
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise TransferIoError(f"could not open {path}") from exc
