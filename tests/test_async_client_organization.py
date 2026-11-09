"""``AsyncLayerLens`` read its organization with a blocking HTTP call on the event loop.

Since BUG-03 the organization and project are read on first use; on the async client that read
was a synchronous ``httpx.Client`` request made while building a URL inside a coroutine, so it
blocked the event loop and skipped the client's retries (CodeRabbit on #99). Async resources now
await an async read through the client's own request path, once, under a lock; the ids are then
cached, and the synchronous properties read the cache.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import httpx
import pytest

from layerlens import AsyncLayerLens
from layerlens._exceptions import NotFoundError, APIStatusError, APIConnectionError, AuthenticationError

ORG_ID, PROJECT_ID = "6a0000000000000000000001", "6a0000000000000000000002"
ORG = {"id": ORG_ID, "name": "Org", "projects": [{"id": PROJECT_ID, "name": "P"}]}
SCOPE = f"/organizations/{ORG_ID}/projects/{PROJECT_ID}"
ENV_ID = "6adcba8583c75b47f74ebd08"


class _API:
    """Answers GET /organizations with ``organization_replies`` (the last repeats), anything else 404."""

    def __init__(self, *organization_replies: httpx.Response) -> None:
        self.replies: List[httpx.Response] = list(organization_replies) or [httpx.Response(200, json={"data": ORG})]
        self.organization_reads = 0
        self.paths: List[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/organizations"):
            self.organization_reads += 1
            return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        self.paths.append(request.url.path)
        return httpx.Response(404, json={"status": "error", "error": "not here"})


@pytest.fixture
def no_blocking_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails any synchronous read of the organization, the one that blocks the event loop."""

    def blocking(self: Any) -> Any:  # noqa: ARG001
        raise AssertionError("the organization was read with a blocking call on the event loop")

    monkeypatch.setattr(AsyncLayerLens, "_get_organization", blocking)


def _client(api: _API) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client._transport = httpx.MockTransport(api)
    return client


async def _call(coro: Any) -> None:
    try:
        await coro
    except NotFoundError:  # the fake API answers 404 to everything but the organization
        pass


def test_async_resources_read_the_organization_without_blocking(no_blocking_read: None) -> None:
    api = _API()
    client = _client(api)

    async def main() -> None:
        await _call(client.traces.get_many())
        await _call(client.judges.get_many())
        await _call(client.scorers.get_many())
        await _call(client.models.get())
        await _call(client.integrations.get_many())
        await _call(client.environments.jobs.get(ENV_ID, "6a8f2c1d9e0b4a7f3c5d6e99"))
        await _call(client.environments.runs.get(ENV_ID, "6a91c0e5f1d2a3b4c5d6e7f8"))

    asyncio.run(main())
    assert api.organization_reads == 1
    # Integrations are the organization's; every other one of these is the project's.
    assert len(api.paths) == 7 and all(f"/organizations/{ORG_ID}/" in p for p in api.paths), api.paths
    assert sum(SCOPE in p for p in api.paths) == 6, api.paths
    # Read once, the ids are the client's: the properties read them without a request.
    assert (client.organization_id, client.project_id) == (ORG_ID, PROJECT_ID)


def test_concurrent_first_calls_read_the_organization_once(no_blocking_read: None) -> None:
    api = _API()
    client = _client(api)

    async def main() -> None:
        await asyncio.gather(*(_call(client.traces.get_many()) for _ in range(5)))

    asyncio.run(main())
    assert api.organization_reads == 1 and len(api.paths) == 5


def test_a_failed_read_is_retried_on_the_next_call_even_on_another_event_loop(no_blocking_read: None) -> None:
    api = _API(httpx.Response(503, json={"status": "error", "error": "down"}), httpx.Response(200, json={"data": ORG}))
    client = _client(api)
    with pytest.raises(APIStatusError):
        asyncio.run(client.traces.get_many())
    asyncio.run(_call(client.traces.get_many()))  # a new loop: the lock is not the first loop's
    assert api.organization_reads == 2 and api.paths and SCOPE in api.paths[-1]


@pytest.mark.parametrize(
    ("reply", "raised"),
    [
        (httpx.Response(401, json={"status": "error", "error": "Unknown API key"}), AuthenticationError),
        (httpx.ConnectError("refused"), APIConnectionError),
    ],
)
def test_the_read_raises_what_the_api_said(no_blocking_read: None, reply: Any, raised: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = AsyncLayerLens(api_key="rotated-key", base_url="https://api.test/api/v1", max_retries=0)
    client._transport = httpx.MockTransport(handler)
    with pytest.raises(raised):
        asyncio.run(client.traces.get_many())


def test_a_blocking_read_in_another_thread_does_not_stall_the_event_loop(no_blocking_read: None) -> None:
    # CodeRabbit on #99: the async read took the lock the synchronous read holds across its HTTP
    # call, so a sync read in another thread (asyncio.to_thread, say) stalled the event loop. Here
    # the "sync read" holds that lock until the async call has finished, or gives up after 5 s:
    # the call finishing while the lock is still held is the proof, with no timing threshold.
    import threading

    api = _API()
    client = _client(api)
    holding, call_done = threading.Event(), threading.Event()
    gave_up: List[bool] = []

    def sync_read_in_progress() -> None:
        with client._scope_lock:
            holding.set()
            gave_up.append(not call_done.wait(timeout=5))

    reader = threading.Thread(target=sync_read_in_progress)
    reader.start()
    try:
        holding.wait(timeout=5)

        async def main() -> None:
            await _call(client.traces.get_many())
            call_done.set()

        asyncio.run(main())
    finally:
        call_done.set()
        reader.join(timeout=10)
    assert gave_up == [False], "the async call waited for the synchronous read's lock"
    assert api.paths and SCOPE in api.paths[-1]
