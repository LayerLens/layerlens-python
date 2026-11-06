"""The SDK's tracing hands ``layerlens env run exec`` the id of the trace it uploads (LAY-4430).

``exec`` sets ``LAYERLENS_TRACE_ID_FILE`` for each attempt and links the id it finds there.
An agent instrumented with the SDK needs no code: once its trace is uploaded, the
server-assigned id is written there, unless the file already holds one (the agent's own
choice, or an earlier trace of the attempt).
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional
from pathlib import Path

import pytest

from layerlens.models import CreateTracesResponse
from layerlens.instrument import _upload
from layerlens._trace_id_file import TRACE_ID_FILE_ENV
from layerlens.instrument._collector import TraceCollector
from layerlens.instrument._capture_config import CaptureConfig

SERVER_ID = "6a91c0f2a7b8c9d0e1f2a3b4"
LATER_ID = "6a91c0f2a7b8c9d0e1f2a3b5"


class _Traces:
    def __init__(self, replies: List[Optional[CreateTracesResponse]]) -> None:
        self._replies = list(replies)

    def upload(self, path: str) -> Optional[CreateTracesResponse]:
        return self._replies.pop(0)


class _Client:
    def __init__(self, *replies: Optional[CreateTracesResponse]) -> None:
        self.traces = _Traces(list(replies))


def _upload_one_trace(client: Any) -> None:
    collector = TraceCollector(client, CaptureConfig())
    collector.emit("model.invoke", {"model": "test-model", "latency_ms": 1}, span_id="s00001")
    collector.flush()


@pytest.fixture
def trace_id_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "trace-6a91c0e5f1d2a3b4c5d6e801.txt"
    path.write_bytes(b"")  # as exec leaves it for the agent
    monkeypatch.setenv(TRACE_ID_FILE_ENV, str(path))
    return path


def test_an_uploaded_trace_writes_its_server_id(trace_id_file: Path) -> None:
    _upload_one_trace(_Client(CreateTracesResponse(trace_ids=[SERVER_ID])))
    assert trace_id_file.read_text() == SERVER_ID


def test_the_first_trace_uploaded_is_the_one_kept(trace_id_file: Path) -> None:
    client = _Client(CreateTracesResponse(trace_ids=[SERVER_ID]), CreateTracesResponse(trace_ids=[LATER_ID]))
    _upload_one_trace(client)
    _upload_one_trace(client)
    assert trace_id_file.read_text() == SERVER_ID


def test_an_id_the_agent_wrote_itself_is_kept(trace_id_file: Path) -> None:
    trace_id_file.write_text("the-agents-choice\n")
    _upload_one_trace(_Client(CreateTracesResponse(trace_ids=[SERVER_ID])))
    assert trace_id_file.read_text() == "the-agents-choice\n"


def test_a_rejected_upload_writes_nothing(trace_id_file: Path) -> None:
    _upload_one_trace(_Client(None))
    assert trace_id_file.read_bytes() == b""


def test_nothing_is_written_when_the_variable_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TRACE_ID_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    _upload_one_trace(_Client(CreateTracesResponse(trace_ids=[SERVER_ID])))
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_cannot_be_written_is_logged_and_the_upload_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(TRACE_ID_FILE_ENV, str(tmp_path / "no-such-dir" / "trace.txt"))
    client = _Client(CreateTracesResponse(trace_ids=[SERVER_ID]))
    channel = _upload._get_channel(client)
    losses_before = _upload.get_upload_loss_stats()
    with caplog.at_level(logging.WARNING, logger="layerlens"):
        _upload_one_trace(client)
    assert any(TRACE_ID_FILE_ENV in r.getMessage() for r in caplog.records)
    assert channel._error_count == 0 and _upload.get_upload_loss_stats() == losses_before


def test_the_background_uploader_writes_it_too(trace_id_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_upload, "_sync_mode", False)
    client = _Client(CreateTracesResponse(trace_ids=[SERVER_ID]))
    _upload_one_trace(client)
    _upload._get_channel(client).shutdown(timeout=5.0)
    assert trace_id_file.read_text() == SERVER_ID


def test_an_id_the_agent_wrote_in_utf16_is_kept_without_a_warning(
    trace_id_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # exec reads a UTF-16 file with a byte order mark; the SDK must see it as taken, not fail to read it.
    written = b"\xff\xfe" + "the-agents-choice".encode("utf-16-le")
    trace_id_file.write_bytes(written)
    with caplog.at_level(logging.WARNING, logger="layerlens"):
        _upload_one_trace(_Client(CreateTracesResponse(trace_ids=[SERVER_ID])))
    assert trace_id_file.read_bytes() == written
    assert not [r for r in caplog.records if TRACE_ID_FILE_ENV in r.getMessage()]


def test_the_file_is_scanned_past_its_first_chunk(trace_id_file: Path) -> None:
    # Read a chunk at a time, never whole: an id after a long run of whitespace is still the agent's,
    # and a file of nothing but whitespace is still empty.
    trace_id_file.write_bytes(b" " * 100_000 + b"the-agents-choice")
    _upload_one_trace(_Client(CreateTracesResponse(trace_ids=[SERVER_ID])))
    assert trace_id_file.read_bytes().endswith(b"the-agents-choice")

    trace_id_file.write_bytes(b" \n" * 50_000)
    _upload_one_trace(_Client(CreateTracesResponse(trace_ids=[SERVER_ID])))
    assert trace_id_file.read_text() == SERVER_ID
