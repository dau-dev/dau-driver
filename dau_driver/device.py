from __future__ import annotations

import os
import struct
import threading
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Protocol, cast

from dau_core.capabilities import DeviceCapabilities
from dau_core.registers import RegisterBlockSnapshot, RegisterOffset

from .capability_decode import read_register_block_snapshot
from .errors import RegisterAccessError, UnhealthyBarError
from .status import DeviceCounters, DeviceStatus
from .transfer import DmaDirection, DmaTransfer, validate_transfer
from .xdma_io import read_exact_at, write_all_at


class DeviceTransport(Protocol):
    def read_u32(self, offset: int) -> int: ...

    def write_u32(self, offset: int, value: int) -> None: ...

    def read_dma(self, device_address: int, byte_count: int) -> bytes: ...

    def write_dma(self, device_address: int, data: bytes | bytearray | memoryview) -> int: ...


class PathDeviceTransport:
    """A device's devnodes, with the register node held open.

    A register access is one ``pread`` or ``pwrite`` on a descriptor opened
    at first use. Opening and closing the node around every access made each
    one three system calls, and a resident query makes ~175 of them, most
    while polling for the job to finish. Reads and writes keep the separate
    read-only and write-only descriptors the per-access path opened, so a
    caller allowed only to read the node still can. :meth:`close` releases
    them, as does the transport being collected.
    """

    def __init__(self, user_path: Path, h2c_path: Path, c2h_path: Path) -> None:
        self.user_path = Path(user_path)
        self.h2c_path = Path(h2c_path)
        self.c2h_path = Path(c2h_path)
        self._descriptors: dict[int, int] = {}
        self._lock = threading.Lock()
        self._release = weakref.finalize(self, _close_descriptors, self._descriptors)

    def _descriptor(self, flags: int, failure: str) -> int:
        descriptor = self._descriptors.get(flags)
        if descriptor is not None:
            return descriptor
        with self._lock:
            descriptor = self._descriptors.get(flags)
            if descriptor is None:
                try:
                    descriptor = os.open(self.user_path, flags)
                except OSError as exc:
                    raise RegisterAccessError(failure) from exc
                self._descriptors[flags] = descriptor
            return descriptor

    def read_u32(self, offset: int) -> int:
        offset_name = _offset_name(offset)
        failure = f"could not read {offset_name} from {self.user_path}"
        register_bytes = _pread_register(self._descriptor(os.O_RDONLY, failure), self.user_path, int(offset), 4, offset_name)
        return struct.unpack("<I", register_bytes)[0]

    def write_u32(self, offset: int, value: int) -> None:
        failure = f"could not write 0x{offset:x} to {self.user_path}"
        _pwrite_register(self._descriptor(os.O_WRONLY, failure), self.user_path, offset, struct.pack("<I", value))

    def close(self) -> None:
        self._release()

    def read_dma(self, device_address: int, byte_count: int) -> bytes:
        return read_exact_at(self.c2h_path, device_address, byte_count)

    def write_dma(self, device_address: int, data: bytes | bytearray | memoryview) -> int:
        return write_all_at(self.h2c_path, device_address, data)


@dataclass(frozen=True)
class DeviceIdentity:
    """What a device is called for keying purposes, and how stable that is.

    ``kind`` is ``"bdf"`` when the value is the PCI endpoint the board sits
    at (``0000:04:00.0``), which survives a reboot and tells two boards of
    one platform apart; it is ``"node"`` when only the driver's devnode name
    (``xdma0``) could be given, which a reboot can renumber. Callers that
    persist anything keyed by this should read ``kind`` before trusting it.
    """

    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class DauDevice:
    name: str
    backend: str
    user_path: Path
    h2c_path: Path
    c2h_path: Path
    capabilities: DeviceCapabilities
    register_window_offset: int = 0
    transport: DeviceTransport | None = None
    # the addressable size of the device's memory, when the caller knows it
    # (the platform's `memory.size_bytes`). None keeps the previous
    # behaviour of trusting the caller — the guard is only as good as the
    # fact, and inventing a default would either refuse valid addresses or
    # give false confidence.
    memory_bytes: int | None = None

    @property
    def identity(self) -> DeviceIdentity:
        """The most stable name this backend can give the device.

        On ``linux-xdma`` that is the PCI endpoint (BDF) sysfs says owns the
        ``_user`` devnode, resolved through the node itself -- so a device
        whose node is not present (a test fake) or whose backend has no
        sysfs to ask (``macos-xdma`` reports no location) falls back to the
        devnode name, and says so through ``kind``. Never raises.
        """
        if self.backend == "linux-xdma" and self.user_path.exists():
            from .linux_xdma import endpoint_for_devnode

            bdf = endpoint_for_devnode(self.user_path)
            if bdf is not None:
                return DeviceIdentity("bdf", bdf)
        return DeviceIdentity("node", self.name)

    @property
    def resolved_transport(self) -> DeviceTransport:
        if self.transport is not None:
            return self.transport
        return self._path_transport

    @cached_property
    def _path_transport(self) -> PathDeviceTransport:
        """One transport for the device's lifetime, so its register node
        stays open between accesses."""
        return PathDeviceTransport(self.user_path, self.h2c_path, self.c2h_path)

    def read_register(self, offset: RegisterOffset | int) -> int:
        return self.resolved_transport.read_u32(self.register_window_offset + _checked_offset(offset))

    def write_register(self, offset: RegisterOffset | int, value: int) -> None:
        if not 0 <= value <= 0xFFFF_FFFF:
            raise RegisterAccessError(f"register value must fit in 32 bits, got {value!r}")
        self.resolved_transport.write_u32(self.register_window_offset + _checked_offset(offset), value)

    def write_address_register(self, low_offset: RegisterOffset | int, high_offset: RegisterOffset | int, address: int) -> None:
        """Method form of :func:`write_address`, kept for direct callers."""
        write_address(self, low_offset, high_offset, address, memory_bytes=self.memory_bytes)

    def check_bar_health(self, *, offsets: Sequence[RegisterOffset | int] = (RegisterOffset.MAGIC, RegisterOffset.STREAM_PROTOCOL_VERSION)) -> None:
        for offset in offsets:
            value = self.resolved_transport.read_u32(self.register_window_offset + _checked_offset(offset))
            if value == 0xFFFF_FFFF:
                raise UnhealthyBarError(f"{_offset_name(offset)} read all ones from {self.user_path}; BAR/AXI-lite path may be unhealthy")

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(
            raw_status=self.read_register(RegisterOffset.STATUS),
            last_error=self.read_register(RegisterOffset.LAST_ERROR),
        )

    def read_register_block_snapshot(self) -> RegisterBlockSnapshot:
        return read_register_block_snapshot(self.read_register)

    def read_counters(self) -> DeviceCounters:
        return DeviceCounters(
            h2c_bytes=self._read_u64(RegisterOffset.H2C_BYTES_LOW),
            c2h_bytes=self._read_u64(RegisterOffset.C2H_BYTES_LOW),
            rows=self._read_u64(RegisterOffset.ROWS_LOW),
            batches=self.read_register(RegisterOffset.BATCHES),
            stalls=self.read_register(RegisterOffset.STALLS),
        )

    def _read_u64(self, offset: RegisterOffset | int) -> int:
        low = self.resolved_transport.read_u32(self.register_window_offset + _checked_offset(offset))
        high = self.resolved_transport.read_u32(self.register_window_offset + _checked_offset(offset) + 4)
        return low | (high << 32)

    def close(self) -> None:
        transport = self.transport if self.transport is not None else self.__dict__.get("_path_transport")
        if transport is None:
            return
        close = getattr(transport, "close", None)
        if close is not None:
            close()

    def prepare_transfer(
        self,
        direction: DmaDirection,
        *,
        device_address: int,
        byte_count: int,
    ) -> DmaTransfer:
        validate_transfer(direction, device_address, byte_count, self.capabilities.dma, memory_bytes=self.memory_bytes)
        return DmaTransfer(
            direction=direction,
            device_address=device_address,
            byte_count=byte_count,
            channel_path=self.h2c_path if direction == DmaDirection.H2C else self.c2h_path,
        )

    def read_dma(self, *, device_address: int, byte_count: int) -> bytes:
        transfer = self.prepare_transfer(DmaDirection.C2H, device_address=device_address, byte_count=byte_count)
        return self.resolved_transport.read_dma(transfer.device_address, transfer.byte_count)

    def write_dma(self, *, device_address: int, data: bytes | bytearray | memoryview) -> DmaTransfer:
        transfer = self.prepare_transfer(DmaDirection.H2C, device_address=device_address, byte_count=len(data))
        self.resolved_transport.write_dma(transfer.device_address, data)
        return transfer


def read_u32(path: Path, offset: RegisterOffset | int) -> int:
    offset_name = _offset_name(offset)
    register_bytes = _read_register_bytes(path, int(offset), 4, offset_name)
    return struct.unpack("<I", register_bytes)[0]


def read_u64(path: Path, offset: RegisterOffset | int) -> int:
    low = read_u32(path, int(offset))
    high = read_u32(path, int(offset) + 4)
    return low | (high << 32)


def _read_register_bytes(path: Path, offset: int, byte_count: int, offset_name: str) -> bytes:
    try:
        file_descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise RegisterAccessError(f"could not read {offset_name} from {path}") from exc
    try:
        return _pread_register(file_descriptor, path, offset, byte_count, offset_name)
    finally:
        os.close(file_descriptor)


def _pread_register(file_descriptor: int, path: Path, offset: int, byte_count: int, offset_name: str) -> bytes:
    try:
        register_bytes = os.pread(file_descriptor, byte_count, offset)
    except OSError as exc:
        raise RegisterAccessError(f"could not read {offset_name} from {path}") from exc
    if len(register_bytes) != byte_count:
        raise RegisterAccessError(f"short read for {offset_name} from {path}")
    return register_bytes


def _pwrite_register(file_descriptor: int, path: Path, offset: int, data: bytes) -> None:
    try:
        written = os.pwrite(file_descriptor, data, offset)
    except OSError as exc:
        raise RegisterAccessError(f"could not write 0x{offset:x} to {path}") from exc
    if written != len(data):
        raise RegisterAccessError(f"short write for 0x{offset:x} to {path}")


def _close_descriptors(descriptors: dict[int, int]) -> None:
    for descriptor in descriptors.values():
        os.close(descriptor)
    descriptors.clear()


def _checked_offset(offset: RegisterOffset | int) -> int:
    """A register offset is a non-negative multiple of four; anything else is a
    caller's arithmetic error, and the transports would read or write a word
    somewhere unintended."""
    value = int(offset)
    if value < 0 or value % 4:
        raise RegisterAccessError(f"register offset must be a non-negative multiple of 4, got {value!r}")
    return value


def _offset_name(offset: RegisterOffset | int) -> str:
    if isinstance(offset, RegisterOffset):
        return offset.name
    return f"0x{offset:x}"


_DEVICE_MEMORY = object()  # sentinel: take the bound from the device


def write_address(device, low_offset, high_offset, address: int, *, memory_bytes: int | None | object = _DEVICE_MEMORY) -> None:
    """Program an address across its LOW/HIGH register pair on any device.

    A FUNCTION, not a method, because devices here are duck-typed: test
    fakes and alternative transports implement `write_register` without
    subclassing `DauDevice`, and making this a method silently broke every
    one of them.

    BOTH halves are always written, including a zero high half. A design
    whose master is wider than 32 bits keeps the high half across a LOW
    write, so writing only LOW inherits whatever the previous job left and
    quietly addresses the wrong region.

    The offsets are passed explicitly rather than derived as `low + 4`: the
    job pair is adjacent but a LANE's is not — its high half is at 0x14
    because 0x04 is the result length.

    `memory_bytes` bounds the address. It DEFAULTS to the device's own
    declared memory rather than to None, because a guard that is off
    unless every call site remembers to switch it on is not a guard: the
    execution paths that program job addresses all used the plain function
    and so ran unbounded. Reading it off the device is a duck-typed probe
    on purpose — devices here are not required to subclass `DauDevice`, and
    one that does not declare its memory simply goes unbounded, as before.
    Pass an explicit value to override, or `None` to disable the check.
    """
    bound: int | None = getattr(device, "memory_bytes", None) if memory_bytes is _DEVICE_MEMORY else cast("int | None", memory_bytes)
    if not 0 <= address <= 0xFFFF_FFFF_FFFF_FFFF:
        raise RegisterAccessError(f"address must fit in 64 bits, got {address!r}")
    if bound is not None and address >= bound:
        raise RegisterAccessError(
            f"address 0x{address:X} is past the device's {bound} byte memory; a transfer from here wraps to the bottom of memory instead of failing"
        )
    device.write_register(low_offset, address & 0xFFFF_FFFF)
    device.write_register(high_offset, (address >> 32) & 0xFFFF_FFFF)
