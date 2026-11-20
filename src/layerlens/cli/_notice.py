"""Notices the CLI prints for a person: deprecations and settings it ignores.

They go to stderr only when stderr is a terminal, as the banner does. In CI, where stderr is a
pipe or a file, the CLI prints nothing extra, exactly as 1.11.0 did, so a step that captures
``2>&1`` and parses the JSON keeps working (R4 F1). Python's own ``DeprecationWarning`` and
``UserWarning`` from the SDK are not these: they follow the warnings filters.

No click here: ``legacy_main`` prints its notice before it checks that click is installed.
"""

from __future__ import annotations

import sys


def stderr_is_terminal() -> bool:
    """Whether a person reads stderr: it is a terminal, not a pipe, a file or a closed stream."""
    stream = sys.stderr
    if stream is None:
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def notice(text: str) -> None:
    """Print ``warning: <text>`` on stderr when stderr is a terminal; print nothing otherwise."""
    if stderr_is_terminal():
        print(f"warning: {text}", file=sys.stderr, flush=True)  # noqa: T201
