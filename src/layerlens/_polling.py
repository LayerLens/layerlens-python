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
from typing import Union, NoReturn, Optional

import httpx

from ._constants import NOT_GIVEN, NotGiven, capped
from ._exceptions import LayerLensError, RateLimitError, APIConnectionError, InternalServerError
from ._base_client import INITIAL_RETRY_DELAY, parse_retry_after

# How each poll of a wait is sent: once, since the wait does its own retrying.
POLL_RETRIES = 0

# Where a wait with a deadline aims a poll: this long before the deadline, so that poll has time
# to answer and can still see the thing done. Polls can also run inside these last seconds, each
# with MIN_POLL_GAP_SECONDS or more left. Each request of such a wait is sent with the client's
# timeout cut to the time left; the one check of a wait whose deadline was gone before its
# first request (timeout_seconds=0) is given at most this long instead. httpx applies a timeout
# to connecting and to each wait for data, not to a whole response.
MIN_POLL_SECONDS = 2.0

# Half a second: the least time before the deadline that any poll after the first is sent with,
# and the least time from an answer to a poll aimed at MIN_POLL_SECONDS before the deadline. With
# less left, the poll could not answer in time, and its timeout hid why the wait gave up: a
# Retry-After, an interval or a backoff that ended a few ms before the deadline made that doomed
# poll's timeout the cause, not the 429 or what the last poll saw. The wait sleeps out such a
# sliver and sends nothing more. Aimed sooner, the poll at MIN_POLL_SECONDS before the deadline
# was a second poll straight after one that answered just over MIN_POLL_SECONDS before it.
MIN_POLL_GAP_SECONDS = 0.5

# A wait with no deadline reads a Retry-After longer than this as this; the wait then polls again
# and the API can ask once more. Nothing else bounds the pause then, and ``time.sleep`` raises
# OverflowError on a header such as ``1e300``. A wait with a deadline never cuts one short.
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

    def left(self) -> Optional[float]:
        """The seconds left until the deadline (0 once it has passed), or None without one."""
        return None if self._end is None else max(0.0, self._end - time.monotonic())


class PollPolicy:
    """When a wait polls again, and when it gives up; the same for every wait, sync and async.

    - While the thing waited for is unfinished, the next poll is ``interval_seconds``
      later. With ``max_interval_seconds``, the interval grows by half after each such
      poll, up to that.
    - After a transient error (:func:`is_transient`), the next poll is half a second later,
      then twice as long after each further error in a row, up to the interval (or half a
      second, for a shorter interval); and never sooner than the error's ``Retry-After``. A poll
      that answers starts this over.
    - Any other error is raised as it is.
    - With a deadline, each request of the wait is sent with the client's timeout cut to the
      time left (:meth:`poll_timeout`); httpx applies it to connecting and to each wait for
      data. The wait aims a poll at ``MIN_POLL_SECONDS`` before the deadline, so that poll has
      that long to answer; polls can also run inside those last seconds, at the interval or after
      an error. A poll that answers just over ``MIN_POLL_SECONDS`` before the deadline counts as
      that aimed poll: no second one follows at once. No poll after the first is sent with less
      than ``MIN_POLL_GAP_SECONDS`` left, too little to answer: the wait sleeps out that time
      instead and gives up, from what the last poll saw. No request is sent once the deadline has
      passed, and no sleep runs past it; a sleep that runs out the time left is the last thing
      the wait does.
    - A wait never polls sooner than a ``Retry-After`` asks. When the ``Retry-After`` reaches
      past the wait's deadline, the wait sleeps until the deadline and then raises, with the 429
      as the cause; so it does when the ``Retry-After`` ends less than ``MIN_POLL_GAP_SECONDS``
      before the deadline, too little for a poll to answer. A wait with no deadline reads a
      ``Retry-After`` longer than 5 minutes (``MAX_RETRY_AFTER_SECONDS``) as 5 minutes.
    - At the deadline the wait raises :class:`TimeoutError`, from the last error when the last
      poll failed.
    - A wait whose deadline was gone before its first request (``timeout_seconds=0``) checks
      once: the requests of that check (a lookup by name, its poll, and the read that follows
      it) are given the client's timeout, at most ``MIN_POLL_SECONDS``. A deadline that runs out during a check
      gives no such allowance: the next request raises :class:`TimeoutError` instead.
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
        # Whether the deadline was gone before the wait's first request (timeout_seconds=0): None
        # until that request. Only then is one check allowed past it.
        self._started_late: Optional[bool] = None
        # Whether a poll has answered (or failed) yet: the one check ends with it.
        self._polled = False
        # The error of the last poll, when it failed: the TimeoutError is raised from it.
        self._last_error: Optional[LayerLensError] = None
        # Set when the wait sleeps until its deadline (a Retry-After reaches past it, or no poll
        # would have time to answer): it sends nothing more, even if the clock leaves a sliver of
        # time after that sleep, and gives up from the last poll's error.
        self._done = False
        # Only raised past a deadline, so only ever read with a timeout.
        within = "" if timeout_seconds is None else f" within {_seconds(timeout_seconds)} seconds"
        self._timed_out = f"{what} did not complete{within}"

    def poll_timeout(self, client_timeout: object) -> Union[httpx.Timeout, NotGiven]:
        """The timeout of the next request of the wait: the client's, cut to the time left.

        A poll used the client's timeout, so with ``LayerLens(timeout=None)`` one the API never
        answered blocked past ``timeout_seconds``: the deadline is checked between polls. With
        a deadline, each request is now sent with the time left, or the client's timeout when
        that is shorter; httpx applies it to connecting and to each wait for data, not to the
        whole response. Without a deadline the client's timeout applies. Once the deadline has
        passed, raises :class:`TimeoutError` instead of sending the request, except within the
        one check of a wait whose deadline was gone before its first request.
        """
        left = self._deadline.left()
        if left is None:
            return NOT_GIVEN
        if self._started_late is None:
            self._started_late = left <= 0
        if self._done:
            self._give_up()
        if left > 0:
            return capped(client_timeout, left)
        if self._started_late and not self._polled:
            # One check: timeout_seconds=0, or any deadline that was gone before the wait sent
            # anything (a tiny timeout, a thread scheduled late). Every wait checks at least once.
            return capped(client_timeout, MIN_POLL_SECONDS)
        self._give_up()

    def answered(self) -> None:
        """A request of the wait answered, and another follows in the same poll (the results
        once the status says done): a TimeoutError raised before that one is sent is not
        chained to an older failure."""
        self._last_error = None

    def after_pending(self, timed_out: Optional[str] = None) -> float:
        """The seconds to sleep before polling again. Past the deadline, raises
        :class:`TimeoutError` saying ``timed_out``, which describes what this poll saw, or
        that the thing waited for did not complete."""
        if timed_out is not None:
            self._timed_out = timed_out
        self._last_error = None
        self._backoff = INITIAL_RETRY_DELAY
        seconds = self._interval
        if self._max_interval is not None:
            self._interval = min(self._interval * 1.5, self._max_interval)
        return self._next_poll_in(seconds)

    def after_error(self, err: LayerLensError) -> float:
        """The seconds to sleep before polling again after ``err``. Raises ``err`` when it is
        not transient, and :class:`TimeoutError` from it past the deadline."""
        if not is_transient(err):
            raise err
        self._last_error = err
        asked = retry_after_seconds(err, 0.0)
        if self._deadline.left() is None:
            # Nothing else bounds the pause. With a deadline the Retry-After is compared whole with
            # it: capped first, a 900 s one with 600 s left was read as 300 s and polled too soon.
            asked = min(asked, MAX_RETRY_AFTER_SECONDS)
        seconds = max(self._backoff, asked)
        self._backoff = min(self._backoff * 2, max(self._interval, INITIAL_RETRY_DELAY))
        return self._next_poll_in(seconds, not_before=asked)

    def _next_poll_in(self, seconds: float, *, not_before: float = 0.0) -> float:
        """``seconds``, cut so that a poll is aimed at ``MIN_POLL_SECONDS`` before the deadline
        and no sleep runs past it, but never shorter than ``not_before`` (a ``Retry-After``); the
        time left when the poll after it would have less than ``MIN_POLL_GAP_SECONDS`` to answer.
        Raises :class:`TimeoutError` once the deadline has passed."""
        self._polled = True
        left = self._deadline.left()
        if left is None:
            return seconds
        if left <= 0:
            self._give_up()
        to_aim = left - MIN_POLL_SECONDS
        if to_aim >= seconds:
            sleep = seconds
        elif to_aim >= MIN_POLL_GAP_SECONDS:
            # The next poll would come inside the last MIN_POLL_SECONDS: aim it at their start, so
            # it has that long to answer.
            sleep = to_aim
        else:
            # Inside those seconds, or just short of them: the poll that answered was the aimed one.
            # The next comes an interval (or a backoff) later.
            sleep = min(seconds, left)
        sleep = max(sleep, not_before)
        if left - sleep < MIN_POLL_GAP_SECONDS:
            # The poll after this sleep could not answer in time: the Retry-After, the interval or
            # the backoff ends past the deadline or too close to it. Its timeout would have become
            # the cause of the TimeoutError. Sleep out the time and send nothing more, even if the
            # sleep ends a hair early: the next poll_timeout() gives up, from what the last poll saw.
            self._done = True
            return left
        return sleep

    def _give_up(self) -> NoReturn:
        err = self._last_error
        if err is None:
            raise TimeoutError(self._timed_out)
        raise TimeoutError(f"{self._timed_out}; the last poll failed: {err}") from err
