"""m11 and m14: ``traces.get_many`` with a trace it can't read, and ``traces.upload``'s PUT to storage.

m11: the docs say ``get_many`` returns None when the API's answer can't be read, or raises
``LayerLensError`` with ``strict=True``. A page whose trace lacked fields raised pydantic's
``ValidationError`` either way (the item was built outside the guard), and an answer that is a
JSON array raised ``APIResponseValidationError`` without ``strict``.

m14: an S3 "503 Slow Down" on the upload's PUT was not retried, and the customer got httpx's
``HTTPStatusError``, whose text quotes the whole presigned URL, credential and signature
included; httpx's own INFO log line quoted it too. The PUT is now retried as the client retries
its calls, and neither an error nor a log line carries the URL's query.
"""

from __future__ import annotations

import time
import socket
import asyncio
import logging
import threading
import traceback
from types import SimpleNamespace
from typing import Any, List, Tuple, Iterator, Optional
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens._presigned import put_presigned, put_presigned_async, _hide_signed_urls_in_httpx_logs
from layerlens._exceptions import (
    APIStatusError,
    LayerLensError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    PermissionDeniedError,
    APIResponseValidationError,
)

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
TRACES = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces"
SIGNED = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAEXAMPLE%2F20261118%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Signature=0123456789abcdef"
GOOD = {
    "id": "t1",
    "organization_id": ORG,
    "project_id": PROJECT,
    "created_at": "2026-11-18T00:00:00Z",
    "filename": "a.json",
}


def _client(handler: Any) -> LayerLens:
    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(handler)
    return client


def _async_client(handler: Any) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(handler)
    return client


def _answering(body: Any) -> Any:
    return lambda request: httpx.Response(200, json=body)


UNREADABLE_PAGES = [
    {"status": "success", "data": {"traces": [{"id": "t-without-fields"}], "count": 1, "total_count": 1}},
    {"status": "success", "data": {"traces": [GOOD, {"id": "t2"}], "count": 2, "total_count": 2}},
    [GOOD],  # an array, not an object
]


@pytest.mark.parametrize("body", UNREADABLE_PAGES)
def test_get_many_returns_none_for_a_page_it_cannot_read(body: Any) -> None:
    assert _client(_answering(body)).traces.get_many() is None


@pytest.mark.parametrize("body", UNREADABLE_PAGES)
def test_get_many_strict_raises_layerlens_error(body: Any) -> None:
    with pytest.raises(LayerLensError):
        _client(_answering(body)).traces.get_many(strict=True)


@pytest.mark.parametrize("body", UNREADABLE_PAGES)
def test_the_async_get_many_too(body: Any) -> None:
    client = _async_client(_answering(body))
    assert asyncio.run(client.traces.get_many()) is None
    with pytest.raises(LayerLensError):
        asyncio.run(client.traces.get_many(strict=True))


def test_get_returns_none_for_an_array_and_strict_raises() -> None:
    client = _client(_answering([GOOD]))
    assert client.traces.get("t1") is None
    with pytest.raises(APIResponseValidationError):
        client.traces.get("t1", strict=True)


def test_a_readable_page_still_reads() -> None:
    body = {"status": "success", "data": {"traces": [GOOD], "count": 1, "total_count": 1}}
    page = _client(_answering(body)).traces.get_many()
    assert page is not None and [t.id for t in page.traces] == ["t1"]


class _Storage:
    """A stand-in S3: answers each PUT with the next of ``replies`` (status, "drop" or "slow"); the last repeats."""

    def __init__(self, replies: List[Any]) -> None:
        self.replies = replies
        self.puts: List[Tuple[str, bytes]] = []
        storage = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self) -> None:  # noqa: N802 - the http.server hook
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                storage.puts.append((self.path, body))
                reply = storage.replies[min(len(storage.puts) - 1, len(storage.replies) - 1)]
                if reply == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return
                if reply == "slow":  # answers after the client's timeout
                    time.sleep(1)
                    reply = 200
                payload = b"<Error><Code>SlowDown</Code></Error>" if reply >= 400 else b""
                self.send_response(reply)
                self.send_header("Content-Length", str(len(payload)))
                if reply == 503:
                    self.send_header("Retry-After", "0")
                if 300 <= reply < 400:  # S3's redirect to the bucket's own endpoint, signed query and all
                    self.send_header("Location", storage.url)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/bucket/trace.json?{SIGNED}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _upload_api(storage: _Storage) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{TRACES}/upload":
            return httpx.Response(200, json={"status": "success", "data": {"url": storage.url}})
        if request.url.path == TRACES:
            return httpx.Response(200, json={"status": "success", "data": ["t-new"]})
        return httpx.Response(404, json={"status": "error", "error": "not here"})

    return handler


@pytest.fixture
def trace_file(tmp_path: Path) -> str:
    path = tmp_path / "trace.json"
    path.write_text('[{"input": "hi", "output": "hello"}]')
    return str(path)


def _storage(replies: List[Any]) -> Iterator[_Storage]:
    storage = _Storage(replies)
    try:
        yield storage
    finally:
        storage.close()


@pytest.fixture
def slow_down_once() -> Iterator[_Storage]:
    yield from _storage([503, 200])


@pytest.fixture
def dropped_once() -> Iterator[_Storage]:
    yield from _storage(["drop", 200])


@pytest.fixture
def always_slow_down() -> Iterator[_Storage]:
    yield from _storage([503])


@pytest.fixture
def signature_refused() -> Iterator[_Storage]:
    yield from _storage([403])


def _uploader(storage: _Storage, max_retries: int = 2) -> LayerLens:
    client = _client(_upload_api(storage))
    client._max_retries = max_retries
    client.trusted_upload_hosts = ["127.0.0.1"]
    return client


@pytest.mark.parametrize("fixture", ["slow_down_once", "dropped_once"])
def test_the_put_is_retried_after_a_503_or_a_dropped_connection(
    fixture: str, trace_file: str, request: pytest.FixtureRequest
) -> None:
    storage: _Storage = request.getfixturevalue(fixture)
    created = _uploader(storage).traces.upload(trace_file)

    assert created is not None and created.trace_ids == ["t-new"]
    assert len(storage.puts) == 2


def test_the_async_put_is_retried_too(slow_down_once: _Storage, trace_file: str) -> None:
    client = _async_client(_upload_api(slow_down_once))
    client._max_retries = 2
    client.trusted_upload_hosts = ["127.0.0.1"]
    created = asyncio.run(client.traces.upload(trace_file))
    assert created is not None and len(slow_down_once.puts) == 2


@pytest.mark.parametrize(
    ("fixture", "raised"),
    [("always_slow_down", InternalServerError), ("signature_refused", PermissionDeniedError)],
)
def test_a_refused_put_raises_without_the_signed_query(
    fixture: str, raised: type, trace_file: str, request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> None:
    storage: _Storage = request.getfixturevalue(fixture)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="httpx")  # a library imported elsewhere may have raised it
    with pytest.raises(raised) as caught:
        _uploader(storage).traces.upload(trace_file)

    err = caught.value
    shown = " ".join([str(err), repr(err), str(err.request.url), str(err.response.url), str(err.body), caplog.text])
    assert "X-Amz" not in shown and "AKIAEXAMPLE" not in shown and "0123456789abcdef" not in shown
    assert "trace file could not be stored" in str(err)
    assert len(storage.puts) == (3 if raised is InternalServerError else 1)
    # httpx logged each PUT, without the query
    assert "PUT http://127.0.0.1" in caplog.text


class _FormatsFirst(logging.Filter):
    """As litellm's SecretRedactionFilter does on the "httpx" logger: formats the record into msg."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = record.getMessage(), ()
        return True


def test_the_log_line_holds_no_signed_query_after_a_filter_that_formats_it(
    signature_refused: _Storage, trace_file: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    _hide_signed_urls_in_httpx_logs()  # the SDK's filter is on the logger; the formatting one goes first
    httpx_log = logging.getLogger("httpx")
    formats_first = _FormatsFirst()
    httpx_log.filters.insert(0, formats_first)
    try:
        with pytest.raises(PermissionDeniedError):
            _uploader(signature_refused).traces.upload(trace_file)
    finally:
        httpx_log.removeFilter(formats_first)
    assert "PUT http://127.0.0.1" in caplog.text
    assert "X-Amz" not in caplog.text and "0123456789abcdef" not in caplog.text


def test_httpx_0_23s_log_line_holds_no_signed_query(caplog: pytest.LogCaptureFixture) -> None:
    # httpx 0.23 logs each request on its "httpx._client" logger, at DEBUG, and a logger's filter
    # does not see the records of its children: a filter on "httpx" alone let the query through.
    caplog.set_level(logging.DEBUG, logger="httpx._client")
    _hide_signed_urls_in_httpx_logs()
    url = httpx.URL(f"http://127.0.0.1:9/bucket/trace.json?{SIGNED}")
    logging.getLogger("httpx._client").debug('HTTP Request: %s %s "%s"', "PUT", url, "HTTP/1.1 403 Forbidden")
    assert "PUT http://127.0.0.1:9/bucket/trace.json" in caplog.text
    assert "X-Amz" not in caplog.text and "0123456789abcdef" not in caplog.text


def test_a_redirect_is_not_stored_and_raises_without_the_signed_query(trace_file: str) -> None:
    # httpx does not follow a redirect, so nothing was stored, yet the upload went on as if it had.
    for storage in _storage([307]):
        with pytest.raises(APIStatusError) as caught:
            _uploader(storage).traces.upload(trace_file)
        err = caught.value
        assert err.status_code == 307 and len(storage.puts) == 1  # not sent again
        assert "trace file could not be stored: the storage service answered 307" in str(err)
        assert err.response.headers["location"] == storage.url.split("?")[0]
        shown = " ".join([str(err), repr(err), str(err.request.url), str(err.response.headers)])
        assert "X-Amz" not in shown and "0123456789abcdef" not in shown


def test_the_async_upload_does_not_take_a_redirect_for_stored_either(trace_file: str) -> None:
    for storage in _storage([307]):
        client = _async_client(_upload_api(storage))
        client.trusted_upload_hosts = ["127.0.0.1"]
        with pytest.raises(APIStatusError) as caught:
            asyncio.run(client.traces.upload(trace_file))
        assert caught.value.status_code == 307 and len(storage.puts) == 1


def test_a_connection_that_keeps_dropping_raises_without_the_signed_query(trace_file: str) -> None:
    for storage in _storage(["drop"]):
        with pytest.raises(APIConnectionError) as caught:
            _uploader(storage, max_retries=1).traces.upload(trace_file)
        assert "X-Amz" not in str(caught.value) + str(caught.value.request.url)
        assert len(storage.puts) == 2


def _chain(err: BaseException) -> List[BaseException]:
    """``err`` and every error under it, through ``__cause__`` and ``__context__``."""
    found: List[BaseException] = []
    todo: List[Optional[BaseException]] = [err]
    while todo:
        current = todo.pop()
        if current is not None and all(current is not seen for seen in found):
            found.append(current)
            todo += [current.__cause__, current.__context__]
    return found


def _shown(err: BaseException) -> str:
    """What an error shows: its traceback, and each error of its chain with its request's URL."""
    texts = ["".join(traceback.format_exception(type(err), err, err.__traceback__))]
    for each in _chain(err):
        request = vars(each).get("request") or vars(each).get("_request")  # the SDK's, or httpx's
        texts += [str(each), repr(each), str(request.url) if request is not None else ""]
    return " ".join(texts)


def _refused_url() -> str:
    """A signed URL on a local port that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/bucket/trace.json?{SIGNED}"


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("failure", ["timeout", "refused", "dropped"])
def test_a_put_that_fails_keeps_no_httpx_error_with_the_signed_query(failure: str, asynchronous: bool) -> None:
    # `raise ... from None` in the except block hid httpx's error from the traceback only: it was
    # still the SDK error's __context__, and its request held the whole signed URL.
    client = SimpleNamespace(_max_retries=0)
    for storage in _storage(["slow" if failure == "timeout" else "drop"]):
        url = _refused_url() if failure == "refused" else storage.url
        with pytest.raises(APIConnectionError) as caught:
            if asynchronous:
                put = put_presigned_async(
                    url, b"[]", content_type="application/json", timeout=0.2, client=client, what="trace file"
                )
                asyncio.run(put)
            else:
                put_presigned(
                    url, b"[]", content_type="application/json", timeout=0.2, client=client, what="trace file"
                )
        err = caught.value
        assert isinstance(err, APITimeoutError) == (failure == "timeout")
        assert err.__cause__ is None and err.__context__ is None
        shown = _shown(err)
        assert "/bucket/trace.json" in shown
        assert "X-Amz" not in shown and "AKIAEXAMPLE" not in shown and "0123456789abcdef" not in shown


def test_a_benchmark_file_upload_is_retried_and_its_errors_hold_no_signed_query(tmp_path: Path) -> None:
    data = tmp_path / "data.jsonl"
    data.write_text('{"input": "q", "truth": "a"}\n')
    storage = _Storage([503, 403])
    try:
        answer = {"status": "success", "data": {"url": storage.url}}
        client = _client(lambda request: httpx.Response(200, json=answer))
        client._max_retries = 2
        client.trusted_upload_hosts = ["127.0.0.1"]
        with pytest.raises(PermissionDeniedError) as caught:
            client.benchmarks._upload_file(str(data), "my-bench", 30.0)
    finally:
        storage.close()
    assert len(storage.puts) == 2  # the 503 was sent again
    assert "X-Amz" not in str(caught.value) + str(caught.value.request.url)
    assert "benchmark file could not be stored" in str(caught.value)
