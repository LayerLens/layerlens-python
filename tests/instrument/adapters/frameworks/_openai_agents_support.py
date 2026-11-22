"""Build real openai-agents ``SpanImpl`` objects the way each supported release takes them.

The suites build spans by hand so they can drive the adapter's processor hooks
(``on_span_start`` / ``on_span_end``) directly. Later openai-agents releases added
a required ``tracing_api_key`` argument to ``SpanImpl`` (the key the span exports
with; ``None`` means the globally configured one), which 0.4.x doesn't accept.
The SDK always passes it itself, so pass it exactly when the installed release
declares it.
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from agents.tracing.spans import SpanImpl

_TAKES_TRACING_API_KEY = "tracing_api_key" in inspect.signature(SpanImpl.__init__).parameters


def make_span(trace_id: str, span_id: str, span_data: Any, processor: Any, parent_id: Optional[str] = None) -> SpanImpl:
    """A real ``SpanImpl`` reporting to ``processor``."""
    extra = {"tracing_api_key": None} if _TAKES_TRACING_API_KEY else {}
    return SpanImpl(
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent_id,
        processor=processor,
        span_data=span_data,
        **extra,
    )
