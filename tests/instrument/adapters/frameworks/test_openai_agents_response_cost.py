"""A run on the OpenAI Agents SDK's default model records what each model call cost.

The Agents SDK calls the Responses API by default, and the adapter recorded a ``response_span`` as
a ``model.invoke`` with its tokens but no ``cost.record``, where a ``generation_span`` (the Chat
Completions model) got one beside each call; the traces list showed no cost for those runs (docs
E2E 2026-11-10, L3 AD-32, L7 AD-10). A response span with token usage now records its cost too.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._optional_deps import importorskip

if sys.version_info < (3, 10):
    pytest.skip("openai-agents requires Python >= 3.10", allow_module_level=True)
importorskip("agents")

from agents.tracing import TracingProcessor, set_trace_processors  # noqa: E402
from agents.tracing.traces import TraceImpl  # noqa: E402
from agents.tracing.span_data import ResponseSpanData  # noqa: E402

from openai.types.responses import ResponseUsage  # noqa: E402
from layerlens.instrument._capture_config import CaptureConfig  # noqa: E402
from layerlens.instrument.adapters.frameworks.openai_agents import OpenAIAgentsAdapter  # noqa: E402

from .conftest import find_event, find_events, capture_framework_trace  # noqa: E402
from ._openai_agents_support import make_span  # noqa: E402


class _NoOp(TracingProcessor):
    def on_trace_start(self, trace: Any) -> None:
        pass

    def on_trace_end(self, trace: Any) -> None:
        pass

    def on_span_start(self, span: Any) -> None:
        pass

    def on_span_end(self, span: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_processors() -> Any:
    yield
    set_trace_processors([])


def _usage() -> ResponseUsage:
    # As the API sends it; openai releases differ in which detail fields they require.
    return ResponseUsage.model_validate(
        {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 150,
        }
    )


def _record_one_response(mock_client: Any, usage: Any, *, error: Any = None) -> Any:
    uploaded = capture_framework_trace(mock_client)
    adapter = OpenAIAgentsAdapter(mock_client, capture_config=CaptureConfig(capture_content=False))
    adapter.connect()
    try:
        trace = TraceImpl(name="run", trace_id="trace_resp_cost", group_id=None, metadata=None, processor=_NoOp())
        adapter.on_trace_start(trace)
        response = MagicMock()
        response.id, response.model, response.usage = "resp_1", "gpt-4o-mini-2024-07-18", usage
        span = make_span("trace_resp_cost", "span_resp_1", ResponseSpanData(response=response), _NoOp())
        span.start()
        if error is not None:
            span.set_error(error)
        span.finish()
        adapter.on_span_end(span)
        adapter.on_trace_end(trace)
    finally:
        adapter.disconnect()
    return uploaded["events"]


def test_a_response_with_usage_records_its_cost(mock_client: Any) -> None:
    events = _record_one_response(mock_client, _usage())
    invoke = find_event(events, "model.invoke")
    costs = find_events(events, "cost.record")
    assert len(costs) == 1
    cost = costs[0]
    assert cost["payload"]["model"] == "gpt-4o-mini-2024-07-18"
    assert (cost["payload"]["tokens_prompt"], cost["payload"]["tokens_completion"]) == (120, 30)
    assert cost["payload"]["cost_usd"] == pytest.approx(120 * 0.15e-6 + 30 * 0.6e-6)
    assert cost["span_id"] == invoke["span_id"]
    # A response that reports no usage has nothing to price.
    assert not find_events(_record_one_response(mock_client, None), "cost.record")


def test_a_failed_response_with_usage_still_records_its_cost(mock_client: Any) -> None:
    events = _record_one_response(mock_client, _usage(), error={"message": "rate limited", "data": None})
    assert find_events(events, "agent.error")
    assert len(find_events(events, "cost.record")) == 1
