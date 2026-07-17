from __future__ import annotations

import argparse
import ctypes
import ctypes.util
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dau_core.capabilities import DeviceCapabilities, DmaConstraints
from dau_core.registers import DAU_MAGIC_WORD, RegisterBlockSnapshot, RegisterOffset

from .capability_decode import REGISTER_WINDOW_OFFSETS, operators_from_snapshot, read_register_block_snapshot
from .device import DauDevice
from .errors import DeviceNotFoundError, RegisterAccessError, TransferIoError, UnhealthyBarError, WrongBitstreamError

DEFAULT_BACKEND = "macos-xdma"
CANONICAL_SERVICE_NAME = "com.dau-dev.xdma"
LEGACY_SERVICE_NAME = "XDMADriver"
DEFAULT_SERVICE_NAME = CANONICAL_SERVICE_NAME
DEFAULT_SERVICE_NAMES = (CANONICAL_SERVICE_NAME, LEGACY_SERVICE_NAME)
REGISTER_BAR_INDICES = (0, 1, 2, 3, 4, 5)
K_METHOD_READ_BAR = 0
K_METHOD_WRITE_BAR = 1
K_METHOD_DMA_WRITE = 2
K_METHOD_DMA_READ = 3
K_METHOD_GET_INFO = 4
MACOS_XDMA_DMA_CONSTRAINTS = DmaConstraints(
    alignment_bytes=64,
    descriptor_bytes=32,
    max_transfer_bytes=(1 << 28) - 1,
    read_channels=0,
    write_channels=0,
)


@dataclass(frozen=True)
class MacOSXDMAServiceProbe:
    service_name: str
    matched_services: int = 0
    opened_clients: int = 0
    error: str | None = None


@dataclass(frozen=True)
class MacOSXDMAProbeResult:
    status: str
    devices: tuple[DauDevice, ...]
    service_names: tuple[str, ...]
    service_probes: tuple[MacOSXDMAServiceProbe, ...]
    failures: tuple[str, ...]
    message: str


class MacOSXDMAClientProtocol(Protocol):
    def read_bar_u32(self, bar_index: int, offset: int) -> int: ...

    def write_bar_u32(self, bar_index: int, offset: int, value: int) -> None: ...

    def get_info(self) -> MacOSXDMADeviceInfo: ...

    def dma_read(self, device_offset: int, length: int) -> bytes: ...

    def dma_write(self, device_offset: int, data: bytes | bytearray | memoryview) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class MacOSXDMADeviceInfo:
    vendor_id: int
    device_id: int
    subsystem_vendor_id: int
    subsystem_device_id: int


@dataclass(frozen=True)
class MacOSXDMATransport:
    client: MacOSXDMAClientProtocol
    bar_index: int

    def read_u32(self, offset: int) -> int:
        return self.client.read_bar_u32(self.bar_index, offset)

    def write_u32(self, offset: int, value: int) -> None:
        self.client.write_bar_u32(self.bar_index, offset, value)

    def read_dma(self, device_address: int, byte_count: int) -> bytes:
        return self.client.dma_read(device_address, byte_count)

    def write_dma(self, device_address: int, data: bytes | bytearray | memoryview) -> int:
        return self.client.dma_write(device_address, data)

    def close(self) -> None:
        self.client.close()


@dataclass(frozen=True)
class MacOSXDMAClient:
    connection: int
    service: int
    bindings: _IOKitBindings

    @classmethod
    def open_all(
        cls,
        *,
        service_name: str | None = None,
        service_names: Sequence[str] | None = None,
    ) -> tuple[MacOSXDMAClient, ...]:
        clients, service_probes = cls.open_candidates(service_name=service_name, service_names=service_names)
        if not clients:
            names = _normalize_service_names(service_name=service_name, service_names=service_names)
            detail = _format_service_probe_details(service_probes)
            raise DeviceNotFoundError(f"no macOS XDMA DriverKit user client opened for {', '.join(names)}; {detail}")
        return clients

    @classmethod
    def open_candidates(
        cls,
        *,
        service_name: str | None = None,
        service_names: Sequence[str] | None = None,
    ) -> tuple[tuple[MacOSXDMAClient, ...], tuple[MacOSXDMAServiceProbe, ...]]:
        names = _normalize_service_names(service_name=service_name, service_names=service_names)
        bindings = _IOKitBindings.load()
        all_clients: list[MacOSXDMAClient] = []
        service_probes: list[MacOSXDMAServiceProbe] = []
        seen_services: set[int] = set()
        for name in names:
            clients, probe = cls._open_service_name(bindings, name, seen_services)
            all_clients.extend(clients)
            service_probes.append(probe)
        return tuple(all_clients), tuple(service_probes)

    @classmethod
    def _open_service_name(
        cls,
        bindings: _IOKitBindings,
        service_name: str,
        seen_services: set[int],
    ) -> tuple[tuple[MacOSXDMAClient, ...], MacOSXDMAServiceProbe]:
        try:
            iterators = bindings.matching_iterators(service_name)
        except DeviceNotFoundError as exc:
            return (), MacOSXDMAServiceProbe(service_name=service_name, error=str(exc))

        clients: list[MacOSXDMAClient] = []
        matched_services = 0
        try:
            for iterator in iterators:
                while True:
                    service = bindings.next_service(iterator)
                    if service == 0:
                        break
                    matched_services += 1
                    if service in seen_services:
                        bindings.release(service)
                        continue
                    seen_services.add(service)
                    connection = bindings.open_service(service)
                    if connection == 0:
                        bindings.release(service)
                        continue
                    clients.append(cls(connection=connection, service=service, bindings=bindings))
        finally:
            for iterator in iterators:
                bindings.release(iterator)
        return tuple(clients), MacOSXDMAServiceProbe(
            service_name=service_name,
            matched_services=matched_services,
            opened_clients=len(clients),
        )

    def read_bar_u32(self, bar_index: int, offset: int) -> int:
        return self.bindings.call_scalar(self.connection, K_METHOD_READ_BAR, (bar_index, offset), 1)[0]

    def write_bar_u32(self, bar_index: int, offset: int, value: int) -> None:
        self.bindings.call_scalar(self.connection, K_METHOD_WRITE_BAR, (bar_index, offset, value), 0)

    def get_info(self) -> MacOSXDMADeviceInfo:
        vendor_id, device_id, subsystem_vendor_id, subsystem_device_id = self.bindings.call_scalar(self.connection, K_METHOD_GET_INFO, (), 4)
        return MacOSXDMADeviceInfo(
            vendor_id=vendor_id,
            device_id=device_id,
            subsystem_vendor_id=subsystem_vendor_id,
            subsystem_device_id=subsystem_device_id,
        )

    def dma_read(self, device_offset: int, length: int) -> bytes:
        raise TransferIoError("macOS XDMA DriverKit DMA methods do not carry data buffers yet")

    def dma_write(self, device_offset: int, data: bytes | bytearray | memoryview) -> int:
        raise TransferIoError("macOS XDMA DriverKit DMA methods do not carry data buffers yet")

    def close(self) -> None:
        self.bindings.close(self.connection)
        self.bindings.release(self.service)


@dataclass(frozen=True)
class _IOKitBindings:
    iokit: ctypes.CDLL
    libsystem: ctypes.CDLL

    @classmethod
    def load(cls) -> _IOKitBindings:
        iokit_path = ctypes.util.find_library("IOKit")
        libsystem_path = ctypes.util.find_library("System")
        if iokit_path is None or libsystem_path is None:
            raise DeviceNotFoundError("macOS IOKit libraries are not available")
        bindings = cls(iokit=ctypes.CDLL(iokit_path), libsystem=ctypes.CDLL(libsystem_path))
        bindings._configure_signatures()
        return bindings

    def _configure_signatures(self) -> None:
        self.iokit.IOServiceNameMatching.argtypes = [ctypes.c_char_p]
        self.iokit.IOServiceNameMatching.restype = ctypes.c_void_p
        self.iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        self.iokit.IOServiceMatching.restype = ctypes.c_void_p
        self.iokit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.iokit.IOServiceGetMatchingServices.restype = ctypes.c_int32
        self.iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
        self.iokit.IOIteratorNext.restype = ctypes.c_uint32
        self.iokit.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        self.iokit.IOServiceOpen.restype = ctypes.c_int32
        self.iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
        self.iokit.IOServiceClose.restype = ctypes.c_int32
        self.iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        self.iokit.IOObjectRelease.restype = ctypes.c_int32
        self.iokit.IOConnectCallScalarMethod.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.iokit.IOConnectCallScalarMethod.restype = ctypes.c_int32

    def matching_services(self, service_name: str) -> int:
        iterators = self.matching_iterators(service_name)
        for iterator in iterators[1:]:
            self.release(iterator)
        return iterators[0] if iterators else 0

    def matching_iterators(self, service_name: str) -> tuple[int, ...]:
        iterators: list[int] = []
        for matcher_name in ("IOServiceNameMatching", "IOServiceMatching"):
            iterator = self._matching_iterator(matcher_name, service_name)
            if iterator:
                iterators.append(iterator)
        return tuple(iterators)

    def _matching_iterator(self, matcher_name: str, service_name: str) -> int:
        matching_function = getattr(self.iokit, matcher_name)
        matching = matching_function(service_name.encode("utf-8"))
        if not matching:
            raise DeviceNotFoundError(f"could not create IOKit {matcher_name} dictionary for {service_name}")
        iterator = ctypes.c_uint32()
        result = self.iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator))
        if result != 0:
            raise DeviceNotFoundError(f"IOServiceGetMatchingServices({matcher_name}, {service_name}) failed: 0x{result:x}")
        return int(iterator.value)

    def next_service(self, iterator: int) -> int:
        return int(self.iokit.IOIteratorNext(iterator))

    def open_service(self, service: int) -> int:
        connection = ctypes.c_uint32()
        result = self.iokit.IOServiceOpen(service, self.mach_task_self(), 0, ctypes.byref(connection))
        if result != 0:
            return 0
        return int(connection.value)

    def call_scalar(self, connection: int, selector: int, inputs: Sequence[int], output_count: int) -> tuple[int, ...]:
        input_array = (ctypes.c_uint64 * len(inputs))(*inputs) if inputs else None
        output_array = (ctypes.c_uint64 * output_count)()
        output_count_value = ctypes.c_uint32(output_count)
        result = self.iokit.IOConnectCallScalarMethod(
            connection,
            selector,
            input_array,
            len(inputs),
            output_array if output_count else None,
            ctypes.byref(output_count_value),
        )
        if result != 0:
            raise RegisterAccessError(f"IOConnectCallScalarMethod({selector}) failed: 0x{result:x}")
        return tuple(int(output_array[index]) for index in range(output_count_value.value))

    def close(self, connection: int) -> None:
        self.iokit.IOServiceClose(connection)

    def release(self, obj: int) -> None:
        if obj:
            self.iokit.IOObjectRelease(obj)

    def mach_task_self(self) -> int:
        try:
            self.libsystem.mach_task_self.restype = ctypes.c_uint32
            return int(self.libsystem.mach_task_self())
        except AttributeError:
            return int(ctypes.c_uint32.in_dll(self.libsystem, "mach_task_self_").value)


def discover_devices(client_factory: Callable[[], Iterable[MacOSXDMAClientProtocol]] | None = None) -> list[DauDevice]:
    return list(probe_devices(client_factory=client_factory).devices)


def probe_devices(
    client_factory: Callable[[], Iterable[MacOSXDMAClientProtocol]] | None = None,
    *,
    service_names: Sequence[str] = DEFAULT_SERVICE_NAMES,
    service_probes: Sequence[MacOSXDMAServiceProbe] = (),
) -> MacOSXDMAProbeResult:
    probes = tuple(service_probes)
    try:
        if client_factory is None:
            clients, probes = MacOSXDMAClient.open_candidates(service_names=service_names)
        else:
            clients = tuple(client_factory())
    except DeviceNotFoundError as exc:
        return _probe_result(
            status="no-service",
            devices=(),
            service_names=tuple(service_names),
            service_probes=probes,
            failures=(str(exc),),
        )

    devices: list[DauDevice] = []
    failures: list[str] = []
    for index, client in enumerate(clients):
        try:
            devices.append(identify_device(client, index=index))
        except (UnhealthyBarError, WrongBitstreamError, RegisterAccessError) as exc:
            failures.append(str(exc))
            client.close()

    return _probe_result(
        status=_probe_status(tuple(devices), tuple(clients), probes, tuple(failures)),
        devices=tuple(devices),
        service_names=tuple(service_names),
        service_probes=probes,
        failures=tuple(failures),
    )


def _normalize_service_names(
    *,
    service_name: str | None = None,
    service_names: Sequence[str] | None = None,
) -> tuple[str, ...]:
    if service_name is not None and service_names is not None:
        raise ValueError("pass either service_name or service_names, not both")
    raw_names = (service_name,) if service_name is not None else tuple(DEFAULT_SERVICE_NAMES if service_names is None else service_names)
    names: list[str] = []
    for name in raw_names:
        if name and name not in names:
            names.append(name)
    if not names:
        raise ValueError("at least one macOS XDMA service name is required")
    return tuple(names)


def _probe_status(
    devices: tuple[DauDevice, ...],
    clients: tuple[MacOSXDMAClientProtocol, ...],
    service_probes: tuple[MacOSXDMAServiceProbe, ...],
    failures: tuple[str, ...],
) -> str:
    if devices:
        return "found"
    if failures:
        if any("all ones" in failure for failure in failures):
            return "unhealthy-bar"
        if any("DAU magic" in failure for failure in failures):
            return "wrong-bitstream"
        return "register-access-failed"
    if clients:
        return "no-compatible-bitstream"
    if any(probe.matched_services and probe.opened_clients == 0 for probe in service_probes):
        return "no-user-client"
    return "no-service"


def _probe_result(
    *,
    status: str,
    devices: tuple[DauDevice, ...],
    service_names: tuple[str, ...],
    service_probes: tuple[MacOSXDMAServiceProbe, ...],
    failures: tuple[str, ...],
) -> MacOSXDMAProbeResult:
    return MacOSXDMAProbeResult(
        status=status,
        devices=devices,
        service_names=service_names,
        service_probes=service_probes,
        failures=failures,
        message=_probe_message(status, devices, service_names, service_probes, failures),
    )


def _probe_message(
    status: str,
    devices: tuple[DauDevice, ...],
    service_names: tuple[str, ...],
    service_probes: tuple[MacOSXDMAServiceProbe, ...],
    failures: tuple[str, ...],
) -> str:
    service_list = ", ".join(service_names)
    if status == "found":
        return f"found {len(devices)} DAU macOS XDMA device(s)"
    if status == "no-service":
        detail = _format_service_probe_details(service_probes)
        suffix = f" Details: {detail}." if detail else ""
        return (
            f"No macOS XDMA DriverKit service found for {service_list}. "
            "Check that the PCIe endpoint is visible, the com.dau-dev.xdma dext is signed/installed/approved, "
            f"and the driver called RegisterService().{suffix}"
        )
    if status == "no-user-client":
        detail = _format_service_probe_details(service_probes)
        return (
            f"DriverKit service matched but IOServiceOpen did not return a user client. {detail}. "
            "Check XDMADriver::NewUserClient, XDMAUserClientProperties, and user-client access entitlements."
        )
    if status == "wrong-bitstream":
        return "DriverKit service opened, but the BAR/register scan did not find the DAU magic word. " + "; ".join(failures)
    if status == "unhealthy-bar":
        return "DriverKit service opened, but BAR reads look unhealthy. " + "; ".join(failures)
    if status == "register-access-failed":
        return "DriverKit service opened, but scalar BAR/register calls failed. " + "; ".join(failures)
    return "DriverKit service opened, but no DAU-compatible register window was found."


def _format_service_probe_details(service_probes: Sequence[MacOSXDMAServiceProbe]) -> str:
    parts = []
    for probe in service_probes:
        if probe.error:
            parts.append(f"{probe.service_name}: {probe.error}")
        else:
            parts.append(f"{probe.service_name}: matched {probe.matched_services} service(s), opened {probe.opened_clients} user client(s)")
    return "; ".join(parts)


def identify_device(client: MacOSXDMAClientProtocol, *, index: int = 0) -> DauDevice:
    bar_index, register_window_offset, magic = _find_register_window(client)
    if magic == 0xFFFF_FFFF:
        raise UnhealthyBarError("DAU BAR magic read all ones from macOS XDMA user client; BAR/AXI-lite path may be unhealthy")
    if magic != DAU_MAGIC_WORD:
        raise WrongBitstreamError(f"expected DAU magic 0x{DAU_MAGIC_WORD:08x}, found 0x{magic:08x} from macOS XDMA user client")

    info = client.get_info()
    snapshot = _read_register_block_snapshot(client, bar_index, register_window_offset)
    operators, host_operators = operators_from_snapshot(snapshot)
    capabilities = DeviceCapabilities(
        platform_id=snapshot.platform_id,
        pci_vendor_id=info.vendor_id,
        pci_device_id=info.device_id,
        register_map_version=snapshot.register_map_version,
        stream_protocol_version=snapshot.stream_protocol_version,
        dma=MACOS_XDMA_DMA_CONSTRAINTS,
        operators=operators,
        host_operators=host_operators,
    )

    return DauDevice(
        name=f"xdma{index}",
        backend=DEFAULT_BACKEND,
        user_path=Path(f"macos-xdma{index}_bar{bar_index}_user"),
        h2c_path=Path(f"macos-xdma{index}_h2c_0"),
        c2h_path=Path(f"macos-xdma{index}_c2h_0"),
        capabilities=capabilities,
        register_window_offset=register_window_offset,
        transport=MacOSXDMATransport(client=client, bar_index=bar_index),
    )


def _find_register_window(client: MacOSXDMAClientProtocol) -> tuple[int, int, int]:
    first_magic: int | None = None
    for bar_index in REGISTER_BAR_INDICES:
        for register_window_offset in REGISTER_WINDOW_OFFSETS:
            try:
                magic = client.read_bar_u32(bar_index, register_window_offset + int(RegisterOffset.MAGIC))
            except RegisterAccessError:
                continue
            if first_magic is None:
                first_magic = magic
            if magic == DAU_MAGIC_WORD:
                return bar_index, register_window_offset, magic
    return 0, 0, 0 if first_magic is None else first_magic


def _read_register_block_snapshot(client: MacOSXDMAClientProtocol, bar_index: int, register_window_offset: int) -> RegisterBlockSnapshot:
    return read_register_block_snapshot(lambda offset: _read_register(client, bar_index, register_window_offset, offset))


def _read_register(client: MacOSXDMAClientProtocol, bar_index: int, register_window_offset: int, offset: RegisterOffset) -> int:
    return client.read_bar_u32(bar_index, register_window_offset + int(offset))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe DAU-compatible macOS XDMA DriverKit devices")
    parser.add_argument("--service-name", action="append", dest="service_names", help="IOKit service name/class to probe; may be repeated")
    parser.add_argument("--verbose", action="store_true", help="Print service match/open diagnostics")
    args = parser.parse_args(argv)

    service_names = tuple(args.service_names) if args.service_names else DEFAULT_SERVICE_NAMES
    result = probe_devices(service_names=service_names)
    if not result.devices:
        print(f"macos-xdma\tstatus={result.status} devices=0 message={result.message}")
        if args.verbose:
            for service_probe in result.service_probes:
                print(
                    "macos-xdma-service\t"
                    f"name={service_probe.service_name} "
                    f"matched={service_probe.matched_services} "
                    f"opened={service_probe.opened_clients} "
                    f"error={service_probe.error or ''}"
                )
        return 1
    for device in result.devices:
        snapshot = device.read_register_block_snapshot()
        print(
            "macos-xdma-device\t"
            f"name={device.name} "
            f"vendor=0x{device.capabilities.pci_vendor_id:04x} "
            f"device=0x{device.capabilities.pci_device_id:04x} "
            f"platform={snapshot.platform_id} "
            f"register_window=0x{device.register_window_offset:x}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
