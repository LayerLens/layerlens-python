"""Streaming-response timing + accumulation helper.

Extracted from :mod:`_base_provider` per LAY-3329 Claude Code Prompt: the
streaming wrapper logic lives in its own module so both the OpenAI and the
Anthropic adapters (and any future provider with SSE streaming) can share it.

The flow:

1. :func:`stream_chunks_sync` / :func:`stream_chunks_async` are generators
   that re-yield every chunk to the caller (preserving the iterator contract
   downstream consumers expect) while feeding a :class:`StreamingResponseWrapper`
   that times the first chunk and total duration.

2. On exhaustion the generator calls back into the caller's emit hooks with
   ``ttft_ms`` + ``streaming_duration_ms`` + the aggregated response.

3. If the underlying stream raises, the wrapper emits an ``agent.error``
   event with ``partial_meta`` reflecting whatever was accumulated before
   the failure (LAY-3329 / LAY-3332 partial-event ACs).

4. Step 2 never raises into the caller: it runs when the caller's loop asks for
   the chunk after the last one, over a stream that succeeded
   (:func:`emit_streamed_events`).
"""

from __future__ import annotations

import time
import logging
from typing import Any, Dict, Callable, Iterator, Optional, AsyncIterator

from ._emit_helpers import emit_llm_error, emit_llm_events

log: logging.Logger = logging.getLogger(__name__)


class StreamingResponseWrapper:
    """State machine for one streamed LLM invocation.

    Tracks chunk arrival times so the emitter can surface ``ttft_ms`` and
    ``streaming_duration_ms`` on the final ``model.invoke`` event. Holds the
    accumulated chunk list so :func:`_safe_partial_meta` can build a partial
    response if the stream fails mid-iteration.
    """

    __slots__ = ("event_name", "kwargs", "start", "chunks", "first_chunk_at")

    def __init__(self, event_name: str, kwargs: Dict[str, Any], start: float) -> None:
        self.event_name = event_name
        self.kwargs = kwargs
        self.start = start
        self.chunks: list[Any] = []
        self.first_chunk_at: Optional[float] = None

    def record_chunk(self, chunk: Any) -> None:
        if self.first_chunk_at is None:
            self.first_chunk_at = time.time()
        self.chunks.append(chunk)

    @property
    def ttft_ms(self) -> Optional[float]:
        if self.first_chunk_at is None:
            return None
        return (self.first_chunk_at - self.start) * 1000

    def total_duration_ms(self, now: Optional[float] = None) -> float:
        ts = now if now is not None else time.time()
        return (ts - self.start) * 1000


def _safe_partial_meta(
    aggregate: Callable[[list[Any]], Any],
    extract_meta: Callable[[Any], Dict[str, Any]],
    chunks: list[Any],
) -> Optional[Dict[str, Any]]:
    """Best-effort partial-response meta extraction for mid-stream errors.

    Returns ``None`` when there's nothing useful to surface. All exceptions
    in the aggregate / extract path are swallowed — partial meta is observability
    only, never a correctness requirement.
    """
    if not chunks:
        return None
    try:
        partial_response = aggregate(chunks)
        if partial_response is None:
            return None
        meta = extract_meta(partial_response)
        return meta or None
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _no_output(response: Any) -> Any:  # noqa: ARG001
    return None


def _no_meta(response: Any) -> Dict[str, Any]:  # noqa: ARG001
    return {}


def emit_streamed_events(
    *,
    event_name: str,
    kwargs: Dict[str, Any],
    chunks: list[Any],
    aggregate: Callable[[list[Any]], Any],
    extract_output: Callable[[Any], Any],
    extract_meta: Callable[[Any], Dict[str, Any]],
    extract_tool_calls: Callable[[Any], list[dict[str, Any]]],
    capture_params: frozenset[str],
    pricing_table: Optional[dict[str, dict[str, float]]],
    extra_params: Dict[str, Any],
    latency_ms: float,
    ttft_ms: Optional[float],
    provider: Optional[str] = None,
    framework: Optional[str] = None,
    extract_provider_cost: Optional[Callable[[Any], Optional[float]]] = None,
    provider_cost_only: bool = False,
    tool_arguments_json: bool = False,
) -> None:
    """Emit the events of a stream the caller has read to the end. Never raises.

    It runs when the caller's loop asks for the chunk after the last one, so an
    exception here would surface in the caller's own code over a stream that
    succeeded. When the chunks cannot be aggregated into a reply, the call is still
    recorded with what does not depend on the reply (the request, the latency, the
    time to the first chunk) and the failure is logged; a failure while emitting
    is logged too.
    """
    try:
        try:
            response = aggregate(chunks)
        except Exception:  # noqa: BLE001 — must not reach the caller's loop
            log.warning(
                "layerlens: could not aggregate the %s stream; recording the call without its reply",
                event_name,
                exc_info=True,
            )
            emit_llm_events(
                event_name,
                kwargs,
                None,
                _no_output,
                _no_meta,
                capture_params,
                latency_ms,
                pricing_table=pricing_table,
                extra_params=extra_params,
                ttft_ms=ttft_ms,
                streaming_duration_ms=latency_ms,
                provider=provider,
                framework=framework,
            )
            return
        if response is None:
            return
        emit_llm_events(
            event_name,
            kwargs,
            response,
            extract_output,
            extract_meta,
            capture_params,
            latency_ms,
            pricing_table=pricing_table,
            extract_tool_calls=extract_tool_calls,
            extra_params=extra_params,
            ttft_ms=ttft_ms,
            streaming_duration_ms=latency_ms,
            provider=provider,
            framework=framework,
            extract_provider_cost=extract_provider_cost,
            provider_cost_only=provider_cost_only,
            tool_arguments_json=tool_arguments_json,
        )
    except Exception:  # noqa: BLE001 — must not reach the caller's loop
        log.warning("layerlens: could not record the %s stream", event_name, exc_info=True)


def stream_chunks_sync(
    *,
    event_name: str,
    kwargs: Dict[str, Any],
    stream: Iterator[Any],
    start: float,
    aggregate: Callable[[list[Any]], Any],
    extract_output: Callable[[Any], Any],
    extract_meta: Callable[[Any], Dict[str, Any]],
    extract_tool_calls: Callable[[Any], list[dict[str, Any]]],
    capture_params: frozenset[str],
    pricing_table: Optional[dict[str, dict[str, float]]],
    extra_params: Dict[str, Any],
    provider: Optional[str] = None,
    framework: Optional[str] = None,
    extract_provider_cost: Optional[Callable[[Any], Optional[float]]] = None,
    provider_cost_only: bool = False,
    tool_arguments_json: bool = False,
) -> Iterator[Any]:
    """Generator that yields every chunk and emits the consolidated event on close.

    Wraps the underlying SDK iterator without altering its contract — callers
    iterate normally and see identical chunks.
    """
    wrapper = StreamingResponseWrapper(event_name, kwargs, start)

    def generator() -> Iterator[Any]:
        try:
            for chunk in stream:
                wrapper.record_chunk(chunk)
                yield chunk
        except Exception as exc:
            partial_meta = _safe_partial_meta(aggregate, extract_meta, wrapper.chunks)
            emit_llm_error(
                event_name,
                exc,
                wrapper.total_duration_ms(),
                partial_meta=partial_meta,
                partial_chunks=len(wrapper.chunks),
            )
            raise
        emit_streamed_events(
            event_name=event_name,
            kwargs=kwargs,
            chunks=wrapper.chunks,
            aggregate=aggregate,
            extract_output=extract_output,
            extract_meta=extract_meta,
            extract_tool_calls=extract_tool_calls,
            capture_params=capture_params,
            pricing_table=pricing_table,
            extra_params=extra_params,
            latency_ms=wrapper.total_duration_ms(),
            ttft_ms=wrapper.ttft_ms,
            provider=provider,
            framework=framework,
            extract_provider_cost=extract_provider_cost,
            provider_cost_only=provider_cost_only,
            tool_arguments_json=tool_arguments_json,
        )

    return generator()


def stream_chunks_async(
    *,
    event_name: str,
    kwargs: Dict[str, Any],
    stream: AsyncIterator[Any],
    start: float,
    aggregate: Callable[[list[Any]], Any],
    extract_output: Callable[[Any], Any],
    extract_meta: Callable[[Any], Dict[str, Any]],
    extract_tool_calls: Callable[[Any], list[dict[str, Any]]],
    capture_params: frozenset[str],
    pricing_table: Optional[dict[str, dict[str, float]]],
    extra_params: Dict[str, Any],
    provider: Optional[str] = None,
    framework: Optional[str] = None,
    extract_provider_cost: Optional[Callable[[Any], Optional[float]]] = None,
    provider_cost_only: bool = False,
    tool_arguments_json: bool = False,
) -> AsyncIterator[Any]:
    """Async sibling of :func:`stream_chunks_sync`."""
    wrapper = StreamingResponseWrapper(event_name, kwargs, start)

    async def generator() -> AsyncIterator[Any]:
        try:
            async for chunk in stream:
                wrapper.record_chunk(chunk)
                yield chunk
        except Exception as exc:
            partial_meta = _safe_partial_meta(aggregate, extract_meta, wrapper.chunks)
            emit_llm_error(
                event_name,
                exc,
                wrapper.total_duration_ms(),
                partial_meta=partial_meta,
                partial_chunks=len(wrapper.chunks),
            )
            raise
        emit_streamed_events(
            event_name=event_name,
            kwargs=kwargs,
            chunks=wrapper.chunks,
            aggregate=aggregate,
            extract_output=extract_output,
            extract_meta=extract_meta,
            extract_tool_calls=extract_tool_calls,
            capture_params=capture_params,
            pricing_table=pricing_table,
            extra_params=extra_params,
            latency_ms=wrapper.total_duration_ms(),
            ttft_ms=wrapper.ttft_ms,
            provider=provider,
            framework=framework,
            extract_provider_cost=extract_provider_cost,
            provider_cost_only=provider_cost_only,
            tool_arguments_json=tool_arguments_json,
        )

    return generator()
