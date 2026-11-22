"""``@trace(AsyncLayerLens())`` and ``trace_context`` with an async client never uploaded.

The upload worker is a thread, and it called ``client.traces.upload(path)``: on an
``AsyncLayerLens`` that is a coroutine nobody awaited, so nothing was sent, a
``coroutine … was never awaited`` warning was printed, and the trace was counted ``rejected``.
The instrumentation docs show ``@trace(AsyncLayerLens())`` and type ``client`` as
``LayerLens | AsyncLayerLens``. The worker now uploads an async client's traces through a
synchronous client with the same settings: the async client's connections belong to its
event loop, which is busy or, once ``asyncio.run`` has returned, closed.

These tests run a real client against a local HTTP server that answers as the API does.
"""

from __future__ import annotations

import json
import asyncio
import threading
from typing import Any, Dict, List, Tuple, Iterator, Optional
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import pytest

from layerlens import AsyncLayerLens
from layerlens.instrument import emit, flush, trace, _upload, trace_context, get_upload_loss_stats

ORG = "6a0000000000000000000001"
PROJECT = "6a0000000000000000000002"
SERVER_TRACE_ID = "6a00000000000000000000aa"


@pytest.fixture(autouse=True)
def _fresh_loss_stats() -> Iterator[None]:
    _upload.reset_upload_loss_stats()
    yield
    _upload.reset_upload_loss_stats()


@contextmanager
def _api() -> Iterator[Tuple[str, List[Dict[str, Any]]]]:
    """A local API: GET /organizations, the presign, the PUT of the file, the create."""
    requests: List[Dict[str, Any]] = []
    port: List[int] = []  # known once the server is bound

    class _Handler(BaseHTTPRequestHandler):
        def _answer(self, body: Optional[Dict[str, Any]]) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            sent = self.rfile.read(length) if length else b""
            requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": sent})
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            org = {"id": ORG, "name": "Org", "projects": [{"id": PROJECT, "name": "P"}]}
            self._answer({"status": "success", "data": org})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.endswith("/traces/upload"):
                url = f"http://127.0.0.1:{port[0]}/bucket/trace.json"
                self._answer({"status": "success", "data": {"url": url}})
            else:
                self._answer({"status": "success", "data": {"trace_ids": [SERVER_TRACE_ID]}})

        def do_PUT(self) -> None:  # noqa: N802
            self._answer(None)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port.append(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port[0]}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _async_client(base: str, **kwargs: Any) -> AsyncLayerLens:
    # The presigned URL points at the local server, which the SSRF guard allows only when trusted.
    return AsyncLayerLens(
        api_key="test-key", base_url=f"{base}/api/v1", max_retries=0, trusted_upload_hosts=["127.0.0.1"], **kwargs
    )


def _sent_trace(requests: List[Dict[str, Any]]) -> Dict[str, Any]:
    puts = [r for r in requests if r["method"] == "PUT"]
    assert len(puts) == 1, [(r["method"], r["path"]) for r in requests]
    payloads = json.loads(puts[0]["body"])
    assert len(payloads) == 1
    return payloads[0]


def test_a_trace_of_an_async_client_is_uploaded_after_its_event_loop_closed(recwarn: Any) -> None:
    with _api() as (base, requests):
        client = _async_client(base)

        @trace(client, name="answer")
        async def answer(question: str) -> str:
            await asyncio.sleep(0)
            return question.upper()

        # The documented program: asyncio.run returns, and closes its loop, before the upload.
        assert asyncio.run(answer("hi")) == "HI"
        assert flush(timeout=10) is True

        assert get_upload_loss_stats() == {}
        sent = _sent_trace(requests)
        assert [e["event_type"] for e in sent["events"]][:2] == ["agent.input", "agent.output"]
        api_calls = [(r["method"], r["path"]) for r in requests if r["method"] != "PUT"]
        assert api_calls == [
            ("GET", "/api/v1/organizations"),
            ("POST", f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces/upload"),
            ("POST", f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces"),
        ]
        assert all(r["headers"].get("x-api-key") == "test-key" for r in requests if r["method"] != "PUT")
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


def test_trace_context_with_an_async_client_uploads_into_the_clients_project() -> None:
    with _api() as (base, requests):
        client = _async_client(base)
        client.organization_id = ORG
        client.project_id = PROJECT

        async def main() -> str:
            with trace_context(client, tags=["async"]) as collector:
                await asyncio.sleep(0)
                emit("tool.call", {"tool_name": "lookup"})
            return collector.trace_id

        trace_id = asyncio.run(main())
        assert flush(timeout=10) is True

        assert get_upload_loss_stats() == {}
        sent = _sent_trace(requests)
        assert sent["trace_id"] == trace_id and sent["tags"] == ["async"]
        assert "tool.call" in [e["event_type"] for e in sent["events"]]
        # The ids set on the async client are the ones used: no organization read.
        assert [r["path"] for r in requests if r["method"] == "GET"] == []


def test_a_session_async_client_uploads_with_its_bearer_token_under_the_session_prefix() -> None:
    with _api() as (base, requests):
        client = _async_client(base, use_bearer_auth=True)
        client.organization_id = ORG
        client.project_id = PROJECT

        @trace(client)
        async def answer() -> str:
            return "ok"

        asyncio.run(answer())
        assert flush(timeout=10) is True

        assert get_upload_loss_stats() == {}
        api_requests = [r for r in requests if r["method"] != "PUT"]
        assert [r["path"] for r in api_requests] == [
            f"/api/v1/dgklmnr/organizations/{ORG}/projects/{PROJECT}/traces/upload",
            f"/api/v1/dgklmnr/organizations/{ORG}/projects/{PROJECT}/traces",
        ]
        for request in api_requests:
            assert request["headers"].get("Authorization") == "Bearer test-key"
            assert "x-api-key" not in request["headers"]


def test_an_upload_that_returns_an_awaitable_is_an_upload_error_not_a_rejection(recwarn: Any) -> None:
    # A client of another kind whose upload is a coroutine: nothing in the worker thread can run
    # it, so the trace is not sent. It is counted as a trace that could not be sent.
    class _Traces:
        async def upload(self, path: str) -> Any:  # noqa: ARG002
            return None

    class _Client:
        traces = _Traces()

    client = _Client()

    @trace(client)
    def answer() -> str:
        return "ok"

    answer()
    assert flush(timeout=10) is True
    assert get_upload_loss_stats() == {"upload_error": 1}
    assert not [w for w in recwarn if "never awaited" in str(w.message)]
