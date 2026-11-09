from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, Optional, Generator
from contextlib import contextmanager

from ._tags import _pop_scope, _push_scope, tags_argument
from ._context import (
    _pop_span,
    _push_span,
    _parent_span_id,
    _current_span_id,
    _current_collector,
)
from ._collector import TraceCollector
from ._capture_config import CaptureConfig


@contextmanager
def trace_context(
    client: Any,
    *,
    capture_config: Optional[CaptureConfig] = None,
    from_context: Optional[Dict[str, Any]] = None,
    tags: Optional[Iterable[str]] = None,
) -> Generator[TraceCollector, None, None]:
    """Establish a shared trace context for multiple adapters.

    Creates a :class:`TraceCollector` and sets it as the active collector
    in ``contextvars`` so that any adapter emitting events inside the
    block will use the same ``trace_id`` and span hierarchy.

    When *from_context* is provided (a dict from :func:`get_trace_context`),
    the new collector reuses the original ``trace_id`` so events on both
    sides of a boundary belong to the same trace.

    The collector is flushed automatically when the context exits.

    Args:
        client: A :class:`~layerlens.LayerLens` (or compatible) client used
            for uploading the trace on flush.
        capture_config: Optional capture configuration.  Falls back to
            :meth:`CaptureConfig.standard` if not provided.
        from_context: Optional dict produced by :func:`get_trace_context`.
            When supplied the collector inherits the original trace_id, but
            not the original trace's tags: those stay with the caller.
        tags: Labels for this trace, and for every trace started inside the
            block, such as one an adapter opens of its own. Every trace also
            carries ``LAYERLENS_TRACE_TAGS``. ``TypeError`` when not a list of
            strings.

    Yields:
        The shared :class:`TraceCollector`.
    """
    own_tags = tags_argument(tags)
    config = capture_config or CaptureConfig.standard()
    collector = TraceCollector(client, config, tags=own_tags)

    if from_context is not None:
        collector._trace_id = from_context["trace_id"]  # noqa: SLF001

    root_span_id = uuid.uuid4().hex[:16]

    col_token = _current_collector.set(collector)
    span_snapshot = _push_span(root_span_id, "trace_context")
    tag_token = _push_scope(own_tags)
    try:
        yield collector
    finally:
        _pop_scope(tag_token)
        _pop_span(span_snapshot)
        _current_collector.reset(col_token)
        collector.flush()


def get_trace_context() -> Optional[Dict[str, Any]]:
    """Snapshot the current trace context as a plain dict.

    Returns ``None`` when called outside a ``@trace`` / ``trace_context``
    block.  The returned dict is safe to serialise (JSON, headers, message
    queues, etc.) and restore via ``trace_context(client, from_context=ctx)``.

    Keys:

    * ``trace_id`` — 16-char hex trace identifier
    * ``span_id``  — current span (becomes the parent in the remote scope)
    * ``parent_span_id`` — optional grandparent for reference
    * ``version`` — format version for forward compatibility
    """
    collector = _current_collector.get()
    if collector is None:
        return None

    span_id = _current_span_id.get()
    if span_id is None:
        return None

    return {
        "trace_id": collector.trace_id,
        "span_id": span_id,
        "parent_span_id": _parent_span_id.get(),
        "version": 1,
    }
