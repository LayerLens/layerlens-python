"""Skip a test module only when an optional dependency is NOT installed.

Use ``importorskip`` from here instead of ``pytest.importorskip`` (the guard in
``tests/test_skip_hygiene.py`` enforces it). ``pytest.importorskip`` skips on any
``ImportError``, so a framework that is installed but fails to import skips its
whole suite instead of failing, and a broken dependency shows up only as fewer
tests. That happened in CI: the resolver picked semantic-kernel 1.8.0, whose
``from pydantic.networks import Url`` fails on pydantic >= 2.12, and the
semantic-kernel and ms-agent suites were silently skipped (LAY-4242).

Here only "this module is not installed" skips: a ``ModuleNotFoundError`` naming
the requested module or one of its parent packages. Everything else propagates
and fails the test module:

* an ``ImportError`` raised while importing it (an API that moved, a bad pin);
* a ``ModuleNotFoundError`` for some OTHER module, i.e. the requested module is
  installed but one of its own dependencies is missing;
* any other exception raised at import time.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Optional

import pytest

__all__ = ["importorskip", "is_installed"]


def importorskip(modname: str, minversion: Optional[str] = None, reason: Optional[str] = None) -> ModuleType:
    """Import and return ``modname``, skipping only if it is not installed.

    Same signature and behavior as ``pytest.importorskip`` otherwise: ``reason``
    is the skip message, and ``minversion`` skips when the module's
    ``__version__`` is lower.
    """
    __tracebackhide__ = True  # report the skip at the caller, as pytest.importorskip does
    is_installed(modname)  # raises if it is installed but fails to import
    # Not installed: pytest skips with its usual message. Installed: pytest returns
    # the already-imported module and applies minversion.
    return pytest.importorskip(modname, minversion=minversion, reason=reason, exc_type=ModuleNotFoundError)


def is_installed(modname: str) -> bool:
    """Import ``modname``; return False only if it is not installed.

    The flag form of ``importorskip``, for ``pytest.mark.skipif`` conditions and
    version-dependent branches. An installed module that fails to import raises.
    """
    try:
        importlib.import_module(modname)
    except ModuleNotFoundError as exc:
        if _names_module_or_parent(exc.name, modname):
            return False
        raise
    return True


def _names_module_or_parent(missing: Optional[str], modname: str) -> bool:
    # "a.b.c" is absent when "a.b.c", "a.b" or "a" is. A ModuleNotFoundError raised
    # by hand may carry no name; it doesn't prove the module is absent.
    return missing is not None and (modname == missing or modname.startswith(missing + "."))
