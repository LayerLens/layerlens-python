"""m2: concurrent first calls on a new client queued behind a failing organization read, one read each.

A failed read was not remembered and held a lock, so each caller that came while it ran made its
own read once the one before had failed: three threads on a stalled API failed at 30, 60 and 90 s,
eight threads on a 503 sent 24 requests. Callers that come while a read is under way now wait for
it and share its outcome, its error included; a call made after a read failed reads again.
"""

from __future__ import annotations

import json
import time
import asyncio
import threading
from typing import Any, List, Tuple, Iterator, Optional
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import pytest

from layerlens import LayerLens, AsyncLayerLens, APITimeoutError, InternalServerError

ORG_ID, PROJECT_ID = "6a0000000000000000000001", "6a0000000000000000000002"
ORG = {"id": ORG_ID, "name": "Org", "projects": [{"id": PROJECT_ID, "name": "P"}]}


class _OrganizationAPI:
    """Answers GET /organizations as ``mode`` says: "stall" (never), "503", or "slow" (after 0.5 s)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.reads = 0
        self.done = threading.Event()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                api.reads += 1
                if api.mode == "stall":
                    api.done.wait(60)
                    return
                if api.mode == "slow" and api.done.wait(0.5):
                    return
                status, body = (
                    (503, {"status": "error", "error": "down"}) if api.mode == "503" else (200, {"data": ORG})
                )
                payload = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.done.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _api(mode: str) -> Iterator[_OrganizationAPI]:
    api = _OrganizationAPI(mode)
    try:
        yield api
    finally:
        api.close()


@pytest.fixture
def stalled() -> Iterator[_OrganizationAPI]:
    yield from _api("stall")


@pytest.fixture
def down() -> Iterator[_OrganizationAPI]:
    yield from _api("503")


@pytest.fixture
def slow() -> Iterator[_OrganizationAPI]:
    yield from _api("slow")


Outcome = Tuple[float, Any]


def _in_threads(client: LayerLens, count: int = 3) -> List[Outcome]:
    """``count`` threads read ``client.organization_id`` at once: when each ended, and how."""
    outcomes: List[Outcome] = []
    start = threading.Barrier(count)
    began = time.monotonic()

    def call() -> None:
        start.wait()
        try:
            result: Any = client.organization_id
        except Exception as err:  # noqa: BLE001 - recorded for the test
            result = err
        outcomes.append((time.monotonic() - began, result))

    threads = [threading.Thread(target=call) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


def test_threads_on_a_stalled_api_fail_together_after_one_read(stalled: _OrganizationAPI) -> None:
    client = LayerLens(api_key="k", base_url=stalled.base_url, timeout=1.0, max_retries=0)
    outcomes = _in_threads(client)

    assert stalled.reads == 1
    assert [type(result) for _, result in outcomes] == [APITimeoutError] * 3
    assert max(at for at, _ in outcomes) < 1.8, outcomes  # before: 1, 2 and 3 s

    with pytest.raises(APITimeoutError):
        client.organization_id  # noqa: B018 - a call after a failed read reads again
    assert stalled.reads == 2


def test_threads_share_a_503_and_its_retries(down: _OrganizationAPI) -> None:
    client = LayerLens(api_key="k", base_url=down.base_url, timeout=5.0, max_retries=2)
    outcomes = _in_threads(client)

    assert down.reads == 3  # one read and its two retries; before: 9
    assert [type(result) for _, result in outcomes] == [InternalServerError] * 3


def test_threads_share_a_slow_read_that_succeeds(slow: _OrganizationAPI) -> None:
    outcomes = _in_threads(LayerLens(api_key="k", base_url=slow.base_url, max_retries=0))

    assert slow.reads == 1
    assert [result for _, result in outcomes] == [ORG_ID] * 3


def _in_tasks(client: AsyncLayerLens, count: int = 3) -> Tuple[float, List[Any]]:
    async def main() -> List[Any]:
        return await asyncio.gather(*(client._organization_scope_async() for _ in range(count)), return_exceptions=True)

    began = time.monotonic()
    results = asyncio.run(main())
    return time.monotonic() - began, results


def test_tasks_on_a_stalled_api_fail_together_after_one_read(stalled: _OrganizationAPI) -> None:
    client = AsyncLayerLens(api_key="k", base_url=stalled.base_url, timeout=1.0, max_retries=0)
    took, results = _in_tasks(client)

    assert stalled.reads == 1
    assert [type(result) for result in results] == [APITimeoutError] * 3
    assert took < 1.8, took  # before: 3 s

    took, results = _in_tasks(client, 1)  # a call after a failed read reads again
    assert stalled.reads == 2 and isinstance(results[0], APITimeoutError)


def test_tasks_share_a_503_and_its_retries(down: _OrganizationAPI) -> None:
    client = AsyncLayerLens(api_key="k", base_url=down.base_url, timeout=5.0, max_retries=2)
    _, results = _in_tasks(client)

    assert down.reads == 3
    assert [type(result) for result in results] == [InternalServerError] * 3


def test_a_cancelled_reader_hands_nothing_and_the_others_read_again(slow: _OrganizationAPI) -> None:
    client = AsyncLayerLens(api_key="k", base_url=slow.base_url, max_retries=0)

    async def main() -> Tuple[Optional[BaseException], Any]:
        reader = asyncio.ensure_future(client._organization_scope_async())
        await asyncio.sleep(0.1)
        waiter = asyncio.ensure_future(client._organization_scope_async())
        await asyncio.sleep(0.1)
        reader.cancel()
        cancelled: Optional[BaseException] = None
        try:
            await reader
        except asyncio.CancelledError as err:
            cancelled = err
        return cancelled, await waiter

    cancelled, ids = asyncio.run(main())
    assert isinstance(cancelled, asyncio.CancelledError)
    assert ids == (ORG_ID, PROJECT_ID)
    assert slow.reads == 2
