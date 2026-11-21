"""A wait with a deadline keeps to it, even when the client has no timeout.

Each poll of a wait used the client's timeout. With ``LayerLens(timeout=None)`` a poll the API
accepted and never answered blocked forever, though ``timeout_seconds`` is the longest the wait
takes: the deadline was checked only between polls (1.11.0 bounded each poll to 600 seconds).
Now each request of a wait is sent with the client's timeout cut to the time left (httpx applies
it to connecting and to each wait for data), a shorter client timeout still wins, and no request
is sent once the deadline has passed. So that the last poll can still answer, the wait sends it
``MIN_POLL_SECONDS`` before the deadline. These tests run against local servers, with the
production ``MIN_POLL_SECONDS``.
"""

from __future__ import annotations

import json
import time
import asyncio
import threading
from typing import Any, Dict, List, Callable, Iterator, Optional
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens, _polling
from layerlens.models import Evaluation
from layerlens._polling import MIN_POLL_SECONDS, MIN_POLL_GAP_SECONDS, PollPolicy
from layerlens._resource import SyncAPIResource, AsyncAPIResource
from layerlens._constants import NotGiven
from tests.environments.conftest import ENV_ID, RUN_ID, ok, row, run_view

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
ORGANIZATION = {"id": ORG, "name": "Acme", "projects": [{"id": PROJECT, "name": "Default"}]}
ENVIRONMENTS = f"/api/v1/env/organizations/{ORG}/projects/{PROJECT}/environments/{ENV_ID}"
JOB_ID = "6a8c6a45b1c2d3e4f5a6b7c8"
CORPUS = Path(__file__).resolve().parent / "contract" / "corpus"
NEVER = 60.0


class _Server:
    """Answers every GET after ``delay`` seconds with ``replies[n]`` for the n-th request (the
    last one repeats); with ``delay`` of NEVER it never answers. Records each request's time."""

    def __init__(
        self, delay: float, replies: List[Dict[str, Any]], *, organization_after: Optional[float] = None
    ) -> None:
        self.delay, self.replies = delay, replies
        self.received: List[float] = []
        self.paths: List[str] = []
        self.done = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                path = self.path.split("?")[0]
                if path.endswith("/organizations") and organization_after is not None:
                    # The client's first organization read, answered after organization_after s.
                    server.organization_read = time.monotonic()
                    if server.done.wait(organization_after):
                        return
                    reply: Dict[str, Any] = {"status": "success", "data": ORGANIZATION}
                else:
                    index = len(server.received)
                    server.received.append(time.monotonic())
                    server.paths.append(path)
                    if server.done.wait(server.delay):
                        return
                    reply = server.replies[min(index, len(server.replies) - 1)]
                payload = json.dumps(reply).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:  # the client gave up first
                    pass

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.done.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def stalled() -> Iterator[_Server]:
    """A server that accepts every request and never answers."""
    server = _Server(NEVER, [{}])
    yield server
    server.close()


def _evaluation() -> Evaluation:
    return Evaluation(**json.loads((CORPUS / "evaluations_get_many.json").read_text())["evaluations"][0])


def _client(base_url: str, timeout: Any = None) -> LayerLens:
    client = LayerLens(api_key="k", base_url=base_url, timeout=timeout, max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    return client


def _async_client(base_url: str, timeout: Any = None) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="k", base_url=base_url, timeout=timeout, max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    return client


def _waits(seconds: float) -> "Dict[str, Callable[[str, Any], Any]]":
    """The ten waits of the SDK, each a call (base URL, client timeout) waiting ``seconds``."""
    return {
        "evaluations": lambda url, t: _client(url, t).evaluations.wait_for_completion(
            _evaluation(), interval_seconds=5, timeout_seconds=seconds
        ),
        "trace_evaluations": lambda url, t: _client(url, t).trace_evaluations.wait_for_completion(
            "te-1", interval_seconds=5, timeout_seconds=seconds
        ),
        "environments.wait_until_ready": lambda url, t: _client(url, t).environments.wait_until_ready(
            ENV_ID, timeout_seconds=seconds
        ),
        "environments.jobs.wait": lambda url, t: _client(url, t).environments.jobs.wait(
            ENV_ID, JOB_ID, timeout_seconds=seconds
        ),
        "environments.runs.wait": lambda url, t: _client(url, t).environments.runs.wait(
            ENV_ID, RUN_ID, timeout_seconds=seconds
        ),
        "async evaluations": lambda url, t: asyncio.run(
            _async_client(url, t).evaluations.wait_for_completion(
                _evaluation(), interval_seconds=5, timeout_seconds=seconds
            )
        ),
        "async trace_evaluations": lambda url, t: asyncio.run(
            _async_client(url, t).trace_evaluations.wait_for_completion(
                "te-1", interval_seconds=5, timeout_seconds=seconds
            )
        ),
        "async environments.wait_until_ready": lambda url, t: asyncio.run(
            _async_client(url, t).environments.wait_until_ready(ENV_ID, timeout_seconds=seconds)
        ),
        "async environments.jobs.wait": lambda url, t: asyncio.run(
            _async_client(url, t).environments.jobs.wait(ENV_ID, JOB_ID, timeout_seconds=seconds)
        ),
        "async environments.runs.wait": lambda url, t: asyncio.run(
            _async_client(url, t).environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=seconds)
        ),
    }


ONE_SECOND = _waits(1)
TWO_SECONDS = _waits(2)

# The resource GET each wait polls, which the server leaves unanswered.
POLLED = {
    "evaluations": f"/api/v1/evaluations/{_evaluation().id}",
    "trace_evaluations": f"/api/v1/organizations/{ORG}/projects/{PROJECT}/trace-evaluations/te-1",
    "environments.wait_until_ready": f"{ENVIRONMENTS}/build",
    "environments.jobs.wait": f"{ENVIRONMENTS}/jobs/{JOB_ID}",
    "environments.runs.wait": f"{ENVIRONMENTS}/runs/{RUN_ID}",
}


@pytest.mark.parametrize("name", list(ONE_SECOND))
def test_a_stalled_poll_ends_at_the_deadline_when_the_client_has_no_timeout(stalled: _Server, name: str) -> None:
    before = len(stalled.paths)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="the last poll failed"):
        ONE_SECOND[name](stalled.base_url, None)

    # By the 1-second deadline: not MIN_POLL_SECONDS (2 s) later, and not before it.
    assert 0.9 < time.monotonic() - started < 1.5
    # The request that stalled was the wait's own poll, sent once.
    assert stalled.paths[before:] == [POLLED[name.replace("async ", "")]]


@pytest.mark.parametrize(
    "wait",
    [TWO_SECONDS["environments.runs.wait"], TWO_SECONDS["async trace_evaluations"]],
    ids=["sync", "async"],
)
def test_a_shorter_client_timeout_still_wins(wait: Callable[[str, Any], Any]) -> None:
    server = _Server(NEVER, [{}])
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="the last poll failed"):
            wait(server.base_url, 0.2)
        elapsed = time.monotonic() - started
        received = list(server.received)
    finally:
        server.close()

    # The first poll gave up after the client's 0.2 s, not the 2 s left, so the wait polled
    # again half a second later, well before its deadline.
    assert len(received) >= 2
    assert received[1] - received[0] < 0.9
    assert elapsed < 2.5


GOING = ok(run_view(row("x", None), status="in_progress"))[1]
DONE = ok(run_view(row("x", "pass")))[1]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_the_last_poll_before_the_deadline_can_still_answer_done(asynchronous: bool) -> None:
    # Each poll takes 0.4 s to answer: going, then done. With a 3 s deadline and polls 10 s apart,
    # the wait sends its last poll 2 s before the deadline, which answers done in time. A poll
    # sent at the deadline, cut to the time left, could not have answered.
    server = _Server(0.4, [GOING, DONE])
    try:
        if asynchronous:
            client = _async_client(server.base_url)
            wait = client.environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=3, poll_interval_seconds=10)
            run = asyncio.run(wait)
        else:
            run = _client(server.base_url).environments.runs.wait(
                ENV_ID, RUN_ID, timeout_seconds=3, poll_interval_seconds=10
            )
        received = list(server.received)
    finally:
        server.close()

    assert run.finished and run.passed == 1
    assert len(received) == 2
    # The second poll went about 1 s in: 2 s before the 3 s deadline.
    assert 0.8 < received[1] - received[0] < 1.3


TRACE_EVALUATION_PENDING = {
    "status": "success",
    "data": {"id": "te-1", "trace_id": "t-1", "judge_id": "j-1", "status": "pending"},
}

# Each wait on a cold client (one that has not read its organization yet), waiting 1 s with polls
# 0.3 s apart: (call, what the server answers, the TimeoutError's message).
COLD: "Dict[str, Any]" = {
    "environments.runs.wait": (
        lambda client: client.environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=1, poll_interval_seconds=0.3),
        GOING,
        f"^run {RUN_ID} is still in_progress after 1s$",
    ),
    "trace_evaluations": (
        lambda client: client.trace_evaluations.wait_for_completion("te-1", interval_seconds=0.3, timeout_seconds=1),
        TRACE_EVALUATION_PENDING,
        "^Trace evaluation te-1 did not complete within 1 seconds$",
    ),
}


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("name", list(COLD))
def test_the_clients_first_organization_read_comes_before_the_waits_deadline(name: str, asynchronous: bool) -> None:
    # A cold client reads its organization once, before the wait starts: 0.8 s here, not counted
    # in the 1 s of the wait, which then polls (every 0.3 s) until its own deadline.
    wait, pending, message = COLD[name]
    server = _Server(0.0, [pending], organization_after=0.8)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match=message):
            if asynchronous:
                asyncio.run(wait(AsyncLayerLens(api_key="k", base_url=server.base_url, timeout=None, max_retries=0)))
            else:
                wait(LayerLens(api_key="k", base_url=server.base_url, timeout=None, max_retries=0))
        elapsed = time.monotonic() - started
        received = list(server.received)
    finally:
        server.close()

    assert received[0] - started >= 0.75  # the first poll went after the organization read
    # Polls through the wait's own second: at 0 and 0.3 s, the next would have had under half a
    # second left. Counted in the wait, the organization read would have left it one poll.
    assert len(received) >= 2
    assert 1.6 < elapsed < 2.3


def test_a_wait_of_no_time_checks_once() -> None:
    # timeout_seconds=0: one poll, given the client's timeout (at most MIN_POLL_SECONDS), so it
    # can answer, then TimeoutError saying what it saw; no second poll.
    server = _Server(0.3, [GOING])
    try:
        with pytest.raises(TimeoutError, match=f"^run {RUN_ID} is still in_progress after 0s$"):
            _client(server.base_url).environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=0)
        received = list(server.received)
    finally:
        server.close()

    assert len(received) == 1


class _DoneLateServer:
    """The status poll (a trace evaluation, a build) answers done in two parts, 0.6 s apart: each
    wait for data is shorter than the 1 s deadline, the whole answer longer. The read that would
    follow it (the results, the ready environment) never answers. Records the paths asked for."""

    TRACE_EVALUATION = {"id": "te-1", "trace_id": "t-1", "judge_id": "j-1", "status": "success"}
    BUILD = {"status": "ready", "version": 1, "steps": []}

    def __init__(self) -> None:
        self.paths: List[str] = []
        self.done = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                path = self.path.split("?")[0]
                server.paths.append(path)
                if path.endswith("/trace-evaluations/te-1") or path.endswith("/build"):
                    data = server.TRACE_EVALUATION if "trace-evaluations" in path else server.BUILD
                    payload = json.dumps({"status": "success", "data": data}).encode()
                    try:
                        if server.done.wait(0.6):
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        if server.done.wait(0.6):
                            return
                        self.wfile.write(payload)
                    except OSError:
                        pass
                    return
                server.done.wait(NEVER)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.done.set()
        self.httpd.shutdown()
        self.httpd.server_close()


# Each wait whose first poll answers done in two parts (_DoneLateServer), as a call (base URL,
# timeout_seconds): (that call, the status GET it polls, the read that follows a done status).
DONE_LATE: "Dict[str, Any]" = {
    "trace_evaluations": (
        lambda url, seconds: _client(url).trace_evaluations.wait_for_completion(
            "te-1", interval_seconds=5, timeout_seconds=seconds
        ),
        POLLED["trace_evaluations"],
        f"{POLLED['trace_evaluations']}/results",
    ),
    "async trace_evaluations": (
        lambda url, seconds: asyncio.run(
            _async_client(url).trace_evaluations.wait_for_completion(
                "te-1", interval_seconds=5, timeout_seconds=seconds
            )
        ),
        POLLED["trace_evaluations"],
        f"{POLLED['trace_evaluations']}/results",
    ),
    "environments.wait_until_ready": (
        lambda url, seconds: _client(url).environments.wait_until_ready(ENV_ID, timeout_seconds=seconds),
        POLLED["environments.wait_until_ready"],
        ENVIRONMENTS,
    ),
    "async environments.wait_until_ready": (
        lambda url, seconds: asyncio.run(
            _async_client(url).environments.wait_until_ready(ENV_ID, timeout_seconds=seconds)
        ),
        POLLED["environments.wait_until_ready"],
        ENVIRONMENTS,
    ),
}


@pytest.mark.parametrize("name", list(DONE_LATE))
def test_a_deadline_that_runs_out_during_the_first_poll_gives_the_next_read_no_time(name: str) -> None:
    # The first poll answers done after the 1 s deadline has passed. The read that follows is not
    # sent: the 2 s allowance is only for a wait whose deadline was gone before it began.
    wait, status, _ = DONE_LATE[name]
    server = _DoneLateServer()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="did not complete within 1 seconds$"):
            wait(server.base_url, 1)
        elapsed = time.monotonic() - started
        paths = list(server.paths)
    finally:
        server.close()

    assert paths == [status]
    assert elapsed < 1.7


@pytest.mark.parametrize("name", list(DONE_LATE))
def test_a_first_poll_that_answers_done_in_time_is_followed_by_its_read(name: str) -> None:
    # The control for the test above: the same done answer (about 1.2 s) within a 3 s deadline
    # is followed by the read, which the server never answers, so the wait ends at its deadline.
    wait, status, follow_up = DONE_LATE[name]
    server = _DoneLateServer()
    try:
        with pytest.raises(TimeoutError, match="did not complete within 3 seconds; the last poll failed"):
            wait(server.base_url, 3)
        paths = list(server.paths)
    finally:
        server.close()

    assert paths[:2] == [status, follow_up]


class TestPollTimeout:
    def test_without_a_deadline_the_poll_uses_the_clients_timeout(self) -> None:
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=None)
        # Unbounded only when the client's timeout is None too: httpx then waits forever.
        assert isinstance(policy.poll_timeout(httpx.Timeout(None)), NotGiven)
        assert isinstance(policy.poll_timeout(httpx.Timeout(5.0)), NotGiven)

    def test_with_a_deadline_and_no_client_timeout_the_poll_ends_by_the_deadline(self) -> None:
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=30)
        timeout = policy.poll_timeout(httpx.Timeout(None))
        assert isinstance(timeout, httpx.Timeout)
        for part in (timeout.connect, timeout.read, timeout.write, timeout.pool):
            assert part is not None and 29 < part <= 30

    def test_a_shorter_client_timeout_still_applies(self) -> None:
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=30)
        timeout = policy.poll_timeout(httpx.Timeout(600, connect=5.0))
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == 5.0
        assert timeout.read is not None and 29 < timeout.read <= 30

    def test_a_deadline_gone_before_the_first_poll_allows_one_check(self) -> None:
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=0)
        unbounded = policy.poll_timeout(httpx.Timeout(None))
        shorter = policy.poll_timeout(httpx.Timeout(0.5))
        assert isinstance(unbounded, httpx.Timeout) and isinstance(shorter, httpx.Timeout)
        assert unbounded.read == MIN_POLL_SECONDS
        assert shorter.read == 0.5

    def test_a_positive_deadline_gone_before_the_first_request_also_allows_one_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not only timeout_seconds=0: a 1 s wait whose first request comes 1.5 s late (a thread
        # scheduled late) still checks once, with at most MIN_POLL_SECONDS, then sends nothing more.
        now = {"t": 0.0}
        monkeypatch.setattr("layerlens._polling.time.monotonic", lambda: now["t"])
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=1)
        now["t"] = 1.5
        timeout = policy.poll_timeout(httpx.Timeout(None))
        assert isinstance(timeout, httpx.Timeout) and timeout.read == MIN_POLL_SECONDS
        with pytest.raises(TimeoutError, match="^x did not complete within 1 seconds$"):
            policy.after_pending()
        with pytest.raises(TimeoutError):
            policy.poll_timeout(httpx.Timeout(None))

    def test_a_deadline_that_ran_out_after_the_first_request_allows_no_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = {"t": 0.0}
        monkeypatch.setattr("layerlens._polling.time.monotonic", lambda: now["t"])
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=1)
        assert isinstance(policy.poll_timeout(httpx.Timeout(None)), httpx.Timeout)
        now["t"] = 1.5  # the first poll answered after the deadline: the next read is not sent
        with pytest.raises(TimeoutError, match="^x did not complete within 1 seconds$"):
            policy.poll_timeout(httpx.Timeout(None))

    def test_no_poll_is_sent_once_the_deadline_has_passed(self) -> None:
        policy = PollPolicy("x", interval_seconds=1, timeout_seconds=0)
        policy.poll_timeout(httpx.Timeout(None))
        with pytest.raises(TimeoutError, match="^x did not complete within 0 seconds$"):
            policy.after_pending()
        with pytest.raises(TimeoutError):
            policy.poll_timeout(httpx.Timeout(None))


class _ScriptedServer:
    """Answers the n-th GET with ``steps[n]`` (the last repeats): (delay, status, headers, body).
    Records each request's arrival on the monotonic clock."""

    def __init__(self, steps: List[Any]) -> None:
        self.steps = steps
        self.received: List[float] = []
        self.done = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                index = len(server.received)
                server.received.append(time.monotonic())
                delay, status, headers, body = server.steps[min(index, len(server.steps) - 1)]
                if server.done.wait(delay):
                    return
                payload = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:  # the client gave up first
                    pass

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.done.set()
        self.httpd.shutdown()
        self.httpd.server_close()


PENDING_EVALUATION = {
    **json.loads((CORPUS / "evaluations_get_many.json").read_text())["evaluations"][0],
    "status": "in-progress",
}
THROTTLED = {"status": "error", "error": "too many requests"}


def test_a_retry_after_ending_just_before_the_deadline_leaves_the_429_as_the_cause() -> None:
    # m3: the 429 comes 0.4 s into a 3 s wait and asks for 2.5 s, which ends about 0.1 s before the
    # deadline. The poll sent then had that long to answer and the API takes 0.3 s: its timeout,
    # not the 429, became the TimeoutError's cause. Now nothing is sent after the pause.
    server = _ScriptedServer([(0.4, 429, {"Retry-After": "2.5"}, THROTTLED), (0.3, 200, {}, PENDING_EVALUATION)])
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="the last poll failed") as raised:
            _client(server.base_url).evaluations.wait_for_completion(
                _evaluation(), interval_seconds=1, timeout_seconds=3
            )
        elapsed = time.monotonic() - started
        received = list(server.received)
    finally:
        server.close()

    assert type(raised.value.__cause__).__name__ == "RateLimitError"
    assert len(received) == 1
    assert 2.9 < elapsed < 3.6


def test_a_poll_answered_just_over_2s_before_the_deadline_is_not_followed_by_another_at_once() -> None:
    # m4: polls 1 s apart in a 3.1 s wait: the second answers with ~2.1 s left, and the wait aimed
    # another at 2 s before the deadline, some 0.1 s later. Now the next one follows the interval.
    server = _ScriptedServer([(0.0, 200, {}, PENDING_EVALUATION)])
    try:
        with pytest.raises(TimeoutError):
            _client(server.base_url).evaluations.wait_for_completion(
                _evaluation(), interval_seconds=1, timeout_seconds=3.1
            )
        received = list(server.received)
    finally:
        server.close()

    gaps = [later - earlier for earlier, later in zip(received, received[1:])]
    assert len(received) >= 3
    assert min(gaps) > 0.4, gaps


class _LateAPI:
    """Answers every poll pending, ``LATE`` seconds after it went out, on a clock the test moves;
    a request given less time than that times out. Records the time each request had left."""

    LATE = 0.11

    def __init__(self, deadline: float) -> None:
        self.now, self.deadline = 0.0, deadline
        self.left: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.left.append(self.deadline - self.now)
        read = request.extensions["timeout"]["read"]
        if read is not None and read < self.LATE:
            self.now += read
            raise httpx.ReadTimeout("timed out", request=request)
        self.now += self.LATE
        return httpx.Response(200, json=LATE_PENDING[request.url.path])


LATE_PENDING: "Dict[str, Any]" = {
    POLLED["evaluations"]: PENDING_EVALUATION,
    POLLED["trace_evaluations"]: TRACE_EVALUATION_PENDING,
    POLLED["environments.wait_until_ready"]: ok({"status": "building", "steps": []})[1],
    POLLED["environments.jobs.wait"]: ok(
        {"id": JOB_ID, "kind": "generate_tasks", "status": "in_progress", "created_at": "2026-11-04T12:00:00Z"}
    )[1],
    POLLED["environments.runs.wait"]: GOING,
}


def _on(api: _LateAPI, client: Any) -> Any:
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api.handle)
    return client


def _sync(api: _LateAPI) -> LayerLens:
    return _on(api, LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0))


def _async(api: _LateAPI) -> AsyncLayerLens:
    return _on(api, AsyncLayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0))


# The ten waits, polling every second, each with a timeout_seconds after which its last poll went
# out a few ms before the deadline: 4.48 s for the evaluation waits, 5.13 s for the environments
# waits, whose interval grows by half after each poll.
SLIVER: "Dict[str, Callable[[_LateAPI], Any]]" = {
    "evaluations": lambda api: _sync(api).evaluations.wait_for_completion(
        _evaluation(), interval_seconds=1, timeout_seconds=api.deadline
    ),
    "trace_evaluations": lambda api: _sync(api).trace_evaluations.wait_for_completion(
        "te-1", interval_seconds=1, timeout_seconds=api.deadline
    ),
    "environments.wait_until_ready": lambda api: _sync(api).environments.wait_until_ready(
        ENV_ID, timeout_seconds=api.deadline, poll_interval_seconds=1
    ),
    "environments.jobs.wait": lambda api: _sync(api).environments.jobs.wait(
        ENV_ID, JOB_ID, timeout_seconds=api.deadline, poll_interval_seconds=1
    ),
    "environments.runs.wait": lambda api: _sync(api).environments.runs.wait(
        ENV_ID, RUN_ID, timeout_seconds=api.deadline, poll_interval_seconds=1
    ),
    "async evaluations": lambda api: asyncio.run(
        _async(api).evaluations.wait_for_completion(_evaluation(), interval_seconds=1, timeout_seconds=api.deadline)
    ),
    "async trace_evaluations": lambda api: asyncio.run(
        _async(api).trace_evaluations.wait_for_completion("te-1", interval_seconds=1, timeout_seconds=api.deadline)
    ),
    "async environments.wait_until_ready": lambda api: asyncio.run(
        _async(api).environments.wait_until_ready(ENV_ID, timeout_seconds=api.deadline, poll_interval_seconds=1)
    ),
    "async environments.jobs.wait": lambda api: asyncio.run(
        _async(api).environments.jobs.wait(ENV_ID, JOB_ID, timeout_seconds=api.deadline, poll_interval_seconds=1)
    ),
    "async environments.runs.wait": lambda api: asyncio.run(
        _async(api).environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=api.deadline, poll_interval_seconds=1)
    ),
}


@pytest.mark.parametrize("name", list(SLIVER))
def test_no_poll_after_the_first_goes_out_with_under_half_a_second_left(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V2: polls 1 s apart, each answered 0.1 s late; with timeout_seconds=4.48 the last went out
    # 7 ms before the deadline, timed out, and its timeout became the TimeoutError's cause. Now the
    # wait sleeps out such a sliver and raises from what the last poll saw.
    api = _LateAPI(5.13 if "environments" in name else 4.48)

    async def sleep_async(_resource: Any, seconds: float) -> None:
        api.now += seconds

    monkeypatch.setattr(_polling, "time", api)
    monkeypatch.setattr(SyncAPIResource, "_sleep", lambda _resource, seconds: setattr(api, "now", api.now + seconds))
    monkeypatch.setattr(AsyncAPIResource, "_sleep", sleep_async)

    with pytest.raises(TimeoutError) as raised:
        SLIVER[name](api)

    assert raised.value.__cause__ is None, raised.value
    assert len(api.left) >= 3
    assert min(api.left[1:]) >= MIN_POLL_GAP_SECONDS - 1e-9, api.left
    assert api.now == pytest.approx(api.deadline)  # it slept out the time left
