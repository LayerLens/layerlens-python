"""A trace the upload worker has taken off the queue is accounted for at exit.

The worker took a trace off the queue, checked the circuit breaker and wrote the trace's file
before it marked the trace as being sent. An exit that counted in that window found the trace
neither queued nor being sent: it was not counted ``exit_timeout``, the exit said nothing, and the
trace died with the process. With ``LAYERLENS_EXIT_TIMEOUT=0`` that was every run of a program that
records one trace and exits: 6 of 6 exited with nothing sent and no warning (docs E2E 2026-11-10,
L1-121). The take now marks the trace in the same step, under the queue's lock.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Callable, Iterator
from unittest.mock import Mock

import pytest

from layerlens.instrument import _upload
from layerlens.instrument._upload import _get_channel

_LOGGER = "layerlens.instrument._upload"


@pytest.fixture(autouse=True)
def _isolate_channel_registry() -> Iterator[None]:
    before = list(_upload._channels)
    yield
    added = [ch for ch in _upload._channels if ch not in before]
    for ch in added:
        ch.shutdown(timeout=5.0)
    with _upload._registry_lock:
        _upload._channels[:] = before


@pytest.fixture
def losses() -> Iterator[List[Any]]:
    seen: List[Any] = []
    _upload.reset_upload_loss_stats()
    _upload.set_upload_loss_callback(lambda reason, payload: seen.append((reason, payload.get("trace_id"))))
    yield seen
    _upload.set_upload_loss_callback(None)
    _upload.reset_upload_loss_stats()


def _on_the_worker(step: Callable[..., Any], reached: threading.Event, release: threading.Event) -> Callable[..., Any]:
    """``step``, held on the upload worker until ``release``; ``reached`` says it got there."""

    def held(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name == "layerlens-upload":
            reached.set()
            release.wait(10.0)
        return step(*args, **kwargs)

    return held


@pytest.mark.parametrize("stage", ["the circuit breaker check", "writing the trace file"])
def test_the_exit_counts_and_reports_a_trace_the_worker_has_taken(
    stage: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, losses: List[Any]
) -> None:
    monkeypatch.setattr(_upload, "_sync_mode", False)
    monkeypatch.setenv("LAYERLENS_EXIT_TIMEOUT", "0")
    client = Mock()
    client.traces.upload.return_value = Mock(trace_ids=["accepted"])
    ch = _get_channel(client)
    reached, release = threading.Event(), threading.Event()
    if stage == "the circuit breaker check":
        monkeypatch.setattr(ch, "_allow", _on_the_worker(ch._allow, reached, release))
    else:
        monkeypatch.setattr(_upload, "_write_trace_file", _on_the_worker(_upload._write_trace_file, reached, release))
    try:
        assert ch.enqueue(client, {"trace_id": "taken"}) is True
        assert reached.wait(5.0), "the worker never took the trace"
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _upload._flush_at_exit()
    finally:
        release.set()

    assert losses == [("exit_timeout", "taken")]
    assert _upload.get_upload_loss_stats() == {"exit_timeout": 1}
    assert any(
        "1 trace(s) not sent: the process exited before their upload ended (waited 0s; "
        "set LAYERLENS_EXIT_TIMEOUT to wait longer" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_flush_does_not_report_done_while_the_worker_holds_a_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_upload, "_sync_mode", False)
    client = Mock()
    client.traces.upload.return_value = Mock(trace_ids=["accepted"])
    ch = _get_channel(client)
    reached, release = threading.Event(), threading.Event()
    monkeypatch.setattr(_upload, "_write_trace_file", _on_the_worker(_upload._write_trace_file, reached, release))
    try:
        assert ch.enqueue(client, {"trace_id": "taken"}) is True
        assert reached.wait(5.0)
        assert ch._unsent() == 1
    finally:
        release.set()
    assert _upload.flush(timeout=5.0) is True
    assert ch._unsent() == 0
    assert client.traces.upload.call_count == 1
