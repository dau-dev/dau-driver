from __future__ import annotations

import time
from pathlib import Path

from dau_core.capabilities import DPV1_DEVICE_ID, XILINX_VENDOR_ID, DeviceCapabilities, DmaConstraints
from dau_core.registers import DAU_MAGIC_WORD, RegisterBlockSnapshot, RegisterOffset

from .capability_decode import REGISTER_WINDOW_OFFSETS, operators_from_snapshot, read_register_block_snapshot
from .device import DauDevice, read_u32
from .errors import RegisterAccessError, UnhealthyBarError, WrongBitstreamError

DEFAULT_DEV_ROOT = Path("/dev")
DEFAULT_SYSFS_PCI_ROOT = Path("/sys/bus/pci/devices")
DEFAULT_BACKEND = "linux-xdma"
DEFAULT_DMA_CONSTRAINTS = DmaConstraints(
    alignment_bytes=64,
    descriptor_bytes=32,
    max_transfer_bytes=(1 << 28) - 1,
    read_channels=1,
    write_channels=1,
)


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
    operators, host_operators = operators_from_snapshot(snapshot)

    capabilities = DeviceCapabilities(
        platform_id=snapshot.platform_id,
        pci_vendor_id=XILINX_VENDOR_ID,
        pci_device_id=DPV1_DEVICE_ID,
        register_map_version=snapshot.register_map_version,
        stream_protocol_version=snapshot.stream_protocol_version,
        dma=DEFAULT_DMA_CONSTRAINTS,
        operators=operators,
        host_operators=host_operators,
    )

    return DauDevice(
        name=_device_name(user),
        backend=DEFAULT_BACKEND,
        user_path=user,
        h2c_path=resolved_h2c,
        c2h_path=resolved_c2h,
        capabilities=capabilities,
        register_window_offset=register_window_offset,
    )


# The first AXI-Lite reads after a PCIe rescan can transiently return 0/stale
# data before the link settles, so a single-shot magic probe can misread a
# healthy DAU shell as the wrong bitstream. Re-probe a few times before giving
# up so a transient does not masquerade as a real mismatch.
_MAGIC_PROBE_ATTEMPTS = 5
_MAGIC_PROBE_DELAY_S = 0.01


def _find_register_window(user_path: Path) -> tuple[int, int]:
    first_magic: int | None = None
    for attempt in range(_MAGIC_PROBE_ATTEMPTS):
        for register_window_offset in REGISTER_WINDOW_OFFSETS:
            try:
                magic = _read_register(user_path, register_window_offset, RegisterOffset.MAGIC)
            except RegisterAccessError:
                continue
            if first_magic is None:
                first_magic = magic
            if magic == DAU_MAGIC_WORD:
                return register_window_offset, magic
        if attempt + 1 < _MAGIC_PROBE_ATTEMPTS:
            time.sleep(_MAGIC_PROBE_DELAY_S)
    return 0, 0 if first_magic is None else first_magic


def _read_register(user_path: Path, register_window_offset: int, offset: RegisterOffset) -> int:
    return read_u32(user_path, register_window_offset + int(offset))


def _read_register_block_snapshot(user_path: Path, register_window_offset: int) -> RegisterBlockSnapshot:
    return read_register_block_snapshot(lambda offset: _read_register(user_path, register_window_offset, offset))


def endpoint_for_devnode(user_path: Path, *, sysfs_root: Path | None = None) -> str | None:
    """The PCI endpoint (``0000:04:00.0``) whose xdma instance owns this
    ``_user`` devnode, or None when sysfs does not say.

    The inverse of ``verify_advertisement.device_name_for_endpoint``, over
    the same layout: the devnodes hang off the PCI device in sysfs as
    ``<bdf>/xdma/<node>``. None rather than a raise on a missing sysfs, an
    unowned node or (never seen, but not guessed at) two owners, so the
    caller can fall back to a weaker name and say so.
    """
    root = DEFAULT_SYSFS_PCI_ROOT if sysfs_root is None else sysfs_root
    if not root.is_dir():
        return None
    owners = sorted(entry.name for entry in root.iterdir() if (entry / "xdma" / user_path.name).exists())
    if len(owners) != 1:
        return None
    return owners[0]


def _channel_paths(user_path: Path) -> tuple[Path, Path]:
    name = _device_name(user_path)
    return user_path.with_name(f"{name}_h2c_0"), user_path.with_name(f"{name}_c2h_0")


def _device_name(user_path: Path) -> str:
    return user_path.name.removesuffix("_user")
