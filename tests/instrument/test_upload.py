"""Upload-channel lifecycle tests (LAY-3579 / T5).

Covers the delivery path end to end: per-client channel identity (incl. the
N3 frozen/slotted-client fallback), queue-overflow drop semantics, the
circuit breaker state machine, breaker-open discard of already-dequeued
items, shutdown drain, and sync mode.
"""

from __future__ import annotations

import os
import json
import time
import logging
import tempfile
import threading
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import Mock

import pytest

from layerlens.instrument import _upload
from layerlens._trace_id_file import TRACE_ID_FILE_ENV
from layerlens.instrument._upload import UploadChannel, _get_channel

_LOGGER = "layerlens.instrument._upload"


@pytest.fixture(autouse=True)
def _isolate_channel_registry():
    """Keep channels created here from leaking into other tests."""
    before = list(_upload._channels)
    yield
    added = [ch for ch in _upload._channels if ch not in before]
    for ch in added:
        try:
            ch.shutdown(timeout=1.0)
        except Exception:
            pass
    with _upload._registry_lock:
        _upload._channels[:] = before


@pytest.fixture(autouse=True)
def _no_exit_timeout_variable(monkeypatch: Any) -> None:
    """The exit's budget comes from the code here, not from the shell running the tests."""
    monkeypatch.delenv("LAYERLENS_EXIT_TIMEOUT", raising=False)


def _make_client() -> Mock:
    client = Mock()
    client.traces = Mock()
    client.traces.upload = Mock()
    return client


class _SlottedClient:
    """Simulates an SDK client that rejects attribute injection (N3)."""

    __slots__ = ("traces", "__weakref__")

    def __init__(self) -> None:
        self.traces = Mock()


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Channel identity (incl. N3)
# ---------------------------------------------------------------------------


class TestChannelIdentity:
    def test_same_client_returns_same_channel(self) -> None:
        client = _make_client()
        assert _get_channel(client) is _get_channel(client)

    def test_distinct_clients_get_distinct_channels(self) -> None:
        a, b = _make_client(), _make_client()
        assert _get_channel(a) is not _get_channel(b)

    def test_channel_is_stored_on_the_client(self) -> None:
        client = _make_client()
        ch = _get_channel(client)
        assert getattr(client, _upload._ATTR) is ch

    def test_slotted_client_gets_stable_channel(self) -> None:
        """N3: attribute injection fails on slotted clients — the promised
        side-dict fallback must still return ONE channel per client."""
        client = _SlottedClient()
        assert _get_channel(client) is _get_channel(client)

    def test_slotted_client_does_not_grow_channel_registry(self) -> None:
        """N3: without the fallback every call creates (and pins) a fresh
        channel + daemon worker, growing ``_channels`` without bound."""
        client = _SlottedClient()
        before = len(_upload._channels)
        for _ in range(5):
            _get_channel(client)
        assert len(_upload._channels) - before == 1


# ---------------------------------------------------------------------------
# Queue overflow (documented designed loss, N4)
# ---------------------------------------------------------------------------


class TestQueueOverflow:
    def test_enqueue_drops_when_queue_full(self, monkeypatch: Any, caplog: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        monkeypatch.setattr(ch, "_ensure_worker", lambda: None)  # never drain
        client = _make_client()

        capacity = ch._queue.maxsize
        for i in range(capacity):
            assert ch.enqueue(client, {"trace_id": f"t{i}"}) is True

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert ch.enqueue(client, {"trace_id": "overflow"}) is False
        assert any("queue full" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# A worker that cannot start
# ---------------------------------------------------------------------------


def _upload_threads_fail(monkeypatch: Any, failures: List[bool]) -> None:
    """``Thread.start`` raises for the upload worker while ``failures`` pops True.

    It can: thread exhaustion, or Python 3.12+ refusing new threads at interpreter shutdown,
    which a traced call in a program's own atexit handler reaches.
    """
    real_start = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        if thread.name == "layerlens-upload" and failures and failures.pop(0):
            raise RuntimeError("can't create new thread at interpreter shutdown")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)


class TestAWorkerThatCannotStart:
    def test_enqueue_does_not_raise_and_the_trace_waits_for_the_next_worker(
        self, monkeypatch: Any, caplog: Any
    ) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        _upload_threads_fail(monkeypatch, [True])
        ch = UploadChannel()
        client = _make_client()

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert ch.enqueue(client, {"trace_id": "t0"}) is True, "queued, not dropped"
        assert ch._worker is None
        assert any("could not start the upload worker" in r.message for r in caplog.records)

        assert ch.enqueue(client, {"trace_id": "t1"}) is True  # this start succeeds
        ch.shutdown(timeout=5.0)
        assert client.traces.upload.call_count == 2, "the trace that waited is sent too"

    def test_a_full_queue_still_tries_to_start_a_worker(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        capacity = ch._queue.maxsize
        _upload_threads_fail(monkeypatch, [True] * capacity)
        client = _make_client()
        for i in range(capacity):
            assert ch.enqueue(client, {"trace_id": f"t{i}"}) is True
        assert ch._queue.full() and ch._worker is None

        # Starting works again: the trace that finds the queue full is dropped, but a worker
        # starts for the ones queued.
        assert ch.enqueue(client, {"trace_id": "overflow"}) is False
        ch.shutdown(timeout=5.0)
        assert client.traces.upload.call_count == capacity


# ---------------------------------------------------------------------------
# Circuit breaker state machine
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_opens_after_threshold_consecutive_failures(self) -> None:
        ch = UploadChannel()
        for _ in range(ch._THRESHOLD - 1):
            ch._on_failure()
        assert ch._allow() is True
        ch._on_failure()
        assert ch._circuit_open is True
        assert ch._allow() is False

    def test_half_open_after_cooldown(self) -> None:
        ch = UploadChannel()
        for _ in range(ch._THRESHOLD):
            ch._on_failure()
        assert ch._allow() is False
        ch._opened_at = time.monotonic() - ch._COOLDOWN_S - 1
        assert ch._allow() is True  # half-open: one retry allowed
        assert ch._circuit_open is False

    def test_success_resets_error_count(self) -> None:
        ch = UploadChannel()
        for _ in range(ch._THRESHOLD - 1):
            ch._on_failure()
        ch._on_success()
        assert ch._error_count == 0
        for _ in range(ch._THRESHOLD - 1):
            ch._on_failure()
        assert ch._allow() is True  # consecutive count restarted


# ---------------------------------------------------------------------------
# Breaker-open discard of already-dequeued items (documented designed loss, N4)
# ---------------------------------------------------------------------------


class TestBreakerOpenDiscard:
    def test_open_breaker_discards_dequeued_items(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        client = _make_client()
        for i in range(3):
            ch._queue.put_nowait((client, {"trace_id": f"t{i}"}))

        # Open the breaker, then start the worker: items are dequeued and
        # dropped without an upload attempt.
        ch._circuit_open = True
        ch._opened_at = time.monotonic()
        ch._ensure_worker()
        _wait_until(lambda: ch._queue.empty())

        assert ch._queue.empty()
        assert client.traces.upload.call_count == 0
        ch.shutdown(timeout=2.0)


# ---------------------------------------------------------------------------
# Shutdown drain
# ---------------------------------------------------------------------------


class TestShutdownDrain:
    def test_shutdown_drains_pending_uploads(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        client = _make_client()
        for i in range(5):
            assert ch.enqueue(client, {"trace_id": f"t{i}"}) is True

        ch.shutdown(timeout=5.0)

        assert client.traces.upload.call_count == 5
        assert ch._worker is None

    def test_shutdown_without_worker_is_noop(self) -> None:
        ch = UploadChannel()
        ch.shutdown(timeout=1.0)  # never started — must not raise
        assert ch._worker is None

    def test_shutdown_uploads_covers_registered_channels(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        ch = _get_channel(client)
        assert ch.enqueue(client, {"trace_id": "t"}) is True

        _upload.shutdown_uploads(timeout=5.0)

        assert client.traces.upload.call_count == 1


def _held_uploads(client: Mock, release: threading.Event, started: threading.Event) -> None:
    """Each upload waits for ``release``; ``started`` is set once one has begun."""

    def upload(path: str) -> Mock:  # noqa: ARG001
        started.set()
        release.wait(10.0)
        return Mock(trace_ids=["accepted"])

    client.traces.upload.side_effect = upload


class TestShutdownWaitsOnlyForWhatIsQueued:
    """LAY-4454: the stop was a marker on the queue, dropped when the queue was full. The worker
    then sent what was queued and waited for more, and the exit waited out its whole timeout,
    one channel after another."""

    def test_a_full_queue_is_sent_and_shutdown_returns_once_it_is(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        release, started = threading.Event(), threading.Event()
        _held_uploads(client, release, started)
        try:
            assert ch.enqueue(client, {"trace_id": "first"}) is True
            assert started.wait(5.0)  # the worker holds it, so the queue can fill up behind it
            for i in range(ch._queue.maxsize):
                assert ch.enqueue(client, {"trace_id": f"t{i}"}) is True
            assert ch._queue.full()
            threading.Timer(0.3, release.set).start()

            began = time.monotonic()
            ch.shutdown(timeout=5.0)
            waited = time.monotonic() - began
        finally:
            release.set()

        assert client.traces.upload.call_count == ch._queue.maxsize + 1
        assert waited < 2.5, f"shutdown waited {waited:.1f}s, though the queue was sent after 0.3s"
        assert ch._worker is None

    def test_shutdown_uploads_waits_out_one_timeout_for_every_channel(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        release = threading.Event()
        try:
            for _ in range(3):
                client, started = _make_client(), threading.Event()
                _held_uploads(client, release, started)
                assert _get_channel(client).enqueue(client, {"trace_id": "slow"}) is True
                assert started.wait(5.0)

            began = time.monotonic()
            _upload.shutdown_uploads(timeout=0.5)
            waited = time.monotonic() - began
        finally:
            release.set()

        # One after another, three stuck channels took 3 x 0.5s.
        assert 0.4 < waited < 1.0, f"shutdown_uploads waited {waited:.2f}s for 3 channels with a 0.5s timeout"

    def test_a_worker_still_uploading_after_the_timeout_is_not_doubled(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        release, started = threading.Event(), threading.Event()
        _held_uploads(client, release, started)
        try:
            assert ch.enqueue(client, {"trace_id": "slow"}) is True
            assert started.wait(5.0)
            worker = ch._worker
            ch.shutdown(timeout=0.1)  # gives up on it; it is still in its upload
            assert worker is not None and worker.is_alive()

            assert ch.enqueue(client, {"trace_id": "next"}) is True
            assert ch._worker is worker, "a second worker was started on the same queue"
        finally:
            release.set()
        _wait_until(lambda: client.traces.upload.call_count == 2)
        assert client.traces.upload.call_count == 2
        worker.join(5.0)
        assert not worker.is_alive() and ch._worker is None  # it sent "next", then ended

    def test_after_a_shutdown_a_new_trace_starts_a_worker_that_keeps_running(self, monkeypatch: Any) -> None:
        # Tests and the live harness call shutdown_uploads as a flush, then trace on.
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        assert ch.enqueue(client, {"trace_id": "before"}) is True
        ch.shutdown(timeout=5.0)
        assert ch._worker is None

        assert ch.enqueue(client, {"trace_id": "after"}) is True
        _wait_until(lambda: client.traces.upload.call_count == 2)
        assert client.traces.upload.call_count == 2
        time.sleep(0.1)
        assert ch._worker is not None and ch._worker.is_alive(), "the new worker took the old one's stop"
        ch.shutdown(timeout=5.0)

    def test_a_trace_queued_as_the_worker_ends_is_still_sent(self, monkeypatch: Any) -> None:
        # The worker finds the queue empty and ends; a trace queued at that very moment must
        # start a new worker rather than wait in the queue for one that never comes.
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        monkeypatch.setattr(ch, "_allow", lambda: True)  # the breaker's lock is not under test
        assert ch.enqueue(client, {"trace_id": "first"}) is True
        _wait_until(lambda: client.traces.upload.call_count == 1)

        empty = ch._queue.empty
        late = threading.Thread(target=ch.enqueue, args=(client, {"trace_id": "late"}))

        def empty_as_a_trace_arrives() -> bool:
            answer = empty()
            if answer and ch._stop_when_empty and not late.is_alive() and late.ident is None:
                late.start()  # its put lands now; its _ensure_worker waits for the lock we hold
                _wait_until(lambda: ch._queue.qsize() == 1)
            return answer  # True, though the late trace is now queued

        monkeypatch.setattr(ch._queue, "empty", empty_as_a_trace_arrives)
        ch.shutdown(timeout=5.0)
        if late.ident is not None:
            late.join(5.0)
        _wait_until(lambda: client.traces.upload.call_count == 2)
        assert client.traces.upload.call_count == 2, "the late trace was stranded in the queue"
        ch.shutdown(timeout=5.0)


@pytest.fixture
def losses() -> Any:
    """The loss callback's calls as (reason, trace_id), with the counters reset around the test."""
    seen: List[Any] = []
    _upload.reset_upload_loss_stats()
    _upload.set_upload_loss_callback(lambda reason, payload: seen.append((reason, payload.get("trace_id"))))
    yield seen
    _upload.set_upload_loss_callback(None)
    _upload.reset_upload_loss_stats()


class TestTheExitAccountsForWhatItLeaves:
    """LAY-4454: a trace the exit could not wait for died with the process without a word."""

    def test_traces_left_unsent_at_exit_are_counted_and_reported(
        self, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        monkeypatch.setattr(_upload, "_EXIT_TIMEOUT_S", 0.2)
        client = _make_client()
        ch = _get_channel(client)
        release, started = threading.Event(), threading.Event()
        _held_uploads(client, release, started)
        try:
            assert ch.enqueue(client, {"trace_id": "in-flight"}) is True
            assert started.wait(5.0)
            for i in range(3):
                assert ch.enqueue(client, {"trace_id": f"queued-{i}"}) is True
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                _upload._flush_at_exit()
        finally:
            release.set()

        assert sorted(losses) == [("exit_timeout", t) for t in ("in-flight", "queued-0", "queued-1", "queued-2")]
        assert _upload.get_upload_loss_stats() == {"exit_timeout": 4}
        assert any(
            "4 trace(s) not sent: the process exited before their upload ended (waited 0.2s; "
            "set LAYERLENS_EXIT_TIMEOUT to wait longer, or call layerlens.instrument.flush() before exiting)"
            in r.getMessage()
            for r in caplog.records
        )

    def test_an_exit_that_sent_everything_says_nothing(self, monkeypatch: Any, caplog: Any, losses: List[Any]) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        ch = _get_channel(client)
        for i in range(3):
            assert ch.enqueue(client, {"trace_id": f"t{i}"}) is True
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _upload._flush_at_exit()
        assert client.traces.upload.call_count == 3
        assert losses == [] and _upload.get_upload_loss_stats() == {}
        assert not [r for r in caplog.records if "not sent" in r.getMessage()]


def _slow_uploads(client: Mock, seconds: float) -> None:
    """Each upload takes ``seconds``, then is accepted."""

    def upload(path: str) -> Mock:  # noqa: ARG001
        time.sleep(seconds)
        return Mock(trace_ids=["accepted"])

    client.traces.upload.side_effect = upload


class TestFlushDeliversAtAKnownPoint:
    """BUG-01: a short-lived process lost most of its traces at exit, and had no way to wait for
    them: the exit's 5 s budget against ~1-2 s per upload. ``flush(timeout)`` waits for them."""

    def test_flush_is_public(self) -> None:
        from layerlens.instrument import flush

        assert flush is _upload.flush

    def test_flush_sends_every_clients_queue_then_returns_true(self, monkeypatch: Any, losses: List[Any]) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        a, b = _make_client(), _make_client()
        _slow_uploads(a, 0.02)
        _slow_uploads(b, 0.02)
        for i in range(20):
            assert _upload.enqueue_upload(a, {"trace_id": f"a{i}"}) is True
        for i in range(5):
            assert _upload.enqueue_upload(b, {"trace_id": f"b{i}"}) is True

        assert _upload.flush(timeout=10.0) is True
        assert a.traces.upload.call_count == 20 and b.traces.upload.call_count == 5
        assert losses == []

    def test_flush_without_a_timeout_waits_until_everything_is_sent(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        _slow_uploads(client, 0.05)
        for i in range(8):
            assert _upload.enqueue_upload(client, {"trace_id": f"t{i}"}) is True
        assert _upload.flush() is True
        assert client.traces.upload.call_count == 8

    def test_flush_returns_false_at_its_timeout_and_leaves_the_rest_to_send(
        self, monkeypatch: Any, losses: List[Any]
    ) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        release, started = threading.Event(), threading.Event()
        _held_uploads(client, release, started)
        try:
            assert _upload.enqueue_upload(client, {"trace_id": "in-flight"}) is True
            assert started.wait(5.0)
            for i in range(3):
                assert _upload.enqueue_upload(client, {"trace_id": f"queued-{i}"}) is True
            t0 = time.monotonic()
            assert _upload.flush(timeout=0.2) is False
            assert time.monotonic() - t0 < 2.0
        finally:
            release.set()
        # Not lost: the worker goes on, and a later flush sees them sent.
        assert _upload.flush(timeout=10.0) is True
        assert client.traces.upload.call_count == 4
        assert losses == []

    def test_flush_polled_during_a_slow_upload_leaves_the_queue_to_traces(
        self, monkeypatch: Any, losses: List[Any]
    ) -> None:
        # CodeRabbit on #99: every flush() put a wake-up marker on the queue, though only the first
        # wakes the worker. A program polling flush(timeout=0) while one upload was slow filled the
        # queue's places with markers, and its next traces were dropped as queue_full.
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        release, started = threading.Event(), threading.Event()
        _held_uploads(client, release, started)
        try:
            assert _upload.enqueue_upload(client, {"trace_id": "slow"}) is True
            assert started.wait(5.0)
            for _ in range(_upload._get_channel(client)._queue.maxsize + 6):
                assert _upload.flush(timeout=0) is False
            assert _upload.enqueue_upload(client, {"trace_id": "after-polling"}) is True
        finally:
            release.set()
        assert _upload.flush(timeout=10.0) is True
        assert client.traces.upload.call_count == 2
        assert losses == []

    def test_a_timeout_past_what_a_thread_can_wait_is_as_long_as_it_takes(self, monkeypatch: Any) -> None:
        # Thread.join raises OverflowError past threading.TIMEOUT_MAX (CodeRabbit on #99).
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        _slow_uploads(client, 0.05)
        for i in range(3):
            assert _upload.enqueue_upload(client, {"trace_id": f"t{i}"}) is True
        assert _upload.flush(timeout=threading.TIMEOUT_MAX * 10) is True
        assert client.traces.upload.call_count == 3

    def test_flush_with_nothing_queued_returns_true_at_once(self) -> None:
        t0 = time.monotonic()
        assert _upload.flush(timeout=5.0) is True
        assert time.monotonic() - t0 < 1.0

    def test_flush_starts_a_worker_for_traces_queued_while_none_could_start(self, monkeypatch: Any) -> None:
        # A worker that failed to start (thread exhaustion) left its traces queued.
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        ch = _get_channel(client)
        for i in range(3):
            ch._queue.put_nowait((client, {"trace_id": f"t{i}"}))
        assert ch._worker is None
        assert _upload.flush(timeout=10.0) is True
        assert client.traces.upload.call_count == 3

    def test_tracing_goes_on_after_a_flush(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        assert _upload.enqueue_upload(client, {"trace_id": "before"}) is True
        assert _upload.flush(timeout=10.0) is True
        assert _upload.enqueue_upload(client, {"trace_id": "after"}) is True
        assert _upload.flush(timeout=10.0) is True
        assert client.traces.upload.call_count == 2

    def test_flush_from_a_loss_callback_on_the_worker_does_not_deadlock(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        client.traces.upload.return_value = Mock(trace_ids=[])  # rejected: the callback runs on the worker
        results: List[Any] = []
        _upload.set_upload_loss_callback(lambda reason, payload: results.append(_upload.flush(timeout=0.5)))  # noqa: ARG005
        try:
            assert _upload.enqueue_upload(client, {"trace_id": "t"}) is True
            _wait_until(lambda: bool(results))
        finally:
            _upload.set_upload_loss_callback(None)
            _upload.reset_upload_loss_stats()
        # It cannot wait for the worker running the callback, whose trace is not done until the
        # callback returns: it says so (False) instead of blocking or claiming all is sent.
        assert results == [False]


class TestTheExitBudgetIsLayerlensExitTimeout:
    """BUG-01: the exit hook waited a fixed 5 s, so a CI job lost what it could not send in that
    time. ``LAYERLENS_EXIT_TIMEOUT`` sets the budget, read when the process exits; the default
    stays 5 s."""

    def _exit_with_four_unsent(self, monkeypatch: Any, caplog: Any) -> float:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        client = _make_client()
        ch = _get_channel(client)
        release, started = threading.Event(), threading.Event()
        _held_uploads(client, release, started)
        try:
            assert ch.enqueue(client, {"trace_id": "in-flight"}) is True
            assert started.wait(5.0)
            for i in range(3):
                assert ch.enqueue(client, {"trace_id": f"queued-{i}"}) is True
            t0 = time.monotonic()
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                _upload._flush_at_exit()
            return time.monotonic() - t0
        finally:
            release.set()

    def test_the_exit_waits_as_long_as_the_variable_says(
        self, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        monkeypatch.setattr(_upload, "_EXIT_TIMEOUT_S", 30.0)  # the default, were the variable not read
        monkeypatch.setenv("LAYERLENS_EXIT_TIMEOUT", "0.3")
        waited = self._exit_with_four_unsent(monkeypatch, caplog)
        assert 0.25 < waited < 3.0, f"the exit waited {waited:.2f}s with LAYERLENS_EXIT_TIMEOUT=0.3"
        assert _upload.get_upload_loss_stats() == {"exit_timeout": 4}
        warned = [r.getMessage() for r in caplog.records if "not sent" in r.getMessage()]
        assert warned and "(waited 0.3s" in warned[0] and "LAYERLENS_EXIT_TIMEOUT" in warned[0], warned

    def test_a_longer_budget_lets_every_trace_go_up(self, monkeypatch: Any, losses: List[Any]) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        monkeypatch.setattr(_upload, "_EXIT_TIMEOUT_S", 0.1)  # far too short for these uploads
        monkeypatch.setenv("LAYERLENS_EXIT_TIMEOUT", "10")
        client = _make_client()
        _slow_uploads(client, 0.05)
        for i in range(10):
            assert _upload.enqueue_upload(client, {"trace_id": f"t{i}"}) is True
        _upload._flush_at_exit()
        assert client.traces.upload.call_count == 10
        assert losses == []

    @pytest.mark.parametrize("value", ["soon", "-1", "nan", "inf", "1e400"])
    def test_a_value_it_cannot_use_falls_back_to_the_default_with_a_warning(
        self, value: str, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        monkeypatch.setattr(_upload, "_EXIT_TIMEOUT_S", 0.2)
        monkeypatch.setenv("LAYERLENS_EXIT_TIMEOUT", value)
        waited = self._exit_with_four_unsent(monkeypatch, caplog)
        assert waited < 3.0
        assert any(
            "LAYERLENS_EXIT_TIMEOUT" in r.getMessage() and repr(value) in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_zero_means_do_not_wait(self, monkeypatch: Any, caplog: Any, losses: List[Any]) -> None:
        monkeypatch.setattr(_upload, "_EXIT_TIMEOUT_S", 30.0)
        monkeypatch.setenv("LAYERLENS_EXIT_TIMEOUT", "0")
        waited = self._exit_with_four_unsent(monkeypatch, caplog)
        assert waited < 1.0
        assert _upload.get_upload_loss_stats() == {"exit_timeout": 4}


class TestTheChannelsDocumentedContract:
    """What the module docstring and the docs promise: each drop counted under its reason and
    logged, the breaker's bounds, a worker that never holds the process open, and the file
    each trace goes up in."""

    def _worker_runs(self, monkeypatch: Any, client: Mock, payload: dict) -> UploadChannel:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        assert ch.enqueue(client, payload) is True
        _wait_until(lambda: client.traces.upload.call_count >= 1 or bool(_upload.get_upload_loss_stats()))
        ch.shutdown(timeout=5.0)
        return ch

    def test_a_full_queue_drops_the_65th_trace_counted_as_queue_full(
        self, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        monkeypatch.setattr(ch, "_ensure_worker", lambda: None)  # nothing drains
        for i in range(64):  # the documented 64 per client
            assert ch.enqueue(client, {"trace_id": f"t{i}"}) is True
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert ch.enqueue(client, {"trace_id": "overflow"}) is False
        assert losses == [("queue_full", "overflow")]
        assert _upload.get_upload_loss_stats() == {"queue_full": 1}
        assert any(r.getMessage() == "layerlens: upload queue full, dropping trace overflow" for r in caplog.records)

    def test_an_upload_the_api_rejects_is_counted_and_named(
        self, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        client = _make_client()
        client.traces.upload.return_value = Mock(trace_ids=[])
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            ch = self._worker_runs(monkeypatch, client, {"trace_id": "t-rejected"})
        assert losses == [("rejected", "t-rejected")]
        assert ch._error_count == 1
        assert any(
            r.getMessage() == "layerlens: background trace upload rejected (no trace_ids) for trace t-rejected"
            for r in caplog.records
        )

    def test_an_upload_that_raises_is_counted_and_logged_with_its_traceback(
        self, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        client = _make_client()
        client.traces.upload.side_effect = RuntimeError("backend down")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            self._worker_runs(monkeypatch, client, {"trace_id": "t-error"})
        assert losses == [("upload_error", "t-error")]
        failed = [r for r in caplog.records if r.getMessage() == "layerlens: background trace upload failed"]
        assert failed and failed[0].exc_info is not None

    def test_a_trace_that_cannot_be_serialized_is_counted_and_logged(
        self, monkeypatch: Any, caplog: Any, losses: List[Any]
    ) -> None:
        client = _make_client()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            self._worker_runs(monkeypatch, client, _circular_payload())
        assert losses == [("serialize_error", "bad")]
        failed = [r for r in caplog.records if r.getMessage() == "layerlens: failed to serialize trace for upload"]
        assert failed and failed[0].exc_info is not None

    def test_traces_dequeued_while_the_breaker_is_open_are_counted(self, monkeypatch: Any, losses: List[Any]) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        for i in range(2):
            ch._queue.put_nowait((client, {"trace_id": f"t{i}"}))
        for _ in range(10):
            ch._on_failure()
        ch._ensure_worker()
        _wait_until(lambda: len(losses) == 2)
        assert losses == [("circuit_open", "t0"), ("circuit_open", "t1")]
        assert _upload.get_upload_loss_stats() == {"circuit_open": 2}
        ch.shutdown(timeout=2.0)

    def test_an_accepted_upload_counts_no_loss(self, monkeypatch: Any, losses: List[Any]) -> None:
        client = _make_client()
        client.traces.upload.return_value = Mock(trace_ids=["srv-1"])
        self._worker_runs(monkeypatch, client, {"trace_id": "t-ok"})
        assert client.traces.upload.call_count == 1
        assert losses == [] and _upload.get_upload_loss_stats() == {}

    @pytest.mark.parametrize(
        "upload, reason, logged",
        [
            (RuntimeError("down"), "upload_error", "layerlens: trace upload failed"),
            (Mock(trace_ids=[]), "rejected", "layerlens: trace upload rejected (no trace_ids) for trace t-sync"),
        ],
        ids=["raises", "rejected"],
    )
    def test_the_sync_path_counts_and_logs_the_same_way(
        self, caplog: Any, losses: List[Any], upload: Any, reason: str, logged: str
    ) -> None:
        client = _make_client()
        if isinstance(upload, Exception):
            client.traces.upload.side_effect = upload
        else:
            client.traces.upload.return_value = upload
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _upload.upload_trace(client, {"trace_id": "t-sync"})
        assert losses == [(reason, "t-sync")]
        records = [r for r in caplog.records if r.getMessage() == logged]
        assert records and (records[0].exc_info is not None) == (reason == "upload_error")

    def test_the_sync_path_counts_a_trace_that_cannot_be_serialized(self, caplog: Any, losses: List[Any]) -> None:
        client = _make_client()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _upload.upload_trace(client, _circular_payload())
        assert losses == [("serialize_error", "bad")]
        failed = [r for r in caplog.records if r.getMessage() == "layerlens: failed to serialize trace for upload"]
        assert failed and failed[0].exc_info is not None
        client.traces.upload.assert_not_called()

    def test_the_breaker_opens_on_the_tenth_failure_in_a_row(self, caplog: Any) -> None:
        ch = UploadChannel()
        for _ in range(9):
            ch._on_failure()
        assert ch._allow() is True
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            ch._on_failure()
        assert ch._allow() is False
        assert any(
            r.getMessage() == "layerlens: upload circuit breaker OPEN after 10 errors (cooldown 60s)"
            for r in caplog.records
        )

    def test_the_breaker_half_opens_after_exactly_60_seconds(self, monkeypatch: Any, caplog: Any) -> None:
        now = [1000.0]
        monkeypatch.setattr(_upload, "time", SimpleNamespace(monotonic=lambda: now[0]))
        ch = UploadChannel()
        for _ in range(10):
            ch._on_failure()
        now[0] = 1059.999
        assert ch._allow() is False
        now[0] = 1060.0
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert ch._allow() is True
        assert ch._error_count == 0 and ch._circuit_open is False
        assert any(r.getMessage() == "layerlens: upload circuit breaker half-open, retrying" for r in caplog.records)

    def test_one_success_clears_a_single_failure(self) -> None:
        ch = UploadChannel()
        ch._on_failure()
        ch._on_success()
        assert ch._error_count == 0

    def test_the_worker_is_a_named_daemon_so_it_never_holds_the_process_open(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch, client = UploadChannel(), _make_client()
        assert ch.enqueue(client, {"trace_id": "t"}) is True
        worker = ch._worker
        assert worker is not None and worker.daemon is True and worker.name == "layerlens-upload"
        ch.shutdown(timeout=2.0)

    def test_each_trace_goes_up_as_its_own_json_file_which_is_then_removed(self) -> None:
        client, seen = _make_client(), []

        def upload(path: str) -> Mock:
            with open(path, encoding="utf-8") as f:
                seen.append((os.path.basename(path), json.load(f)))
            return Mock(trace_ids=["srv"])

        client.traces.upload.side_effect = upload
        _upload.upload_trace(client, {"trace_id": "t", "events": []})
        ((name, content),) = seen
        # traces.upload picks its parser by the extension: .json holds one JSON array.
        assert name.startswith("layerlens_trace_") and name.endswith(".json")
        assert content == [{"trace_id": "t", "events": []}]
        assert not os.path.exists(os.path.join(tempfile.gettempdir(), name))

    @pytest.mark.parametrize("background", [False, True], ids=["sync", "background"])
    def test_an_accepted_upload_hands_exec_the_servers_first_trace_id(
        self, monkeypatch: Any, tmp_path: Any, background: bool
    ) -> None:
        # LAY-4430: `layerlens env run exec` links the agent's trace from LAYERLENS_TRACE_ID_FILE.
        id_file = tmp_path / "trace-id.txt"
        id_file.write_text("")
        monkeypatch.setenv(TRACE_ID_FILE_ENV, str(id_file))
        client = _make_client()
        client.traces.upload.return_value = Mock(trace_ids=["srv-1", "srv-2"])
        if background:
            self._worker_runs(monkeypatch, client, {"trace_id": "t"})
        else:
            _upload.upload_trace(client, {"trace_id": "t"})
        assert id_file.read_text() == "srv-1"

    @pytest.mark.parametrize("ids", [[], [123], None], ids=["rejected", "not-a-string", "no-ids"])
    def test_no_trace_id_is_handed_over_without_a_server_id(self, monkeypatch: Any, tmp_path: Any, ids: Any) -> None:
        id_file = tmp_path / "trace-id.txt"
        id_file.write_text("")
        monkeypatch.setenv(TRACE_ID_FILE_ENV, str(id_file))
        client = _make_client()
        client.traces.upload.return_value = Mock(trace_ids=ids)
        _upload.upload_trace(client, {"trace_id": "t"})
        assert id_file.read_text() == ""

    def test_a_client_with_neither_attributes_nor_weakrefs_keeps_one_channel(self) -> None:
        class Frozen:
            __slots__ = ("traces",)  # no __dict__ and no __weakref__

            def __init__(self) -> None:
                self.traces = Mock()

        client = Frozen()
        first = _get_channel(client)
        assert _get_channel(client) is first
        assert _upload._pinned_side_channels[id(client)] == (client, first)
        del _upload._pinned_side_channels[id(client)]


# ---------------------------------------------------------------------------
# Sync mode
# ---------------------------------------------------------------------------


class TestSyncMode:
    def test_sync_mode_uploads_inline(self) -> None:
        # The autouse conftest fixture sets _sync_mode = True.
        client = _make_client()
        assert _upload.enqueue_upload(client, {"trace_id": "t"}) is True
        assert client.traces.upload.call_count == 1

    def test_sync_mode_swallows_upload_failure(self) -> None:
        client = _make_client()
        client.traces.upload.side_effect = RuntimeError("backend down")
        assert _upload.enqueue_upload(client, {"trace_id": "t"}) is True  # never raises

    def test_upload_failure_is_observable_not_silent(self, caplog: Any) -> None:
        """The blanket ``except Exception`` in ``_upload_sync`` must DEGRADE
        OBSERVABLY, not silently: a failed upload logs a WARNING and feeds the
        circuit breaker. This is the same swallow that hid the schema lock
        (LAY-3613) — line coverage alone wouldn't prove the drop is visible."""
        client = _make_client()
        client.traces.upload.side_effect = RuntimeError("backend down")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _upload.enqueue_upload(client, {"trace_id": "t"})
        assert any("trace upload failed" in r.message for r in caplog.records)
        # The breaker counted the failure (a silent drop would leave it at 0).
        assert _upload._get_channel(client)._error_count == 1


# ---------------------------------------------------------------------------
# Worker resilience
# ---------------------------------------------------------------------------


class TestWorkerResilience:
    def test_failed_upload_does_not_kill_worker(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        client = _make_client()
        calls: List[str] = []

        def _upload_fn(path: str) -> None:
            calls.append(path)
            if len(calls) == 1:
                raise RuntimeError("transient")

        client.traces.upload.side_effect = _upload_fn
        assert ch.enqueue(client, {"trace_id": "t1"}) is True
        assert ch.enqueue(client, {"trace_id": "t2"}) is True
        _wait_until(lambda: len(calls) >= 2)

        assert len(calls) == 2  # second item still processed
        assert ch._worker is not None and ch._worker.is_alive()
        ch.shutdown(timeout=2.0)


# ---------------------------------------------------------------------------
# Serialize-failure resilience (LAY-3572 / R3 / B37 / B38)
#
# `_write_trace_file` was called OUTSIDE the worker try/finally. A payload that
# fails to serialize (e.g. a circular reference) (a) leaked its temp file and
# (b) propagated out of the worker loop, killing the daemon thread so every
# already-queued trace behind it was lost.
# ---------------------------------------------------------------------------


def _circular_payload() -> dict:
    p: dict = {"trace_id": "bad"}
    p["loop"] = p  # json.dump raises ValueError: Circular reference detected
    return p


class TestSerializeFailureResilience:
    def test_sync_serialize_failure_leaves_no_temp_file(self, monkeypatch: Any) -> None:
        created: List[str] = []
        real_mkstemp = _upload.tempfile.mkstemp

        def tracking_mkstemp(*a: Any, **k: Any):
            fd, path = real_mkstemp(*a, **k)
            created.append(path)
            return fd, path

        monkeypatch.setattr(_upload.tempfile, "mkstemp", tracking_mkstemp)
        client = _make_client()
        # autouse _sync_mode=True -> _upload_sync; must not raise, must not leak.
        assert _upload.enqueue_upload(client, _circular_payload()) is True
        leaked = [p for p in created if os.path.exists(p)]
        assert created, "test bug: _write_trace_file was never reached"
        assert not leaked, f"serialize failure leaked temp file(s): {leaked}"
        client.traces.upload.assert_not_called()  # nothing valid to upload

    def test_worker_survives_serialize_failure_and_processes_next(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_upload, "_sync_mode", False)
        ch = UploadChannel()
        client = _make_client()
        # Queue a poison (un-serializable) trace AHEAD of a good one, then start
        # ONE worker. The good trace must still be uploaded.
        ch._queue.put_nowait((client, _circular_payload()))
        ch._queue.put_nowait((client, {"trace_id": "good"}))
        ch._ensure_worker()
        _wait_until(lambda: client.traces.upload.call_count >= 1)
        assert client.traces.upload.call_count == 1, "good trace behind a poison trace was lost (worker died)"
        assert ch._worker is not None and ch._worker.is_alive(), "worker died on a serialize failure"
        ch.shutdown(timeout=2.0)
