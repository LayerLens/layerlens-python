"""Background trace upload: per-client channels, queue, circuit breaker.

Delivery is best-effort by design — instrumentation must never crash or block
the host app. The loss modes are deliberate and bounded:

* queue overflow (64/client): new traces dropped with a WARNING;
* circuit breaker (10 consecutive failures → open 60 s): traces discarded,
  including items already dequeued by the worker, until the half-open retry;
* process exit: ``atexit`` drains via :func:`shutdown_uploads`, which waits at
  most its timeout (5 s) in all, however many clients, and returns as soon as
  every queue is sent. A trace still unsent when that time is up dies with the
  process: it is counted (``exit_timeout``) and a WARNING says how many, rather
  than vanishing silently. Abrupt kills can lose queued traces unreported (no
  public flush API yet — D1 decision).
"""

from __future__ import annotations

import os
import json
import time
import queue
import atexit
import logging
import weakref
import tempfile
import threading
from typing import Any, Dict, Tuple, Callable, Optional

from .._trace_id_file import offer_trace_id

log: logging.Logger = logging.getLogger(__name__)


def _upload_succeeded(result: Any) -> bool:
    """True only if the backend ACCEPTED the trace (returned trace_ids).

    ``traces.upload`` returns an empty/None ``trace_ids`` when the backend REJECTS
    the upload WITHOUT raising (e.g. a schema reject that the SDK swallows). Such a
    reject must count as a FAILURE (F-L7-002), not a success — otherwise the circuit
    breaker stays blind to persistent rejects and the trace is silently lost while
    counted as delivered."""
    return result is not None and bool(getattr(result, "trace_ids", None))


def _offer_server_trace_id(result: Any) -> None:
    """Hand ``layerlens env run exec`` the id the server gave an accepted trace (LAY-4430).

    One payload is uploaded per file, so the first id is that trace's.
    """
    ids = getattr(result, "trace_ids", None)
    if isinstance(ids, list) and ids and isinstance(ids[0], str):
        offer_trace_id(ids[0])


# ---------------------------------------------------------------------------
# Data-loss observability (opt-in callback + always-on per-reason counter)
# ---------------------------------------------------------------------------
# Upload is best-effort, so traces can be dropped (queue overflow, circuit
# breaker, serialize/upload failure). Loss was previously only logged; this
# gives the host app a programmatic signal — an OTel-style drop counter plus an
# opt-in callback — WITHOUT changing the default best-effort behavior (F-L7-001).

_LOSS_REASONS = ("queue_full", "circuit_open", "serialize_error", "rejected", "upload_error", "exit_timeout")

_loss_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
_loss_counts: Dict[str, int] = {}
_loss_lock = threading.Lock()


def set_upload_loss_callback(cb: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
    """Register a callback fired (best-effort) whenever a trace is dropped.

    Called as ``cb(reason, payload)`` where ``reason`` is one of
    :data:`_LOSS_REASONS`. Opt-in (default ``None``); a raising callback is
    swallowed so it can never break the upload path. Pass ``None`` to clear.
    """
    global _loss_callback
    _loss_callback = cb


def get_upload_loss_stats() -> Dict[str, int]:
    """Return a snapshot of cumulative dropped-trace counts keyed by reason."""
    with _loss_lock:
        return dict(_loss_counts)


def reset_upload_loss_stats() -> None:
    """Reset the drop counters (primarily for tests)."""
    with _loss_lock:
        _loss_counts.clear()


def _record_loss(reason: str, payload: Dict[str, Any]) -> None:
    """Count a dropped trace and notify the opt-in callback (crash-safe)."""
    with _loss_lock:
        _loss_counts[reason] = _loss_counts.get(reason, 0) + 1
    cb = _loss_callback
    if cb is not None:
        try:
            cb(reason, payload)
        except Exception:
            log.warning("layerlens: upload_loss_callback raised", exc_info=True)


# ---------------------------------------------------------------------------
# Per-client upload channel
# ---------------------------------------------------------------------------


class UploadChannel:
    """Per-client upload state: circuit breaker + background worker + queue.

    Each ``client`` gets its own channel so that a failing backend A
    doesn't trip the breaker for a healthy backend B.
    """

    _THRESHOLD = 10
    _COOLDOWN_S = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._error_count = 0
        self._circuit_open = False
        self._opened_at: float = 0.0
        self._queue: queue.Queue[Optional[Tuple[Any, Dict[str, Any]]]] = queue.Queue(maxsize=64)
        self._worker: Optional[threading.Thread] = None
        # Set by a shutdown: the worker sends what is queued, then ends instead of waiting for
        # more. A worker started later clears it. Guarded by ``_lock`` (LAY-4454).
        self._stop_when_empty = False
        # The trace the worker is uploading now, so an exit that cannot wait for it can say so.
        self._sending: Optional[Dict[str, Any]] = None
        # The last attempt to start a worker failed; warn once until one starts.
        self._start_failed = False

    # -- Circuit breaker --

    def _allow(self) -> bool:
        with self._lock:
            if not self._circuit_open:
                return True
            if time.monotonic() - self._opened_at >= self._COOLDOWN_S:
                self._circuit_open = False
                self._error_count = 0
                log.info("layerlens: upload circuit breaker half-open, retrying")
                return True
            return False

    def _on_success(self) -> None:
        with self._lock:
            if self._error_count > 0:
                self._error_count = 0
                self._circuit_open = False

    def _on_failure(self) -> None:
        with self._lock:
            self._error_count += 1
            if self._error_count >= self._THRESHOLD and not self._circuit_open:
                self._circuit_open = True
                self._opened_at = time.monotonic()
                log.warning(
                    "layerlens: upload circuit breaker OPEN after %d errors (cooldown %.0fs)",
                    self._error_count,
                    self._COOLDOWN_S,
                )

    # -- Worker thread --

    def _worker_loop(self) -> None:
        while True:
            # Ending is decided under the lock _ensure_worker takes, and only with nothing
            # queued: a trace queued meanwhile is either still in the queue here, or finds
            # ``_worker`` cleared and starts a new worker.
            with self._lock:
                if self._stop_when_empty and self._queue.empty():
                    self._worker = None
                    return
            item = self._queue.get()
            if item is None:
                # The wake-up a shutdown sends a worker waiting on an empty queue.
                continue
            client, payload = item
            if not self._allow():
                _record_loss("circuit_open", payload)
                continue
            try:
                path = _write_trace_file(payload)
            except Exception:
                # A serialize failure must not kill the daemon worker (every
                # queued trace behind it would be lost) — count it and continue.
                self._on_failure()
                _record_loss("serialize_error", payload)
                log.warning("layerlens: failed to serialize trace for upload", exc_info=True)
                continue
            self._sending = payload
            try:
                result = client.traces.upload(path)
                if _upload_succeeded(result):
                    self._on_success()
                    _offer_server_trace_id(result)
                else:
                    self._on_failure()
                    _record_loss("rejected", payload)
                    log.warning(
                        "layerlens: background trace upload rejected (no trace_ids) for trace %s",
                        payload.get("trace_id", "?"),
                    )
            except Exception:
                self._on_failure()
                _record_loss("upload_error", payload)
                log.warning("layerlens: background trace upload failed", exc_info=True)
            finally:
                self._sending = None
                try:
                    os.unlink(path)
                except OSError:
                    log.debug("Failed to remove temp trace file: %s", path)

    def _ensure_worker(self) -> None:
        # Always under the lock: a worker that is ending clears ``_worker`` under it, and a
        # check without it could see that worker alive just before it returns.
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_when_empty = False
            worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="layerlens-upload",
            )
            try:
                worker.start()
            except Exception as exc:
                # Thread exhaustion, or Python 3.12+ refusing new threads at interpreter
                # shutdown (a traced call in a program's own atexit handler). Tracing never
                # raises into the program: the trace stays queued for the next enqueue's
                # worker, and what is still queued at exit is counted (``exit_timeout``).
                self._worker = None
                if not self._start_failed:
                    self._start_failed = True
                    log.warning(
                        "layerlens: could not start the upload worker (%s); queued traces wait for the next", exc
                    )
                return
            self._start_failed = False
            self._worker = worker

    def enqueue(self, client: Any, payload: Dict[str, Any]) -> bool:
        """Enqueue a trace for background upload. Returns False if dropped."""
        if _sync_mode:
            self._upload_sync(client, payload)
            return True
        if not self._allow():
            _record_loss("circuit_open", payload)
            return False
        try:
            self._queue.put_nowait((client, payload))
        except queue.Full:
            # A queue that filled because no worker could start: try again, so what is queued
            # can still be sent. This trace is dropped either way.
            self._ensure_worker()
            _record_loss("queue_full", payload)
            log.warning(
                "layerlens: upload queue full, dropping trace %s",
                payload.get("trace_id", "?"),
            )
            return False
        # After the put, so a worker ending after a shutdown sees this trace, or has already
        # cleared ``_worker`` and a new one is started for it.
        self._ensure_worker()
        return True

    def _upload_sync(self, client: Any, payload: Dict[str, Any]) -> None:
        """Synchronous upload (used in tests)."""
        if not self._allow():
            _record_loss("circuit_open", payload)
            return
        try:
            path = _write_trace_file(payload)
        except Exception:
            self._on_failure()
            _record_loss("serialize_error", payload)
            log.warning("layerlens: failed to serialize trace for upload", exc_info=True)
            return
        try:
            result = client.traces.upload(path)
            if _upload_succeeded(result):
                self._on_success()
                _offer_server_trace_id(result)
            else:
                self._on_failure()
                _record_loss("rejected", payload)
                log.warning(
                    "layerlens: trace upload rejected (no trace_ids) for trace %s",
                    payload.get("trace_id", "?"),
                )
        except Exception:
            self._on_failure()
            _record_loss("upload_error", payload)
            log.warning("layerlens: trace upload failed", exc_info=True)
        finally:
            try:
                os.unlink(path)
            except OSError:
                log.debug("Failed to remove temp trace file: %s", path)

    def _stop_when_drained(self) -> Optional[threading.Thread]:
        """Tell the worker to end once the queue is empty; returns it, to wait for, if it runs.

        Never blocks. The worker checks the flag before each wait for a trace, so it ends
        once what is queued is sent, however full the queue was. The stop used to be a marker
        put on the queue, dropped when the queue was full: the worker then sent the 64 queued
        traces and waited for more, and the exit waited out its whole timeout (LAY-4454).
        """
        with self._lock:
            worker = self._worker
            if worker is None or not worker.is_alive():
                return None
            self._stop_when_empty = True
        try:
            # Wakes a worker waiting on an empty queue. A full queue needs no wake-up: its
            # worker is busy, and sees the flag before it waits again.
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        return worker

    def shutdown(self, timeout: float = 5.0) -> None:
        """Send what is queued, then stop the worker; waits at most ``timeout`` seconds.

        A trace queued later starts a new worker.
        """
        worker = self._stop_when_drained()
        if worker is not None:
            worker.join(timeout)

    def _left_unsent_at_exit(self) -> int:
        """Count each trace this channel could not send before the process ends as lost
        (``exit_timeout``): the ones still queued, and the one being uploaded. Returns how many.

        Only for the exit hook: anywhere else the worker would still send them.
        """
        lost = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                lost.append(item[1])
        sending = self._sending
        worker = self._worker
        if sending is not None and worker is not None and worker.is_alive():
            lost.append(sending)
        for payload in lost:
            _record_loss("exit_timeout", payload)
        return len(lost)


# ---------------------------------------------------------------------------
# Channel registry (one per client)
# ---------------------------------------------------------------------------

_ATTR = "_layerlens_upload_channel"
_channels: list[UploadChannel] = []  # keeps refs for shutdown_uploads
_registry_lock = threading.Lock()
# Fallback for clients that reject attribute injection (slotted/frozen):
# weakly keyed so a dropped client doesn't pin its channel forever.
_side_channels: "weakref.WeakKeyDictionary[Any, UploadChannel]" = weakref.WeakKeyDictionary()
# Last resort for clients that are not weak-referenceable either; deliberately
# pins the client (bounded by the number of distinct clients in a process).
_pinned_side_channels: Dict[int, Tuple[Any, UploadChannel]] = {}


def _lookup_side_channel(client: Any) -> Optional[UploadChannel]:
    try:
        ch = _side_channels.get(client)
    except TypeError:
        ch = None
    if ch is not None:
        return ch
    pinned = _pinned_side_channels.get(id(client))
    if pinned is not None and pinned[0] is client:
        return pinned[1]
    return None


def _get_channel(client: Any) -> UploadChannel:
    """Return (or create) the UploadChannel for *client*.

    The channel is stored directly on the client object so that identity
    is tied to the object's lifetime, not its ``id()`` (which can be
    reused after garbage collection). Slotted/frozen clients that reject
    attribute injection fall back to a weak side dict instead.
    """
    ch = getattr(client, _ATTR, None)
    if isinstance(ch, UploadChannel):
        return ch
    with _registry_lock:
        # Double-check under lock
        ch = getattr(client, _ATTR, None)
        if isinstance(ch, UploadChannel):
            return ch
        ch = _lookup_side_channel(client)
        if ch is not None:
            return ch
        ch = UploadChannel()
        try:
            object.__setattr__(client, _ATTR, ch)
        except (AttributeError, TypeError):
            try:
                _side_channels[client] = ch
            except TypeError:
                _pinned_side_channels[id(client)] = (client, ch)
        _channels.append(ch)
        return ch


# ---------------------------------------------------------------------------
# Public API (used by TraceCollector)
# ---------------------------------------------------------------------------

_sync_mode = False


def enqueue_upload(client: Any, payload: Dict[str, Any]) -> bool:
    """Enqueue a trace for background upload via the client's channel."""
    return _get_channel(client).enqueue(client, payload)


def shutdown_uploads(timeout: float = 5.0) -> None:
    """Send every channel's queued traces, waiting at most ``timeout`` seconds in all.

    Every worker is told to stop first, then all are waited for against one deadline, so the
    channels drain at the same time: the wait is the slowest channel's, not the sum of each
    channel's timeout. Safe to call repeatedly; a trace queued later starts a new worker.
    """
    deadline = time.monotonic() + timeout
    with _registry_lock:
        channels = list(_channels)
    workers = [worker for worker in (ch._stop_when_drained() for ch in channels) if worker is not None]
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


# How long the process's exit waits, in all, for the traces still queued.
_EXIT_TIMEOUT_S = 5.0


def _flush_at_exit() -> None:
    """The exit hook: send what is queued within ``_EXIT_TIMEOUT_S``, then account for what is not.

    A trace still unsent then dies with the process. It used to vanish without a word; each
    one is now counted as lost (``exit_timeout``, also through the loss callback) and one
    WARNING says how many (LAY-4454).
    """
    shutdown_uploads(_EXIT_TIMEOUT_S)
    with _registry_lock:
        channels = list(_channels)
    unsent = sum(ch._left_unsent_at_exit() for ch in channels)
    if unsent:
        log.warning(
            "layerlens: %d trace(s) not sent: the process exited before their upload ended (waited %.0fs)",
            unsent,
            _EXIT_TIMEOUT_S,
        )


atexit.register(_flush_at_exit)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_trace_file(payload: Dict[str, Any]) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="layerlens_trace_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump([payload], f, default=str)
    except Exception:
        # A serialization failure (e.g. a circular reference) must not leave the
        # mkstemp'd file orphaned on disk (LAY-3572 / B37).
        try:
            os.unlink(path)
        except OSError:
            log.debug("Failed to remove temp trace file after write failure: %s", path)
        raise
    return path


def upload_trace(client: Any, payload: Dict[str, Any]) -> None:
    """Synchronous upload (testing convenience)."""
    _get_channel(client)._upload_sync(client, payload)
