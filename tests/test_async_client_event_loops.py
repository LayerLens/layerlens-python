"""One ``AsyncLayerLens`` used by two successive ``asyncio.run()`` calls (2026-11-17 R1-164).

A module-level async client, used again by a second ``asyncio.run()``, raised ``RuntimeError: Event
loop is closed``: httpx kept the first run's keep-alive connection in its pool, and that
connection belongs to the first, closed, event loop. A request on another loop than the last one,
when that one is closed, now starts from an empty pool.
"""

from __future__ import annotations

import json
import asyncio
import threading
from typing import Any, List, Iterator
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import pytest
import httpcore

from layerlens import AsyncLayerLens, AsyncPublicClient

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"


class _KeepAliveAPI:
    """An HTTP/1.1 API that keeps connections open, so the client pools them; counts connections."""

    def __init__(self) -> None:
        self.connections: List[Any] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                api.connections.append(self.client_address)

            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                payload = json.dumps({"status": "success", "data": {"integrations": [], "count": 0, "total_count": 0}})
                body = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api() -> Iterator[_KeepAliveAPI]:
    server = _KeepAliveAPI()
    yield server
    server.close()


def test_one_async_client_serves_two_asyncio_runs(api: _KeepAliveAPI) -> None:
    client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT

    for _ in range(3):
        asyncio.run(client.integrations.get_many())

    # Each run opened its own connection: the closed loop's one was dropped, not reused.
    assert len(api.connections) == 3


def test_one_run_still_reuses_its_pooled_connection(api: _KeepAliveAPI) -> None:
    client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT

    async def main() -> None:
        for _ in range(3):
            await client.integrations.get_many()

    asyncio.run(main())
    assert len(api.connections) == 1


def test_the_public_async_client_too(api: _KeepAliveAPI) -> None:
    client = AsyncPublicClient(api_key="k", base_url=api.base_url, max_retries=0)
    for _ in range(2):
        try:
            asyncio.run(client.models.get())
        except Exception as err:  # noqa: BLE001 - the fake answers an integrations page; only the loop matters
            assert "Event loop is closed" not in str(err), err


def test_a_later_run_can_close_the_client(api: _KeepAliveAPI) -> None:
    # The cleanup a module-level client needs: aclose() in its own asyncio.run() raised
    # "Event loop is closed" closing the earlier run's pooled connection.
    client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    asyncio.run(client.integrations.get_many())

    asyncio.run(client.aclose())

    assert client.is_closed


@pytest.fixture
def httpcore_before_1_0_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpcore before 1.0.3 (httpx 0.23-0.25 always pull one): aclose() closes each pooled
    connection before it empties the pool, so a close that fails leaves them pooled."""

    async def aclose(self: Any) -> None:
        connections = getattr(self, "_connections", None)
        if connections is None:
            connections = self._pool
        for connection in list(connections):
            await connection.aclose()
        connections.clear()

    monkeypatch.setattr(httpcore.AsyncConnectionPool, "aclose", aclose)


def test_a_dead_loops_connections_are_dropped_with_an_older_httpcore(
    api: _KeepAliveAPI, httpcore_before_1_0_3: None
) -> None:
    # Three calls at once leave three pooled connections: closing them on the dead loop failed
    # on the first, and the other two, still idle, were reused by the next run.
    client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT

    async def three_at_once() -> None:
        await asyncio.gather(*(client.integrations.get_many() for _ in range(3)))

    for _ in range(3):
        asyncio.run(three_at_once())
    asyncio.run(client.aclose())

    assert len(api.connections) >= 3
