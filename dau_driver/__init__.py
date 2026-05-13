import os
import os.path

from .device import DauDevice
from .discovery import discover_devices, identify_device
from .errors import (
    DauDriverError,
    DeviceNotFoundError,
    RegisterAccessError,
    TransferIoError,
    TransferValidationError,
    UnhealthyBarError,
    WrongBitstreamError,
)
from .native import NativeInfo, native_info
from .status import DeviceCounters, DeviceStatus
from .stream_codec import DecodedStreamColumn, DecodedStreamResult, StreamDecoder, StreamEncoder
from .stream_execution import StreamExecutionError, execute_scalar_operation
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
    "DecodedStreamColumn",
    "DecodedStreamResult",
    "NativeInfo",
    "StreamDecoder",
    "StreamEncoder",
    "StreamExecutionError",
    "RegisterAccessError",
    "TransferIoError",
    "TransferValidationError",
    "UnhealthyBarError",
    "WrongBitstreamError",
    "bin_path",
    "discover_devices",
    "identify_device",
    "include_path",
    "execute_scalar_operation",
    "lib_path",
    "native_info",
]


def include_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "include"))


def bin_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "bin"))


def lib_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "lib"))
