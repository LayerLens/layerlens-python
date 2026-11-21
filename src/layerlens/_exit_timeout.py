"""``LAYERLENS_EXIT_TIMEOUT``: how long a process's exit waits for its traces to upload.

The SDK's exit hook reads it when the process exits; ``layerlens env run exec`` sets it for the
agent it runs when the step sets none. Its own module so the CLI can name it without importing
the upload machinery.
"""

from __future__ import annotations

import math
import threading

EXIT_TIMEOUT_ENV = "LAYERLENS_EXIT_TIMEOUT"


def parse_exit_timeout(value: str) -> float:
    """The seconds ``value`` names: a number from 0 up. Raises ``ValueError`` saying why not."""
    try:
        seconds = float(value.strip())
    except ValueError:
        raise ValueError(f"{EXIT_TIMEOUT_ENV}={value!r} is not a number of seconds") from None
    # A wait longer than the platform's lock timeout raises OverflowError in Thread.join.
    if not math.isfinite(seconds) or seconds < 0 or seconds > threading.TIMEOUT_MAX:
        raise ValueError(f"{EXIT_TIMEOUT_ENV}={value!r} is not a number of seconds from 0 up")
    return seconds
