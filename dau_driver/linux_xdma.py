from __future__ import annotations

from pathlib import Path

from dau_core.capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from dau_core.registers import DAU_MAGIC_WORD, RegisterBlockSnapshot, RegisterMapVersion, RegisterOffset, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode

from .device import DauDevice, read_u32
from .errors import RegisterAccessError, UnhealthyBarError, WrongBitstreamError

DEFAULT_DEV_ROOT = Path("/dev")
DEFAULT_BACKEND = "linux-xdma"
NITEFURY_PLATFORM_ID = "NITE"
XILINX_VENDOR_ID = 0x10EE
NITEFURY_DEVICE_ID = 0x7011
DEFAULT_DMA_CONSTRAINTS = DmaConstraints(
    alignment_bytes=64,
    descriptor_bytes=32,
    max_transfer_bytes=(1 << 28) - 1,
    read_channels=1,
    write_channels=1,
)
DEFAULT_INPUT_TYPES = (
    LogicalType.INT8,
    LogicalType.INT16,
    LogicalType.INT32,
    LogicalType.INT64,
    LogicalType.FLOAT32,
)
REGISTER_WINDOW_OFFSETS = (0x0, 0x1000)
LINUX_XDMA_C2H_READ_CHUNK_BYTES = 64


def discover_devices(root: str | Path = DEFAULT_DEV_ROOT) -> list[DauDevice]:
    root_path = Path(root)
    devices: list[DauDevice] = []
    for user_path in sorted(root_path.glob("xdma*_user")):
        h2c_path, c2h_path = _channel_paths(user_path)
        if not h2c_path.exists() or not c2h_path.exists():
            continue
        try:
            devices.append(identify_device(user_path, h2c_path=h2c_path, c2h_path=c2h_path))
        except (UnhealthyBarError, WrongBitstreamError):
            continue
    return devices


def identify_device(user_path: str | Path, *, h2c_path: str | Path | None = None, c2h_path: str | Path | None = None) -> DauDevice:
    user = Path(user_path)
    resolved_h2c, resolved_c2h = _channel_paths(user)
    if h2c_path is not None:
        resolved_h2c = Path(h2c_path)
    if c2h_path is not None:
        resolved_c2h = Path(c2h_path)

    register_window_offset, magic = _find_register_window(user)
    if magic == 0xFFFF_FFFF:
        raise UnhealthyBarError(f"DAU BAR magic read all ones at {user}; FPGA-side AXI-lite path may be unhealthy")
    if magic != DAU_MAGIC_WORD:
        raise WrongBitstreamError(f"expected DAU magic 0x{DAU_MAGIC_WORD:08x}, found 0x{magic:08x} at {user}")

    snapshot = _read_register_block_snapshot(user, register_window_offset)

    capabilities = DeviceCapabilities(
        platform_id=snapshot.platform_id,
        pci_vendor_id=XILINX_VENDOR_ID,
        pci_device_id=NITEFURY_DEVICE_ID,
        register_map_version=snapshot.register_map_version,
        stream_protocol_version=snapshot.stream_protocol_version,
        dma=DEFAULT_DMA_CONSTRAINTS,
        operators=_operators_from_bitmap(snapshot.operator_bitmap),
    )

    return DauDevice(
        name=_device_name(user),
        backend=DEFAULT_BACKEND,
        user_path=user,
        h2c_path=resolved_h2c,
        c2h_path=resolved_c2h,
        capabilities=capabilities,
        register_window_offset=register_window_offset,
        read_chunk_bytes=LINUX_XDMA_C2H_READ_CHUNK_BYTES,
    )


def _find_register_window(user_path: Path) -> tuple[int, int]:
    first_magic: int | None = None
    for register_window_offset in REGISTER_WINDOW_OFFSETS:
        try:
            magic = _read_register(user_path, register_window_offset, RegisterOffset.MAGIC)
        except RegisterAccessError:
            continue
        if first_magic is None:
            first_magic = magic
        if magic == DAU_MAGIC_WORD:
            return register_window_offset, magic
    return 0, 0 if first_magic is None else first_magic


def _read_register(user_path: Path, register_window_offset: int, offset: RegisterOffset) -> int:
    return read_u32(user_path, register_window_offset + int(offset))


def _read_register_block_snapshot(user_path: Path, register_window_offset: int) -> RegisterBlockSnapshot:
    return RegisterBlockSnapshot(
        register_map_version=_decode_register_map_version(_read_register(user_path, register_window_offset, RegisterOffset.REGISTER_MAP_VERSION)),
        stream_protocol_version=StreamProtocolVersion.from_u32(
            _read_register(user_path, register_window_offset, RegisterOffset.STREAM_PROTOCOL_VERSION)
        ),
        platform_id=_decode_platform_id(_read_register(user_path, register_window_offset, RegisterOffset.PLATFORM_ID)) or NITEFURY_PLATFORM_ID,
        operator_bitmap=_read_register(user_path, register_window_offset, RegisterOffset.OPERATOR_BITMAP),
    )


def _channel_paths(user_path: Path) -> tuple[Path, Path]:
    name = _device_name(user_path)
    return user_path.with_name(f"{name}_h2c_0"), user_path.with_name(f"{name}_c2h_0")


def _device_name(user_path: Path) -> str:
    return user_path.name.removesuffix("_user")


def _decode_platform_id(value: int) -> str:
    platform_bytes = value.to_bytes(4, "little").rstrip(b"\x00")
    if not platform_bytes or any(byte < 0x20 or byte > 0x7E for byte in platform_bytes):
        return ""
    return platform_bytes.decode("ascii")


def _decode_register_map_version(value: int) -> RegisterMapVersion:
    if value == 0xFFFF_FFFF:
        return RegisterMapVersion(0, 1)
    return RegisterMapVersion.from_u32(value)


def _operators_from_bitmap(operator_bitmap: int) -> tuple[OperatorCapability, ...]:
    operators = []
    for opcode in OperationCode:
        if operator_bitmap & (1 << int(opcode)):
            operators.append(
                OperatorCapability(
                    opcode=opcode,
                    input_types=DEFAULT_INPUT_TYPES,
                    output_type=_output_type(opcode),
                    supports_group_by=False,
                )
            )
    return tuple(operators)


def _output_type(opcode: OperationCode) -> LogicalType:
    if opcode == OperationCode.AVERAGE:
        return LogicalType.FLOAT64
    if opcode == OperationCode.COUNT:
        return LogicalType.UINT64
    return LogicalType.INT64
