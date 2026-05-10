import os
import os.path

from .device import DauDevice
from .errors import DauDriverError, DeviceNotFoundError, RegisterAccessError, TransferIoError, TransferValidationError, WrongBitstreamError
from .linux_xdma import discover_devices, identify_device
from .native import NativeInfo, native_info
from .status import DeviceCounters, DeviceStatus
from .transfer import DmaDirection, DmaTransfer

__version__ = "0.1.0"

__all__ = [
    "DauDevice",
    "DauDriverError",
    "DeviceNotFoundError",
    "DeviceCounters",
    "DeviceStatus",
    "DmaDirection",
    "DmaTransfer",
    "NativeInfo",
    "RegisterAccessError",
    "TransferIoError",
    "TransferValidationError",
    "WrongBitstreamError",
    "bin_path",
    "discover_devices",
    "identify_device",
    "include_path",
    "lib_path",
    "native_info",
]


def include_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "include"))


def bin_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "bin"))


def lib_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "lib"))
