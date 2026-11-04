"""The environments waits, sync and async, on a throttled API key (LAY-4429).

A wait sends each poll once and does its own retrying, as the evaluation waits do: a
throttled poll costs one request, not three. When its deadline passes while the polls
keep failing, it raises ``TimeoutError`` from the last error, as it does when the thing
waited for is still going, so ``layerlens env`` exits 11 for both. It raised the 429
itself before, which the CLI turned into exit 1, and ``exec`` then left its run going.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Callable

import pytest

from layerlens._resource import SyncAPIResource, AsyncAPIResource
from layerlens._exceptions import RateLimitError, PermissionDeniedError

from .conftest import ENV_ID, RUN_ID, FakeAPI, ok, err, row, run_view, environment

JOB_ID = "6a8f2c1d9e0b4a7f3c5d6e99"
RUN = f"/{ENV_ID}/runs/{RUN_ID}"
JOB = f"/{ENV_ID}/jobs/{JOB_ID}"
BUILD = f"/{ENV_ID}/build"

THROTTLED = err(429, "Too Many Requests", {"Retry-After": "1"})


def job(status: str) -> Dict[str, Any]:
    return {"id": JOB_ID, "kind": "generate_tasks", "status": status, "created_at": "2026-11-04T12:00:00Z"}


def _run_wait(client: Any, **kw: Any) -> Any:
    return client.environments.runs.wait(ENV_ID, RUN_ID, **kw)


def _job_wait(client: Any, **kw: Any) -> Any:
    return client.environments.jobs.wait(ENV_ID, JOB_ID, **kw)


def _ready_wait(client: Any, **kw: Any) -> Any:
    return client.environments.wait_until_ready(ENV_ID, **kw)


# (path polled, a reply that means "still going", the call, how its timeout reads)
WAITS = [
    pytest.param(RUN, ok(run_view(row("x", None), status="in_progress")), _run_wait, f"run {RUN_ID}", id="runs.wait"),
    pytest.param(JOB, ok(job("in_progress")), _job_wait, f"job {JOB_ID}", id="jobs.wait"),
    pytest.param(BUILD, ok({"status": "building", "steps": []}), _ready_wait, f"environment {ENV_ID}", id="ready"),
]


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    """The waits' own sleeps, apart from the client's sleeps between retries of one request."""
    slept: List[float] = []

    async def async_sleep(_self: Any, seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(SyncAPIResource, "_sleep", lambda _self, seconds: slept.append(seconds))
    monkeypatch.setattr(AsyncAPIResource, "_sleep", async_sleep)
    return slept


@pytest.fixture(params=["sync", "async"])
def any_client(request: pytest.FixtureRequest, client: Any, async_client: Any) -> Any:
    return client if request.param == "sync" else async_client


def call(wait: Callable[..., Any], client: Any, **kw: Any) -> Any:
    result = wait(client, **kw)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


@pytest.mark.parametrize("path, going, wait, what", WAITS)
def test_a_poll_throttled_at_the_deadline_raises_timeout_error_from_the_429(
    any_client: Any, api: FakeAPI, path: str, going: Any, wait: Callable[..., Any], what: str
) -> None:
    api.on("GET", path, THROTTLED)

    with pytest.raises(TimeoutError, match=f"^{what} did not complete within 0 seconds; the last poll failed: ") as e:
        call(wait, any_client, timeout_seconds=0)

    assert isinstance(e.value.__cause__, RateLimitError)
    # One request: the poll was sent once, not with the client's two retries.
    assert len(api.calls("GET", path)) == 1


@pytest.mark.parametrize("path, going, wait, what", WAITS)
def test_still_going_at_the_deadline_keeps_its_message(
    any_client: Any, api: FakeAPI, path: str, going: Any, wait: Callable[..., Any], what: str
) -> None:
    api.on("GET", path, going)

    with pytest.raises(TimeoutError, match=f"^{what} is still (in_progress|building) after 0s$"):
        call(wait, any_client, timeout_seconds=0)


def test_an_error_after_a_good_poll_says_what_that_poll_saw(client: Any, api: FakeAPI) -> None:
    api.on("GET", RUN, ok(run_view(row("x", None), status="in_progress")), THROTTLED)
    clock = {"now": 0.0}

    def tick(seconds: float) -> None:
        clock["now"] += seconds

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("layerlens._polling.time.monotonic", lambda: clock["now"])
        mp.setattr(type(client.environments), "_sleep", lambda _self, seconds: tick(seconds))
        with pytest.raises(
            TimeoutError, match=rf"^run {RUN_ID} is still in_progress after 30s; the last poll failed: "
        ):
            client.environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=30, poll_interval_seconds=5)
    assert clock["now"] == 30.0


@pytest.mark.parametrize("path, going, wait, what", WAITS)
def test_throttled_polls_are_ridden_out_one_request_each(
    any_client: Any, api: FakeAPI, waits: List[float], path: str, going: Any, wait: Callable[..., Any], what: str
) -> None:
    done = {
        RUN: ok(run_view(row("x", "pass"))),
        JOB: ok(job("success")),
        BUILD: ok({"status": "ready", "version": 1, "steps": []}),
    }[path]
    api.on("GET", path, going, THROTTLED, err(503, "Service Unavailable"), done)
    api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))

    call(wait, any_client, poll_interval_seconds=0.01)

    # Four polls, a wait after each of the first three. With the client's own retries
    # inside a poll, the 429 and the 503 were retried there, and the wait slept once.
    assert len(api.calls("GET", path)) == 4
    # After the poll still going: the interval. After the 429: its Retry-After, 1 s. After
    # the 503: the backoff, which starts at half a second.
    assert waits == [0.01, 1.0, 0.5]


def test_a_throttled_read_of_the_ready_environment_is_ridden_out(client: Any, api: FakeAPI) -> None:
    api.on("GET", BUILD, ok({"status": "ready", "version": 1, "steps": []}))
    api.on("GET", f"/{ENV_ID}", THROTTLED, ok({"environment": environment()}))

    found = client.environments.wait_until_ready(ENV_ID, poll_interval_seconds=0.01)

    assert found.id == ENV_ID


def test_a_refusal_still_ends_a_wait_at_once(any_client: Any, api: FakeAPI, waits: List[float]) -> None:
    api.on("GET", RUN, err(403, "this key has Standard access"))

    with pytest.raises(PermissionDeniedError):
        call(_run_wait, any_client, timeout_seconds=600)

    assert len(api.calls("GET", RUN)) == 1
    assert waits == []
