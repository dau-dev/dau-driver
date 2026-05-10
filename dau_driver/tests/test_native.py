from __future__ import annotations

from dau_driver import __version__, native_info


def test_native_info_is_available_without_built_extension() -> None:
    info = native_info()

    assert info.package_version == __version__
    assert info.abi_version == 1
    assert info.backend in {"python", "nanobind"}
