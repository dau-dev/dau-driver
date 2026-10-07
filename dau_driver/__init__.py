from .device import DauDevice, DeviceIdentity
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
    "DecodedStreamColumn",
    "DecodedStreamResult",
    "DeviceCounters",
    "DeviceIdentity",
    "DeviceNotFoundError",
    "DeviceStatus",
    "DmaDirection",
    "DmaTransfer",
    "NativeInfo",
    "RegisterAccessError",
    "StreamDecoder",
    "StreamEncoder",
    "StreamExecutionError",
    "TransferIoError",
    "TransferValidationError",
    "UnhealthyBarError",
    "WrongBitstreamError",
    "discover_devices",
    "execute_scalar_operation",
    "identify_device",
    "native_info",
]
