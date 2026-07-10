from __future__ import annotations

from pathlib import Path

from dau_core.capabilities import DPV1_DEVICE_ID, XILINX_VENDOR_ID, DeviceCapabilities, DmaConstraints
from dau_core.registers import DAU_MAGIC_WORD, RegisterBlockSnapshot, RegisterOffset

from .capability_decode import REGISTER_WINDOW_OFFSETS, operators_from_bitmap, read_register_block_snapshot
from .device import DauDevice, read_u32
from .errors import RegisterAccessError, UnhealthyBarError, WrongBitstreamError

DEFAULT_DEV_ROOT = Path("/dev")
DEFAULT_BACKEND = "linux-xdma"
DEFAULT_DMA_CONSTRAINTS = DmaConstraints(
    alignment_bytes=64,
    descriptor_bytes=32,
    max_transfer_bytes=(1 << 28) - 1,
    read_channels=1,
    write_channels=1,
)
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
        pci_device_id=DPV1_DEVICE_ID,
        register_map_version=snapshot.register_map_version,
        stream_protocol_version=snapshot.stream_protocol_version,
        dma=DEFAULT_DMA_CONSTRAINTS,
        operators=operators_from_bitmap(snapshot.operator_bitmap),
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
    return read_register_block_snapshot(lambda offset: _read_register(user_path, register_window_offset, offset))


def _channel_paths(user_path: Path) -> tuple[Path, Path]:
    name = _device_name(user_path)
    return user_path.with_name(f"{name}_h2c_0"), user_path.with_name(f"{name}_c2h_0")


def _device_name(user_path: Path) -> str:
    return user_path.name.removesuffix("_user")
