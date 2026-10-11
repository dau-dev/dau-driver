"""Importing this package must stay cheap, and its exports must stay honest.

Every model in this stack is a ccflow model, and importing ccflow imports
csp and pandas: about 0.9 s. A package that imports its model modules
at import time puts all of that in front of anything that merely imports
it -- including hydra's searchpath plugin, which imports the config package
to discover what is installed and reads none of these names.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import dau_driver

INIT = Path(dau_driver.__file__)


def _type_checking_imports() -> dict[str, tuple[str, ...]]:
    """What the ``TYPE_CHECKING`` block declares, read from the source."""
    tree = ast.parse(INIT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            return {
                statement.module: tuple(alias.name for alias in statement.names)
                for statement in node.body
                if isinstance(statement, ast.ImportFrom) and statement.module
            }
    raise AssertionError("the package __init__ has no TYPE_CHECKING block")


def test_the_declaration_and_the_type_checking_block_agree() -> None:
    """The two exist for different readers -- the resolver and a type
    checker -- and a name in one and not the other is a name that imports
    for one of them and fails for the other."""
    assert _type_checking_imports() == {module: tuple(names) for module, names in dau_driver._EXPORTS.items()}


def test_every_exported_name_resolves() -> None:
    """A name declared here and missing from its submodule would raise on
    first access, which is a broken import nobody sees until a caller
    reaches for it. Resolve all of them now."""
    for name in dau_driver.__all__:
        getattr(dau_driver, name)
    assert sorted(dau_driver.__all__) == list(dau_driver.__all__)
    assert "__version__" not in dau_driver.__all__


def test_an_unknown_name_still_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'not_a_real_export'"):
        dau_driver.not_a_real_export  # noqa: B018


def test_importing_the_package_stays_cheap() -> None:
    """Run in a fresh interpreter, because this one has already imported
    everything."""
    probe = "import sys; import dau_driver; print(int('ccflow' in sys.modules), int('pandas' in sys.modules))"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    loaded = dict(zip(["ccflow", "pandas"], result.stdout.split(), strict=True))
    assert loaded == {name: "0" for name in ["ccflow", "pandas"]}, f"importing dau_driver pulled in {[n for n, v in loaded.items() if v == '1']}"


def test_two_submodules_cannot_export_one_name() -> None:
    """The declaration is the single source for ``__all__`` and the
    resolver, so a name claimed twice would make which module provides it
    depend on dict order. It is refused where the declaration is read."""
    from dau_driver._lazy import lazy_exports

    with pytest.raises(ValueError, match="exported by both"):
        lazy_exports("dau_driver", {"first": ("Shared",), "second": ("Shared",)})
