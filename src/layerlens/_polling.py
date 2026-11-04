"""Waiting for something the API is still working on (LAY-4429).

A wait polls for minutes, through errors that stop nothing on the server: an API key
throttled by a few CI jobs at once, a deploy, a dropped connection. Every wait in the
SDK, the evaluation waits and the environments waits, shares these pieces.

A wait sends each poll once (``max_retries=0``) and retries here, in one place. With the
client's own retries inside each poll as well, every throttled poll cost three requests,
and a poll started just before the deadline could run up to 16 seconds past it.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from ._exceptions import LayerLensError, RateLimitError, APIConnectionError, InternalServerError
from ._base_client import INITIAL_RETRY_DELAY, parse_retry_after

# How each poll of a wait is sent: once, since the wait does its own retrying.
POLL_RETRIES = 0

# The longest a wait pauses because a Retry-After header asked it to; the wait then polls
# again and the API can ask once more. A wait without a timeout has no deadline to bound the
# pause, and ``time.sleep`` raises OverflowError on a header such as ``1e300``.
MAX_RETRY_AFTER_SECONDS = 300.0


def is_transient(err: BaseException) -> bool:
    """An error a poll rides out until its deadline: no reply, a 5xx, or a 429."""
    return isinstance(err, (APIConnectionError, InternalServerError, RateLimitError))


def retry_after_seconds(err: BaseException, default: float) -> float:
    """The seconds the error's ``Retry-After`` header asks for (delay-seconds or an
    HTTP-date), or ``default`` without a usable one."""
    response = getattr(err, "response", None)
    value = response.headers.get("retry-after") if isinstance(response, httpx.Response) else None
    seconds = parse_retry_after(value)
    return default if seconds is None else seconds


def _seconds(value: float) -> str:
    """``300`` for 300 or 300.0, ``2.5`` for 2.5, and never an exponent (``1e+06``)."""
    return f"{value:.0f}" if float(value).is_integer() else str(value)


class Deadline:
    """A point on the monotonic clock, or none at all for ``seconds=None``."""

    def __init__(self, seconds: Optional[float]) -> None:
        self._end = None if seconds is None else time.monotonic() + seconds

    def passed(self) -> bool:
        return self._end is not None and time.monotonic() >= self._end

    def clip(self, seconds: float) -> float:
        """``seconds``, or less, so that a sleep ends by the deadline."""
        if self._end is None:
            return seconds
        return max(0.0, min(seconds, self._end - time.monotonic()))


class PollPolicy:
    """When a wait polls again, and when it gives up; the same for every wait, sync and async.

    - While the thing waited for is unfinished, the next poll is ``interval_seconds``
      later. With ``max_interval_seconds``, the interval grows by half after each such
      poll, up to that.
    - After a transient error (:func:`is_transient`), the next poll is half a second later,
      then twice as long after each further error in a row, up to the interval (or half a
      second, for a shorter interval); and never sooner than the error's ``Retry-After``, up
      to ``MAX_RETRY_AFTER_SECONDS``. A poll that answers starts this over.
    - Any other error is raised as it is.
    - Past the deadline the wait raises :class:`TimeoutError`, from the last error when the
      last poll failed. No sleep runs past the deadline.
    """

    def __init__(
        self,
        what: str,
        *,
        interval_seconds: float,
        timeout_seconds: Optional[float],
        max_interval_seconds: Optional[float] = None,
    ) -> None:
        self._interval = float(interval_seconds)
        self._max_interval = max_interval_seconds
        self._deadline = Deadline(timeout_seconds)
        self._backoff = INITIAL_RETRY_DELAY
        # Only raised past a deadline, so only ever read with a timeout.
        within = "" if timeout_seconds is None else f" within {_seconds(timeout_seconds)} seconds"
        self._timed_out = f"{what} did not complete{within}"

    def after_pending(self, timed_out: Optional[str] = None) -> float:
        """The seconds to sleep before polling again. Past the deadline, raises
        :class:`TimeoutError` saying ``timed_out``, which describes what this poll saw, or
        that the thing waited for did not complete."""
        if timed_out is not None:
            self._timed_out = timed_out
        if self._deadline.passed():
            raise TimeoutError(self._timed_out)
        self._backoff = INITIAL_RETRY_DELAY
        seconds = self._interval
        if self._max_interval is not None:
            self._interval = min(self._interval * 1.5, self._max_interval)
        return self._deadline.clip(seconds)

    def after_error(self, err: LayerLensError) -> float:
        """The seconds to sleep before polling again after ``err``. Raises ``err`` when it is
        not transient, and :class:`TimeoutError` from it past the deadline."""
        if not is_transient(err):
            raise err
        if self._deadline.passed():
            raise TimeoutError(f"{self._timed_out}; the last poll failed: {err}") from err
        seconds = max(self._backoff, min(retry_after_seconds(err, 0.0), MAX_RETRY_AFTER_SECONDS))
        self._backoff = min(self._backoff * 2, max(self._interval, INITIAL_RETRY_DELAY))
        return self._deadline.clip(seconds)
