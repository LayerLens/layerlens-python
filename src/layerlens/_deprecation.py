"""Deprecated aliases kept from the Stratix (and earlier Atlas) names.

A legacy name resolves to the *same object* as its replacement, so
``isinstance`` checks and ``except StratixError:`` keep behaving exactly as
they did; each access emits a ``DeprecationWarning`` attributed to the caller's
line. Modules install the hook with ``__getattr__ = module_getattr(...)``
(PEP 562), under ``if not TYPE_CHECKING`` so type checkers see plain aliases
instead and still flag typos.
"""

from __future__ import annotations

import sys
import warnings
from typing import Any, Tuple, Mapping, Callable


def warn_deprecated(old: str, new: str, *, stacklevel: int) -> None:
    """Warn that ``old`` is deprecated in favour of ``new``.

    ``stacklevel`` follows :func:`warnings.warn`, counted from the caller of
    this function: ``1`` attributes the warning to that caller's line.
    """
    warnings.warn(
        f"{old} is deprecated and will be removed in a future major release; use {new} instead",
        DeprecationWarning,
        stacklevel=stacklevel + 1,
    )


def _probed_by_import_machinery() -> bool:
    """True when the module ``__getattr__`` is being called by importlib.

    ``from pkg import name`` first probes ``hasattr(pkg, name)`` inside
    ``importlib._bootstrap`` and only then reads the attribute in the importing
    frame. Warning on the probe would add a second warning pointing into
    importlib; the read that follows warns at the user's line.
    """
    try:
        # 0: this function, 1: the module __getattr__, 2: whoever looked the name up.
        caller = sys._getframe(2)
    except ValueError:
        return False
    return caller.f_globals.get("__name__") == "importlib._bootstrap"


def module_getattr(
    module: str,
    aliases: Mapping[str, Tuple[str, str]],
    namespace: Mapping[str, Any],
) -> Callable[[str], Any]:
    """Build a module ``__getattr__`` that resolves deprecated names.

    ``aliases`` maps each legacy name to ``(attribute, public_name)``: the
    attribute of ``namespace`` (the module's ``globals()``) it resolves to, and
    the public path named in the warning.
    """

    def __getattr__(name: str) -> Any:
        try:
            attribute, public_name = aliases[name]
        except KeyError:
            raise AttributeError(f"module {module!r} has no attribute {name!r}") from None
        if not _probed_by_import_machinery():
            # stacklevel 2: the frame that accessed the attribute.
            warn_deprecated(f"{module}.{name}", public_name, stacklevel=2)
        return namespace[attribute]

    return __getattr__
