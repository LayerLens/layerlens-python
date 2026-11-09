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

                    collector.emit(
                        "agent.output",
                        {"name": span_name, "output": result, "status": "ok"},
                        span_id=root_span_id,
                        span_name=span_name,
                    )
                    collector.flush()
                    return result
                except Exception as exc:
                    collector.emit(
                        "agent.error",
                        {"name": span_name, "error": str(exc), **error_category(exc), "status": "error"},
                        span_id=root_span_id,
                        span_name=span_name,
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

                    collector.emit(
                        "agent.output",
                        {"name": span_name, "output": result, "status": "ok"},
                        span_id=root_span_id,
                        span_name=span_name,
                    )
                    collector.flush()
                    return result
                except Exception as exc:
                    collector.emit(
                        "agent.error",
                        {"name": span_name, "error": str(exc), **error_category(exc), "status": "error"},
                        span_id=root_span_id,
                        span_name=span_name,
                    )
                    collector.flush()
                    raise
                finally:
                    _pop_scope(tag_token)
                    _pop_span(span_snapshot)
                    _current_collector.reset(col_token)

            return sync_wrapper

    return decorator


def _capture_input(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
    if args and kwargs:
        return {"args": list(args), "kwargs": kwargs}
    if args:
        arg_list = list(args)
        return arg_list if len(arg_list) > 1 else arg_list[0]
    if kwargs:
        return kwargs
    return None
