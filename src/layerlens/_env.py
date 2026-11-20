"""Environment variables that configure a client.

``LAYERLENS_API_KEY`` and ``LAYERLENS_BASE_URL`` are canonical. The
``LAYERLENS_STRATIX_*`` and ``LAYERLENS_ATLAS_*`` names from earlier releases
are still honoured, in that order, so existing deployments keep working; a
value read from one of them emits a ``DeprecationWarning`` naming the variable
to rename. When the canonical variable is set it wins and the legacy ones are
ignored; a legacy one set to another value is named once per process in a
``UserWarning`` (R1 F2: 1.11.0 read the legacy one, so the switch must not be
silent). An empty value counts as unset.
"""

from __future__ import annotations

import os
import warnings
import threading
from typing import Set, List, Tuple, Optional, NamedTuple

from ._deprecation import warn_deprecated

API_KEY_ENV = "LAYERLENS_API_KEY"
BASE_URL_ENV = "LAYERLENS_BASE_URL"

LEGACY_API_KEY_ENVS: Tuple[str, ...] = ("LAYERLENS_STRATIX_API_KEY", "LAYERLENS_ATLAS_API_KEY")
LEGACY_BASE_URL_ENVS: Tuple[str, ...] = ("LAYERLENS_STRATIX_BASE_URL", "LAYERLENS_ATLAS_BASE_URL")


class EnvValue(NamedTuple):
    value: str
    # The variable (or CLI option) the value came from.
    source: str


def _find(canonical: str, legacy: Tuple[str, ...]) -> Optional[EnvValue]:
    for name in (canonical, *legacy):
        value = os.environ.get(name)
        if value:
            return EnvValue(value, name)
    return None


def find_api_key() -> Optional[EnvValue]:
    """The API key from the environment and where it came from. Never warns."""
    return _find(API_KEY_ENV, LEGACY_API_KEY_ENVS)


def find_base_url() -> Optional[EnvValue]:
    """The base URL from the environment and where it came from. Never warns."""
    return _find(BASE_URL_ENV, LEGACY_BASE_URL_ENVS)


def ignored_legacy(canonical: str, legacy: Tuple[str, ...]) -> List[str]:
    """The legacy variables set to another value than ``canonical``, which is set and wins."""
    value = os.environ.get(canonical)
    if not value:
        return []
    return [name for name in legacy if os.environ.get(name) and os.environ.get(name) != value]


def ignored_message(name: str, canonical: str) -> str:
    """What the SDK and the CLI say about a legacy variable the canonical one overrides."""
    return f"{name} is set but ignored: {canonical} is set and wins. Unset one of them."


# The legacy variables already named in a warning: once per process is enough.
_warned_ignored: Set[str] = set()
_warned_ignored_lock = threading.Lock()


def _warn_ignored(canonical: str, legacy: Tuple[str, ...], stacklevel: int) -> None:
    for name in ignored_legacy(canonical, legacy):
        with _warned_ignored_lock:
            if name in _warned_ignored:
                continue
            _warned_ignored.add(name)
        warnings.warn(ignored_message(name, canonical), UserWarning, stacklevel=stacklevel + 1)


def _resolve(found: Optional[EnvValue], canonical: str, legacy: Tuple[str, ...], stacklevel: int) -> Optional[str]:
    if found is None:
        return None
    if found.source != canonical:
        # +2: this function and its public wrapper sit between the caller and warn.
        warn_deprecated(found.source, canonical, stacklevel=stacklevel + 2)
    else:
        _warn_ignored(canonical, legacy, stacklevel + 2)
    return found.value


def env_api_key(*, stacklevel: int = 1) -> Optional[str]:
    """The API key from the environment, warning if it came from a legacy name,
    or if a legacy name holds another key that the canonical one overrides.

    ``stacklevel`` is counted from the caller of this function, as in
    :func:`warnings.warn`; a client constructor passes ``2`` so the warning
    points at the line that built the client.
    """
    return _resolve(find_api_key(), API_KEY_ENV, LEGACY_API_KEY_ENVS, stacklevel)


def env_base_url(*, stacklevel: int = 1) -> Optional[str]:
    """The base URL from the environment, warning as :func:`env_api_key` does."""
    return _resolve(find_base_url(), BASE_URL_ENV, LEGACY_BASE_URL_ENVS, stacklevel)
