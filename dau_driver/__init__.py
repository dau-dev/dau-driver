"""dau-driver: host I/O for a DAU device.

Exports load on first use (:mod:`dau_driver._lazy`), so importing this
package does not import ccflow and its stack before a caller asks for a
name that needs it."""

from typing import TYPE_CHECKING

from ._lazy import lazy_exports

if TYPE_CHECKING:
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

# {submodule: the names it exports}. ``__all__`` and the lazy resolver both
# come from this one declaration, so a name cannot be in one and missing
# from the other; the lazy-export guard test checks it against the
# TYPE_CHECKING block above and resolves every name.
_EXPORTS = {
    "device": ("DauDevice", "DeviceIdentity"),
    "discovery": ("discover_devices", "identify_device"),
    "errors": (
        "DauDriverError",
        "DeviceNotFoundError",
        "RegisterAccessError",
        "TransferIoError",
        "TransferValidationError",
        "UnhealthyBarError",
        "WrongBitstreamError",
    ),
    "native": ("NativeInfo", "native_info"),
    "status": ("DeviceCounters", "DeviceStatus"),
    "stream_codec": ("DecodedStreamColumn", "DecodedStreamResult", "StreamDecoder", "StreamEncoder"),
    "stream_execution": ("StreamExecutionError", "execute_scalar_operation"),
    "transfer": ("DmaDirection", "DmaTransfer"),
}

__getattr__, __all__ = lazy_exports(__name__, _EXPORTS)
