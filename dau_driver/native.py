from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

NATIVE_ABI_VERSION = 1
PACKAGE_NAME = "dau-driver"


@dataclass(frozen=True)
class NativeInfo:
    backend: str
    abi_version: int
    package_version: str
    extension_available: bool


def native_info() -> NativeInfo:
    extension = _load_extension()
    if extension is None:
        return NativeInfo(
            backend="python",
            abi_version=NATIVE_ABI_VERSION,
            package_version=_package_version(),
            extension_available=False,
        )

    return NativeInfo(
        backend=extension.native_backend(),
        abi_version=extension.native_abi_version(),
        package_version=_package_version(),
        extension_available=True,
    )


def _load_extension():
    try:
        return import_module("dau_driver.extension")
    except ModuleNotFoundError as exc:
        if exc.name != "dau_driver.extension":
            raise
        return None


def _package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        from dau_driver import __version__

        return __version__
