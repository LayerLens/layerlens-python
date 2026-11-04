"""``layerlens._polling``: when a wait polls again, and when it gives up (LAY-4429).

The policy decides; the waiters only do the I/O. Every decision is checked here against
a clock the test moves by hand, so nothing sleeps.
"""

from __future__ import annotations

from typing import Dict, Optional
from email.utils import formatdate

import httpx
import pytest

from layerlens import _polling
from layerlens._polling import Deadline, PollPolicy, is_transient, retry_after_seconds
from layerlens._exceptions import (
    NotFoundError,
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    AuthenticationError,
    InternalServerError,
    APIResponseValidationError,
)

_REQUEST = httpx.Request("GET", "https://api.test/api/v1/evaluations/e1")


def _response(status: int, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=_REQUEST)


def _throttled(retry_after: Optional[str] = None) -> RateLimitError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return RateLimitError("Too Many Requests", response=_response(429, headers), body=None)


def _server_error(status: int = 503) -> InternalServerError:
    return InternalServerError("Service Unavailable", response=_response(status), body=None)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(_polling, "time", fake)
    return fake


class TestIsTransient:
    @pytest.mark.parametrize(
        "err",
        [
            _throttled(),
            _server_error(500),
            _server_error(503),
            APIConnectionError(request=_REQUEST),
            APITimeoutError(request=_REQUEST),
        ],
        ids=["429", "500", "503", "connection", "timeout"],
    )
    def test_a_poll_rides_out_throttling_server_errors_and_lost_connections(self, err: Exception) -> None:
        assert is_transient(err)

    @pytest.mark.parametrize(
        "err",
        [
            AuthenticationError("Unauthorized", response=_response(401), body=None),
            NotFoundError("Not Found", response=_response(404), body=None),
            APIResponseValidationError(_response(200), None),
            ValueError("not an API error"),
        ],
        ids=["401", "404", "unreadable-200", "other"],
    )
    def test_every_other_error_ends_the_wait(self, err: Exception) -> None:
        assert not is_transient(err)


class TestRetryAfterSeconds:
    def test_the_header_in_seconds(self) -> None:
        assert retry_after_seconds(_throttled("7"), 1.0) == 7.0

    def test_the_default_without_the_header(self) -> None:
        assert retry_after_seconds(_throttled(), 1.5) == 1.5

    def test_an_http_date_is_the_seconds_until_then(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = 1_793_000_000.0
        monkeypatch.setattr("layerlens._base_client.time.time", lambda: now)
        assert retry_after_seconds(_throttled(formatdate(now + 25, usegmt=True)), 2.0) == 25.0

    @pytest.mark.parametrize("value", ["soon", "-3", "inf", "Infinity", "nan"])
    def test_the_default_for_a_header_with_no_usable_delay(self, value: str) -> None:
        # float() reads "inf" and "nan" too; time.sleep(inf) raises OverflowError, which would end the wait.
        assert retry_after_seconds(_throttled(value), 2.0) == 2.0

    def test_the_default_for_an_error_without_a_response(self) -> None:
        assert retry_after_seconds(APIConnectionError(request=_REQUEST), 4.0) == 4.0


class TestDeadline:
    def test_none_never_passes(self, clock: _Clock) -> None:
        deadline = Deadline(None)
        clock.now = 1e9
        assert not deadline.passed()
        assert deadline.clip(30.0) == 30.0

    def test_clip_never_sleeps_past_it(self, clock: _Clock) -> None:
        deadline = Deadline(10.0)
        clock.now = 8.0
        assert deadline.clip(5.0) == 2.0
        clock.now = 12.0
        assert deadline.passed()
        assert deadline.clip(5.0) == 0.0


class TestPollPolicy:
    def test_polls_again_after_the_interval_while_unfinished(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=5, timeout_seconds=60)
        assert [policy.after_pending() for _ in range(3)] == [5.0, 5.0, 5.0]

    def test_the_interval_can_grow_by_half_up_to_a_ceiling(self, clock: _Clock) -> None:
        policy = PollPolicy("run r1", interval_seconds=5, timeout_seconds=None, max_interval_seconds=15)
        assert [policy.after_pending() for _ in range(5)] == [5.0, 7.5, 11.25, 15.0, 15.0]

    def test_raises_timeout_once_the_deadline_passed_while_unfinished(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=5, timeout_seconds=60)
        clock.now = 60.0
        with pytest.raises(TimeoutError, match=r"^Evaluation e1 did not complete within 60 seconds$"):
            policy.after_pending()

    def test_the_timeout_is_named_as_given(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=30, timeout_seconds=1_000_000)
        clock.now = 1_000_000.0
        with pytest.raises(TimeoutError, match=r"^Evaluation e1 did not complete within 1000000 seconds$"):
            policy.after_pending()

    def test_a_wait_can_say_what_it_saw_when_it_times_out(self, clock: _Clock) -> None:
        policy = PollPolicy("run r1", interval_seconds=5, timeout_seconds=60)
        clock.now = 60.0
        with pytest.raises(TimeoutError, match=r"^run r1 is still in_progress after 60s$"):
            policy.after_pending("run r1 is still in_progress after 60s")

    def test_transient_errors_back_off_from_half_a_second_up_to_the_interval(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=5, timeout_seconds=None)
        delays = [policy.after_error(_server_error()) for _ in range(6)]
        assert delays == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]

    def test_a_poll_that_succeeds_resets_the_backoff(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=30, timeout_seconds=None)
        assert [policy.after_error(_server_error()) for _ in range(3)] == [0.5, 1.0, 2.0]
        assert policy.after_pending() == 30.0
        assert policy.after_error(_server_error()) == 0.5

    def test_errors_back_off_even_with_an_interval_of_zero(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=0, timeout_seconds=None)
        assert [policy.after_error(_server_error()) for _ in range(3)] == [0.5, 0.5, 0.5]

    def test_retry_after_longer_than_the_backoff_is_honoured_even_past_the_interval(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=5, timeout_seconds=60)
        assert policy.after_error(_throttled("2")) == 2.0
        assert policy.after_error(_throttled("12")) == 12.0

    def test_a_retry_after_of_infinity_is_no_delay(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=30, timeout_seconds=None)
        assert policy.after_error(_throttled("inf")) == 0.5

    @pytest.mark.parametrize(
        "value, seconds",
        [
            ("300", 300.0),
            ("301", 300.0),
            ("86400", 300.0),
            # time.sleep refuses both outright (OverflowError), and an async sleep never wakes.
            ("1e300", 300.0),
            ("Fri, 31 Dec 9999 23:59:59 GMT", 300.0),
        ],
    )
    def test_a_retry_after_is_honoured_up_to_five_minutes(self, clock: _Clock, value: str, seconds: float) -> None:
        # Without a timeout no deadline clips the pause, so the ceiling is all that bounds it.
        policy = PollPolicy("Evaluation e1", interval_seconds=30, timeout_seconds=None)
        assert policy.after_error(_throttled(value)) == seconds

    def test_the_wait_never_sleeps_past_its_deadline(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=5, timeout_seconds=60)
        clock.now = 55.0
        assert policy.after_error(_throttled("30")) == 5.0
        clock.now = 58.0
        assert policy.after_pending() == 2.0

    def test_a_transient_error_at_the_deadline_raises_timeout_from_it(self, clock: _Clock) -> None:
        policy = PollPolicy("Trace evaluation te1", interval_seconds=3, timeout_seconds=300)
        throttled = _throttled("1")
        clock.now = 300.0
        with pytest.raises(TimeoutError) as raised:
            policy.after_error(throttled)
        assert raised.value.__cause__ is throttled
        assert str(raised.value).startswith("Trace evaluation te1 did not complete within 300 seconds")
        assert "the last poll failed" in str(raised.value)
        assert "429" in str(raised.value) or "Too Many Requests" in str(raised.value)

    def test_an_error_at_the_deadline_says_what_the_last_good_poll_saw(self, clock: _Clock) -> None:
        policy = PollPolicy("run r1", interval_seconds=5, timeout_seconds=60)
        policy.after_pending("run r1 is still in_progress after 60s")
        clock.now = 60.0
        with pytest.raises(TimeoutError, match=r"^run r1 is still in_progress after 60s; the last poll failed: "):
            policy.after_error(_throttled())

    def test_any_other_error_is_raised_as_it_is(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=5, timeout_seconds=60)
        denied = AuthenticationError("Unauthorized", response=_response(401), body=None)
        with pytest.raises(AuthenticationError) as raised:
            policy.after_error(denied)
        assert raised.value is denied

    def test_without_a_timeout_it_waits_through_errors_as_long_as_it_takes(self, clock: _Clock) -> None:
        policy = PollPolicy("Evaluation e1", interval_seconds=30, timeout_seconds=None)
        clock.now = 1e9
        assert [policy.after_error(_throttled()) for _ in range(8)][-1] == 30.0
        assert policy.after_pending() == 30.0
