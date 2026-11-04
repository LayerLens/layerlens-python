"""LAY-4429: an evaluation wait keeps polling through a 429, a 5xx or a lost connection.

Production throttles an API key at about 5 requests a second, so parallel CI jobs on one
key get 429s. The client retries a request twice; once those are spent, the wait used to
end with ``RateLimitError`` while the evaluation went on running on the server.

The clients are the real ``LayerLens`` and ``AsyncLayerLens``: only their transport is
fake, so the client's own retries, ``Retry-After`` handling and error mapping run as they
do against the API. Time is a fake clock that every sleep moves forward.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Union, Callable, Iterator, Optional
from email.utils import formatdate
from unittest.mock import patch

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens, _polling
from layerlens.models import Project, Evaluation, Organization, TraceEvaluationResultsResponse
from layerlens._resource import SyncAPIResource, AsyncAPIResource
from layerlens._exceptions import RateLimitError, AuthenticationError

ORG = "691254ecd44a29f6926881a2"
PROJECT = "691254eed44a29f6926881a3"
EVALUATION_ID = "68a1f0c2d4e5b6a7c8d9e001"
TRACE_EVALUATION_ID = "6a91c0e5f1d2a3b4c5d6e7f8"

EVALUATION_PATH = f"/api/v1/evaluations/{EVALUATION_ID}"
TRACE_EVALUATION_PATH = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/trace-evaluations/{TRACE_EVALUATION_ID}"
RESULTS_PATH = f"{TRACE_EVALUATION_PATH}/results"

Reply = Union[httpx.Response, Callable[[httpx.Request], httpx.Response]]


# --------------------------------------------------------------------------- #
# the fake API and the fake clock
# --------------------------------------------------------------------------- #


def evaluation(status: str) -> Dict[str, Any]:
    return {
        "id": EVALUATION_ID,
        "status": status,
        "submitted_at": 1752710400,
        "finished_at": 1752710460 if status == "success" else 0,
        "model_id": "68a1f0c2d4e5b6a7c8d9e010",
        "dataset_id": "68a1f0c2d4e5b6a7c8d9e020",
        "average_duration": 0,
        "accuracy": 0.0,
    }


def trace_evaluation(status: str) -> Dict[str, Any]:
    return {"id": TRACE_EVALUATION_ID, "trace_id": "t-1", "judge_id": "j-1", "status": status}


RESULTS = {
    "id": "result-1",
    "trace_evaluation_id": TRACE_EVALUATION_ID,
    "trace_id": "t-1",
    "judge_id": "j-1",
    "score": 0.85,
    "passed": True,
    "reasoning": "Good output",
    "steps": [],
    "model": "judge-model",
    "turns": 1,
    "latency_ms": 2500,
    "prompt_tokens": 1500,
    "completion_tokens": 300,
    "total_cost": 0.0045,
    "created_at": "2026-11-04T12:00:05Z",
}


def ok(body: Dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def throttled(retry_after: Optional[str] = "7") -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, json={"message": "Too Many Requests"}, headers=headers)


def unavailable() -> httpx.Response:
    return httpx.Response(503, json={"message": "Service Unavailable"})


def connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class FakeAPI:
    """Serves queued replies per path; the last one repeats. Records every request."""

    def __init__(self) -> None:
        self.routes: Dict[str, List[Reply]] = {}
        self.requests: List[str] = []

    def on(self, path: str, *replies: Reply) -> None:
        self.routes.setdefault(path, []).extend(replies)

    def count(self, path: str) -> int:
        return self.requests.count(path)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        queue = self.routes.get(path)
        if not queue:
            return httpx.Response(404, json={"message": f"unrouted in test: {path}"})
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return reply(request) if callable(reply) else reply


class FakeClock:
    """Time that only moves when something sleeps. ``waits`` are the waiter's own sleeps,
    ``retry_pauses`` the client's sleeps between retries of one request; both move the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: List[float] = []
        self.woke_at: List[float] = []
        self.retry_pauses: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds
        self.woke_at.append(self.now)

    def retry_pause(self, seconds: float) -> None:
        self.retry_pauses.append(seconds)
        self.now += seconds


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()

    async def async_wait(_self: Any, seconds: float) -> None:
        fake.wait(seconds)

    async def async_retry_pause(seconds: float) -> None:
        fake.retry_pause(seconds)

    monkeypatch.setattr(_polling, "time", fake)
    monkeypatch.setattr(SyncAPIResource, "_sleep", lambda _self, seconds: fake.wait(seconds))
    monkeypatch.setattr(AsyncAPIResource, "_sleep", async_wait)
    monkeypatch.setattr("layerlens._base_client.time.sleep", fake.retry_pause)
    # The async client imports asyncio inside its request method.
    monkeypatch.setattr("asyncio.sleep", async_retry_pause)
    return fake


_ORGANIZATION = Organization(id=ORG, name="Org", projects=[Project(id=PROJECT, name="Starter")])


@pytest.fixture(params=["sync", "async"])
def client(request: pytest.FixtureRequest, api: FakeAPI) -> Iterator[Union[LayerLens, AsyncLayerLens]]:
    cls = LayerLens if request.param == "sync" else AsyncLayerLens
    with patch.object(cls, "_get_organization", return_value=_ORGANIZATION):
        made = cls(api_key="test-key", base_url="https://api.test/api/v1")
    made._transport = httpx.MockTransport(api.handle)  # type: ignore[assignment]
    yield made


def run(result: Any) -> Any:
    """The waiter's result, awaited when the client is async."""
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def wait_for_evaluation(client: Union[LayerLens, AsyncLayerLens], **kw: Any) -> Optional[Evaluation]:
    pending = Evaluation(**evaluation("in-progress"))
    return run(client.evaluations.wait_for_completion(pending, **kw))  # type: ignore[no-any-return]


def wait_for_trace_evaluation(
    client: Union[LayerLens, AsyncLayerLens], **kw: Any
) -> Optional[TraceEvaluationResultsResponse]:
    return run(client.trace_evaluations.wait_for_completion(TRACE_EVALUATION_ID, **kw))  # type: ignore[no-any-return]


# Every kind of transient failure, with the waits that follow three of them in a row: a
# 429 with Retry-After (RateLimitError), a 503 (InternalServerError), and a refused
# connection (APIConnectionError). The backoff starts at half a second and doubles; a
# Retry-After longer than that is waited out.
TRANSIENT = [
    pytest.param(lambda: throttled("7"), [7.0, 7.0, 7.0], id="429"),
    pytest.param(unavailable, [0.5, 1.0, 2.0], id="503"),
    pytest.param(lambda: connection_refused, [0.5, 1.0, 2.0], id="connection"),
]


# --------------------------------------------------------------------------- #
# client.evaluations.wait_for_completion
# --------------------------------------------------------------------------- #


class TestEvaluationWait:
    @pytest.mark.parametrize("reply, backoff", TRANSIENT)
    def test_a_transient_failure_mid_wait_is_ridden_out(
        self, client: Any, api: FakeAPI, clock: FakeClock, reply: Callable[[], Reply], backoff: List[float]
    ) -> None:
        # One poll in progress, three failed polls, then the evaluation is done.
        api.on(EVALUATION_PATH, ok(evaluation("in-progress")), reply(), reply(), reply(), ok(evaluation("success")))

        done = wait_for_evaluation(client, interval_seconds=5, timeout_seconds=600)

        assert done is not None
        assert done.status == "success"
        assert api.count(EVALUATION_PATH) == 5
        assert clock.waits == [5.0, *backoff]
        # Each poll is one request: the wait retries, the client does not.
        assert clock.retry_pauses == []

    def test_the_wait_after_a_429_honours_retry_after(self, client: Any, api: FakeAPI, clock: FakeClock) -> None:
        api.on(EVALUATION_PATH, throttled("40"), ok(evaluation("success")))

        done = wait_for_evaluation(client, interval_seconds=5, timeout_seconds=600)

        assert done is not None and done.status == "success"
        # The client's own retries would have waited at most 8s: the wait waits what the API asked.
        assert clock.waits == [40.0]
        assert api.count(EVALUATION_PATH) == 2

    def test_a_retry_after_given_as_an_http_date_is_honoured(
        self, client: Any, api: FakeAPI, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 1_793_000_000.0
        monkeypatch.setattr("layerlens._base_client.time.time", lambda: now)
        api.on(EVALUATION_PATH, throttled(formatdate(now + 25, usegmt=True)), ok(evaluation("success")))

        done = wait_for_evaluation(client, interval_seconds=5, timeout_seconds=600)

        assert done is not None and done.status == "success"
        assert clock.waits == [25.0]

    @pytest.mark.parametrize("retry_after", ["1e300", "Fri, 31 Dec 9999 23:59:59 GMT"])
    def test_without_a_timeout_a_huge_retry_after_pauses_five_minutes(
        self, client: Any, api: FakeAPI, clock: FakeClock, retry_after: str
    ) -> None:
        # The default wait has no timeout, so no deadline bounds the pause: a real
        # time.sleep(1e300) raises OverflowError, and the async wait would never wake.
        api.on(EVALUATION_PATH, throttled(retry_after), ok(evaluation("success")))

        done = wait_for_evaluation(client, interval_seconds=5)

        assert done is not None and done.status == "success"
        assert clock.waits == [300.0]

    def test_429s_until_the_timeout_raise_timeout_error(self, client: Any, api: FakeAPI, clock: FakeClock) -> None:
        api.on(EVALUATION_PATH, throttled("7"))

        with pytest.raises(TimeoutError, match=f"Evaluation {EVALUATION_ID} did not complete within 60 seconds") as e:
            wait_for_evaluation(client, interval_seconds=5, timeout_seconds=60)

        assert isinstance(e.value.__cause__, RateLimitError)
        assert "the last poll failed" in str(e.value)
        # It polled through the whole minute, never sooner than Retry-After asked, one
        # request per poll, and raised at the deadline, not after it.
        assert clock.waits == [7.0] * 8 + [4.0]
        assert api.count(EVALUATION_PATH) == len(clock.waits) + 1
        assert clock.retry_pauses == []
        assert clock.now == 60.0

    def test_an_error_that_is_not_transient_ends_the_wait_at_once(
        self, client: Any, api: FakeAPI, clock: FakeClock
    ) -> None:
        api.on(EVALUATION_PATH, ok(evaluation("in-progress")), httpx.Response(401, json={"message": "bad key"}))

        with pytest.raises(AuthenticationError):
            wait_for_evaluation(client, interval_seconds=5, timeout_seconds=600)

        assert api.count(EVALUATION_PATH) == 2
        assert clock.waits == [5.0]

    def test_still_running_at_the_timeout_raises_timeout_error_as_before(
        self, client: Any, api: FakeAPI, clock: FakeClock
    ) -> None:
        api.on(EVALUATION_PATH, ok(evaluation("in-progress")))

        with pytest.raises(TimeoutError, match=f"^Evaluation {EVALUATION_ID} did not complete within 60 seconds$"):
            wait_for_evaluation(client, interval_seconds=30, timeout_seconds=60)

        assert clock.waits == [30.0, 30.0]


# --------------------------------------------------------------------------- #
# client.trace_evaluations.wait_for_completion
# --------------------------------------------------------------------------- #


class TestTraceEvaluationWait:
    @pytest.mark.parametrize("reply, backoff", TRANSIENT)
    def test_a_transient_failure_mid_wait_is_ridden_out(
        self, client: Any, api: FakeAPI, clock: FakeClock, reply: Callable[[], Reply], backoff: List[float]
    ) -> None:
        api.on(
            TRACE_EVALUATION_PATH,
            ok(trace_evaluation("pending")),
            reply(),
            reply(),
            reply(),
            ok(trace_evaluation("success")),
        )
        api.on(RESULTS_PATH, ok(RESULTS))

        results = wait_for_trace_evaluation(client, interval_seconds=3, timeout_seconds=300)

        assert isinstance(results, TraceEvaluationResultsResponse)
        assert results.score == 0.85
        assert clock.waits == [3.0, *backoff]
        assert clock.retry_pauses == []

    def test_a_throttled_results_read_is_ridden_out_without_polling_the_status_again(
        self, client: Any, api: FakeAPI, clock: FakeClock
    ) -> None:
        api.on(TRACE_EVALUATION_PATH, ok(trace_evaluation("success")))
        api.on(RESULTS_PATH, throttled("4"), ok(RESULTS))

        results = wait_for_trace_evaluation(client, interval_seconds=3, timeout_seconds=300)

        assert isinstance(results, TraceEvaluationResultsResponse)
        assert api.count(TRACE_EVALUATION_PATH) == 1
        assert clock.waits == [4.0]

    def test_429s_until_the_timeout_raise_timeout_error(self, client: Any, api: FakeAPI, clock: FakeClock) -> None:
        api.on(TRACE_EVALUATION_PATH, ok(trace_evaluation("pending")), throttled("7"))

        with pytest.raises(
            TimeoutError, match=f"Trace evaluation {TRACE_EVALUATION_ID} did not complete within 300 seconds"
        ) as e:
            wait_for_trace_evaluation(client, interval_seconds=3, timeout_seconds=300)

        assert isinstance(e.value.__cause__, RateLimitError)
        assert clock.now == 300.0
        assert clock.retry_pauses == []
        assert api.count(RESULTS_PATH) == 0

    def test_an_error_that_is_not_transient_ends_the_wait_at_once(
        self, client: Any, api: FakeAPI, clock: FakeClock
    ) -> None:
        api.on(TRACE_EVALUATION_PATH, httpx.Response(401, json={"message": "bad key"}))

        with pytest.raises(AuthenticationError):
            wait_for_trace_evaluation(client, interval_seconds=3, timeout_seconds=300)

        assert api.count(TRACE_EVALUATION_PATH) == 1
        assert clock.waits == []

    def test_a_failed_evaluation_still_returns_none(self, client: Any, api: FakeAPI, clock: FakeClock) -> None:
        api.on(TRACE_EVALUATION_PATH, ok(trace_evaluation("failure")))
        api.on(RESULTS_PATH, httpx.Response(404, json={"message": "no results"}))

        assert wait_for_trace_evaluation(client, interval_seconds=3, timeout_seconds=300) is None
        assert clock.waits == []
