"""Import a package's exports on first use.

Every model in this stack is a ccflow model, and importing ccflow imports
csp, pandas and the rest of its stack: about 0.9 s. A package whose
``__init__`` imports its model modules therefore pays all of it just to be
imported -- including when hydra's searchpath plugin imports the config
package to discover what is installed, which is the slow step in composing
a registry and wants none of those names.

So a package declares which submodule provides each exported name and this
builds the ``__getattr__`` that imports the one submodule the name needs.
The names stay importable exactly as before, and a type checker reads them
from the ``TYPE_CHECKING`` block the package keeps beside the declaration.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

__all__ = ("lazy_exports",)


def lazy_exports(package: str, exports: dict[str, tuple[str, ...]]) -> tuple[Callable[[str], object], list[str]]:
    """A module ``__getattr__`` and ``__all__`` for ``{submodule: names}``.

    Returns both so the two cannot disagree: a name exported by nothing is
    not reachable, and a name declared here and missing from its submodule
    raises on first access rather than at import -- which is what the
    package's lazy-export guard test turns back into an import-time error.
    """
    providers: dict[str, str] = {}
    for module, names in exports.items():
        for name in names:
            if name in providers:
                raise ValueError(f"{package}: {name!r} is exported by both {providers[name]!r} and {module!r}")
            providers[name] = module

    def getattr_(name: str) -> object:
        module = providers.get(name)
        if module is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        return getattr(import_module(f"{package}.{module}"), name)

    return getattr_, sorted(providers)
