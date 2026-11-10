from __future__ import annotations

import uuid
import asyncio
import functools
from typing import Any, Dict, Tuple, Callable, Iterable, Optional

from ._tags import _pop_scope, _push_scope, tags_argument
from ._context import _pop_span, _push_span, _current_collector
from ._collector import TraceCollector
from ._error_code import error_category
from ._capture_config import CaptureConfig


def trace(
    client: Any,
    *,
    name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    capture_config: Optional[CaptureConfig] = None,
    tags: Optional[Iterable[str]] = None,
) -> Callable[..., Any]:
    """Record each call of the decorated function as one trace, uploaded through ``client``.

    ``tags`` label each of those traces, and the traces started during the call, such as one
    an adapter opens of its own. Every trace also carries ``LAYERLENS_TRACE_TAGS``. Raises
    ``TypeError`` here, before anything runs, when ``tags`` is not a list of strings.
    """
    own_tags = tags_argument(tags)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        span_name = name or fn.__name__

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                config = capture_config or CaptureConfig.standard()
                collector = TraceCollector(client, config, tags=own_tags)
                root_span_id = uuid.uuid4().hex[:16]

                col_token = _current_collector.set(collector)
                span_snapshot = _push_span(root_span_id, span_name)
                tag_token = _push_scope(own_tags)
                try:
                    collector.emit(
                        "agent.input",
                        {
                            "name": span_name,
                            "input": _capture_input(args, kwargs),
                            **(metadata or {}),
                        },
                        span_id=root_span_id,
                        span_name=span_name,
                    )

                    result = await fn(*args, **kwargs)

                    ending = _ending_recorder(collector, span_name, root_span_id)
                    # A stream the function opened and returns: its output is what it replies, once
                    # the caller has read it, or the error it fails with (S2). Anything else is
                    # recorded as returned.
                    if not collector._when_stream_ends(result, ending):  # noqa: SLF001
                        ending(result, None)
                    # Uploaded now, or once a stream the function opened and returned is read (S2).
                    collector._finish()  # noqa: SLF001
                    return result
                except Exception as exc:
                    collector.emit(
                        "agent.error", _error_payload(span_name, exc), span_id=root_span_id, span_name=span_name
                    )
                    collector.flush()
                    raise
                finally:
                    _pop_scope(tag_token)
                    _pop_span(span_snapshot)
                    _current_collector.reset(col_token)

            return async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                config = capture_config or CaptureConfig.standard()
                collector = TraceCollector(client, config, tags=own_tags)
                root_span_id = uuid.uuid4().hex[:16]

                col_token = _current_collector.set(collector)
                span_snapshot = _push_span(root_span_id, span_name)
                tag_token = _push_scope(own_tags)
                try:
                    collector.emit(
                        "agent.input",
                        {
                            "name": span_name,
                            "input": _capture_input(args, kwargs),
                            **(metadata or {}),
                        },
                        span_id=root_span_id,
                        span_name=span_name,
                    )

                    result = fn(*args, **kwargs)

                    ending = _ending_recorder(collector, span_name, root_span_id)
                    # A stream the function opened and returns: its output is what it replies, once
                    # the caller has read it, or the error it fails with (S2). Anything else is
                    # recorded as returned.
                    if not collector._when_stream_ends(result, ending):  # noqa: SLF001
                        ending(result, None)
                    # Uploaded now, or once a stream the function opened and returned is read (S2).
                    collector._finish()  # noqa: SLF001
                    return result
                except Exception as exc:
                    collector.emit(
                        "agent.error", _error_payload(span_name, exc), span_id=root_span_id, span_name=span_name
                    )
                    collector.flush()
                    raise
                finally:
                    _pop_scope(tag_token)
                    _pop_span(span_snapshot)
                    _current_collector.reset(col_token)

            return sync_wrapper

    return decorator


def _ending_recorder(
    collector: TraceCollector, span_name: str, root_span_id: str
) -> Callable[[Any, Optional[BaseException]], None]:
    """Records how the function's call ended: ``agent.output`` with the output it is given, or,
    given an error, ``agent.error``, as when the function raises.

    The error is a returned stream's: the function returned, and the stream failed as its caller
    read it. That call failed, so its trace ends with the error alone, not also with an
    ``agent.output`` saying ``ok``.
    """

    def record(output: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            collector.emit("agent.error", _error_payload(span_name, error), span_id=root_span_id, span_name=span_name)
            return
        collector.emit(
            "agent.output",
            {"name": span_name, "output": output, "status": "ok"},
            span_id=root_span_id,
            span_name=span_name,
        )

    return record


def _error_payload(span_name: str, error: BaseException) -> Dict[str, Any]:
    """The root span's ``agent.error``: what failed and why (see :func:`error_category`)."""
    return {"name": span_name, "error": str(error), **error_category(error), "status": "error"}


def _capture_input(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
    if args and kwargs:
        return {"args": list(args), "kwargs": kwargs}
    if args:
        arg_list = list(args)
        return arg_list if len(arg_list) > 1 else arg_list[0]
    if kwargs:
        return kwargs
    return None
