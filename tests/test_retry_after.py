"""``Retry-After`` as RFC 9110 defines it: delay-seconds or an HTTP-date (LAY-4429).

The client used ``float()`` on the header, so a reply with an HTTP-date (``Retry-After:
Wed, 21 Oct 2026 07:28:00 GMT``), or with a negative number, raised ``ValueError`` from
inside the request: not an SDK error, so nothing above it could catch it as one.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional
from email.utils import formatdate

import httpx
import pytest

from layerlens import _base_client
from layerlens._base_client import MAX_RETRY_DELAY, INITIAL_RETRY_DELAY, BaseClient, BaseAsyncClient, parse_retry_after

NOW = 1_793_000_000.0  # a fixed "now" for the HTTP-date cases


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(_base_client.time, "time", lambda: NOW)
    return NOW


def http_date(seconds_from_now: float) -> str:
    return formatdate(NOW + seconds_from_now, usegmt=True)


class TestParseRetryAfter:
    @pytest.mark.parametrize("value, seconds", [("0", 0.0), ("7", 7.0), (" 12 ", 12.0), ("1.5", 1.5)])
    def test_delay_seconds(self, value: str, seconds: float) -> None:
        assert parse_retry_after(value) == seconds

    def test_an_http_date_is_the_seconds_until_then(self, fixed_now: float) -> None:
        assert parse_retry_after(http_date(30)) == 30.0

    def test_an_http_date_in_the_past_is_now(self, fixed_now: float) -> None:
        assert parse_retry_after(http_date(-30)) == 0.0

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "soon",
            "-3",
            "inf",
            "nan",
            "Infinity",
            "Wed, 99 Foo 2026",
            # Dates the parser accepts but the calendar cannot hold: ValueError, OverflowError.
            "Mon, 01 Jan 10000 00:00:00 GMT",
            "Mon, 01 Jan 99999999999 00:00:00 GMT",
        ],
    )
    def test_no_usable_delay_is_none(self, value: Optional[str]) -> None:
        assert parse_retry_after(value) is None


def _replies(*responses: httpx.Response) -> httpx.MockTransport:
    queue: List[httpx.Response] = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.MockTransport(handle)


def _throttled(retry_after: str) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": retry_after}, json={"message": "Too Many Requests"})


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.mark.parametrize(
    "retry_after, expected_sleep",
    [
        (None, INITIAL_RETRY_DELAY),  # http_date(5), filled in below
        ("Wed, 99 Foo 2026 07:28:00 GMT", INITIAL_RETRY_DELAY),
        ("-3", INITIAL_RETRY_DELAY),
        ("nan", INITIAL_RETRY_DELAY),
        ("3", 3.0),
        ("60", MAX_RETRY_DELAY),
    ],
    ids=["http-date", "unreadable-date", "negative", "nan", "seconds", "capped"],
)
class TestClientRetrySleep:
    """A throttled request is sent again after the header's delay, capped at 8 s, or after
    the client's own backoff when the header holds no usable delay; never an exception."""

    def test_sync(
        self, fixed_now: float, monkeypatch: pytest.MonkeyPatch, retry_after: Optional[str], expected_sleep: float
    ) -> None:
        header = http_date(5) if retry_after is None else retry_after
        expected = 5.0 if retry_after is None else expected_sleep
        slept: List[float] = []
        monkeypatch.setattr(_base_client.time, "sleep", slept.append)
        client = BaseClient(base_url="https://api.test", transport=_replies(_throttled(header), _ok()))

        response = client._request_cast("GET", "/thing")

        assert isinstance(response, httpx.Response) and response.status_code == 200
        assert slept == [expected]

    def test_async(
        self, fixed_now: float, monkeypatch: pytest.MonkeyPatch, retry_after: Optional[str], expected_sleep: float
    ) -> None:
        header = http_date(5) if retry_after is None else retry_after
        expected = 5.0 if retry_after is None else expected_sleep
        slept: List[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", sleep)
        client = BaseAsyncClient(base_url="https://api.test", transport=_replies(_throttled(header), _ok()))

        response = asyncio.run(client._request_cast("GET", "/thing"))

        assert isinstance(response, httpx.Response) and response.status_code == 200
        assert slept == [expected]
