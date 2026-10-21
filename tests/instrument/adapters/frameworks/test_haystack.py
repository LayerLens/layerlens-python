"""Tests for the Haystack adapter.

Mocks ``haystack.tracing`` since haystack-ai is not installed in the test env.
Drives the tracer with exact operation names and tags that Haystack uses.
"""

from __future__ import annotations

import threading
import contextvars
from typing import Any, Dict, Optional
from unittest.mock import Mock, MagicMock
from concurrent.futures import ThreadPoolExecutor

import pytest

import layerlens.instrument.adapters.frameworks.haystack as _mod
from layerlens.instrument._context import _current_run
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.haystack import (
    HaystackAdapter,
    _NullSpan,
    _extract_model,
    _extract_usage,
    _LayerLensTracer,
)

from .conftest import (
    find_event,
    find_events,
    model_calls,
    assert_one_closed_tree,
    capture_framework_trace,
    assert_priced_once_per_call,
)


@pytest.fixture(autouse=True)
def mock_haystack_tracing():
    prev_tracing = _mod._hs_tracing
    prev_has = _mod._HAS_HAYSTACK
    mock_tracing = MagicMock()
    _mod._hs_tracing = mock_tracing
    _mod._HAS_HAYSTACK = True
    yield mock_tracing
    # Restore the real values rather than hard-resetting to False, so this file
    # does not poison module globals for other adapter tests in the same process.
    _mod._hs_tracing = prev_tracing
    _mod._HAS_HAYSTACK = prev_has


def _make_adapter(client: Any, config: Optional[CaptureConfig] = None) -> HaystackAdapter:
    adapter = HaystackAdapter(client, capture_config=config)
    adapter.connect()
    return adapter


def _simulate_pipeline(
    tracer: _LayerLensTracer,
    *,
    input_data: Any = None,
    output_data: Any = None,
    components: Optional[list] = None,
    error: Optional[str] = None,
    max_runs: Optional[int] = None,
) -> None:
    with tracer.trace("haystack.pipeline.run") as pipe:
        if input_data is not None:
            pipe.set_content_tag("haystack.pipeline.input_data", input_data)
        if max_runs is not None:
            pipe.set_tag("haystack.pipeline.max_runs_per_component", max_runs)

        for comp in components or []:
            with tracer.trace("haystack.component.run") as cs:
                cs.set_tag("haystack.component.name", comp["name"])
                cs.set_tag("haystack.component.type", comp["type"])
                if comp.get("model"):
                    cs.set_tag("haystack.model", comp["model"])
                if comp.get("input") is not None:
                    cs.set_content_tag("haystack.component.input", comp["input"])
                if comp.get("output") is not None:
                    cs.set_content_tag("haystack.component.output", comp["output"])
                if comp.get("error"):
                    cs.set_tag("error", True)
                    cs.set_tag("error.message", comp["error"])

        if output_data is not None:
            pipe.set_content_tag("haystack.pipeline.output_data", output_data)
        if error:
            pipe.set_tag("error", True)
            pipe.set_tag("error.message", error)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_disconnect(self, mock_client):
        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        assert adapter.is_connected
        adapter.disconnect()
        assert not adapter.is_connected

    def test_installs_and_restores_tracer(self, mock_client, mock_haystack_tracing):
        original = Mock(spec=[])
        mock_haystack_tracing.tracer.actual_tracer = original
        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        assert isinstance(mock_haystack_tracing.tracer.actual_tracer, _LayerLensTracer)
        adapter.disconnect()
        assert mock_haystack_tracing.tracer.actual_tracer is original

    def test_raises_when_haystack_missing(self, mock_client):
        _mod._HAS_HAYSTACK = False
        with pytest.raises(ImportError, match="haystack"):
            HaystackAdapter(mock_client).connect()

    def test_adapter_info(self, mock_client):
        adapter = HaystackAdapter(mock_client)
        assert adapter.adapter_info().name == "haystack"
        assert not adapter.adapter_info().connected
        adapter.connect()
        assert adapter.adapter_info().connected
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Pipeline spans
# ---------------------------------------------------------------------------


class TestPipelineSpans:
    def test_input_and_output(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _simulate_pipeline(adapter._tracer, input_data={"q": "hello"}, output_data={"a": "world"})

        inp = find_event(uploaded["events"], "agent.input")
        assert inp["payload"]["framework"] == "haystack"
        assert inp["payload"]["input"] == {"q": "hello"}

        out = find_event(uploaded["events"], "agent.output")
        assert out["payload"]["output"] == {"a": "world"}
        assert out["payload"]["latency_ms"] > 0
        adapter.disconnect()

    def test_content_gating(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=False))
        _simulate_pipeline(adapter._tracer, input_data="secret", output_data="classified")

        assert "input" not in find_event(uploaded["events"], "agent.input")["payload"]
        assert "output" not in find_event(uploaded["events"], "agent.output")["payload"]
        adapter.disconnect()

    def test_error(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _simulate_pipeline(adapter._tracer, error="Pipeline failed")
        assert find_event(uploaded["events"], "agent.output")["payload"]["error"] == "Pipeline failed"
        adapter.disconnect()

    def test_exception(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        with pytest.raises(ValueError):
            with adapter._tracer.trace("haystack.pipeline.run"):
                raise ValueError("boom")
        assert find_event(uploaded["events"], "agent.output")["payload"]["error"] == "boom"
        adapter.disconnect()

    def test_max_runs_tag(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(adapter._tracer, max_runs=100)
        assert find_event(uploaded["events"], "agent.input")["payload"]["max_runs_per_component"] == 100
        adapter.disconnect()

    def test_flushes_trace(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(adapter._tracer)
        assert uploaded.get("trace_id") is not None
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Generator components
# ---------------------------------------------------------------------------


class _ChatReply:
    """Stands in for ``haystack.dataclasses.ChatMessage``, which a chat generator
    puts in ``replies`` (haystack-ai is not installed in this venv). It exposes the
    two members the adapter touches: the ``meta`` dict and the ``to_dict()`` form
    the output is captured in. ``test_haystack_recorded.py`` drives a real one."""

    def __init__(self, text: str, meta: dict) -> None:
        self.text = text
        self.meta = meta

    def to_dict(self) -> dict:
        return {"role": "assistant", "meta": self.meta, "name": None, "content": [{"text": self.text}]}


#: ``ChatMessage.meta`` of a real ``OpenAIChatGenerator`` reply to the recorded
#: OpenAI body (tests/fixtures/recorded/openai/default.json), identical on
#: haystack-ai 2.30.1 and 3.4.0.
_REAL_CHAT_REPLY_META = {
    "model": "gpt-4o-mini-2024-07-18",
    "index": 0,
    "finish_reason": "stop",
    "usage": {
        "completion_tokens": 1,
        "prompt_tokens": 12,
        "total_tokens": 13,
        "completion_tokens_details": {
            "accepted_prediction_tokens": 0,
            "audio_tokens": 0,
            "reasoning_tokens": 0,
            "rejected_prediction_tokens": 0,
            "text_tokens": None,
        },
        "prompt_tokens_details": {
            "audio_tokens": 0,
            "cache_write_tokens": None,
            "cached_tokens": 0,
            "image_tokens": None,
            "text_tokens": None,
        },
    },
}


class TestGeneratorComponents:
    def _gen_component(self, **overrides: Any) -> dict:
        base = {
            "name": "llm",
            "type": "OpenAIChatGenerator",
            "model": "gpt-4o",
            "output": {
                "replies": ["answer"],
                "meta": [
                    {
                        "model": "gpt-4o",
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                    }
                ],
            },
        }
        base.update(overrides)
        return base

    def test_model_invoke_with_tokens(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(adapter._tracer, components=[self._gen_component()])

        invoke = find_event(uploaded["events"], "model.invoke")
        assert invoke["payload"]["model"] == "gpt-4o"
        assert invoke["payload"]["tokens_prompt"] == 100
        assert invoke["payload"]["tokens_completion"] == 50
        assert invoke["payload"]["tokens_total"] == 150
        assert invoke["span_name"] == "component:llm"
        adapter.disconnect()

    def test_model_invoke_emits_finish_reason(self, mock_client):
        """S18/F11: surface the generator's own finish_reason, never fabricated."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        component = self._gen_component()
        component["output"]["meta"][0]["finish_reason"] = "stop"
        _simulate_pipeline(adapter._tracer, components=[component])

        invoke = find_event(uploaded["events"], "model.invoke")
        assert invoke["payload"]["finish_reason"] == "stop"
        adapter.disconnect()

    def test_model_invoke_no_finish_reason_when_absent(self, mock_client):
        """No finish_reason in meta must stay honestly blank, not fabricated."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(adapter._tracer, components=[self._gen_component()])

        invoke = find_event(uploaded["events"], "model.invoke")
        assert "finish_reason" not in invoke["payload"]
        adapter.disconnect()

    def test_cost_record(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(adapter._tracer, components=[self._gen_component()])

        cost = find_event(uploaded["events"], "cost.record")
        assert cost["payload"]["tokens_total"] == 150
        assert cost["payload"]["model"] == "gpt-4o"
        # Parented to model.invoke span
        assert cost["parent_span_id"] == find_event(uploaded["events"], "model.invoke")["span_id"]
        adapter.disconnect()

    def test_chatgenerator_classified(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {
                    "name": "c",
                    "type": "haystack.components.generators.chat.openai.OpenAIChatGenerator",
                },
            ],
        )
        assert len(find_events(uploaded["events"], "model.invoke")) == 1
        adapter.disconnect()

    def test_content_gating(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=False))
        _simulate_pipeline(adapter._tracer, components=[self._gen_component()])

        invoke = find_event(uploaded["events"], "model.invoke")
        assert "input" not in invoke["payload"]
        assert "output" not in invoke["payload"]
        adapter.disconnect()

    def test_chat_generator_reply_meta(self, mock_client):
        """A chat generator (``OpenAIChatGenerator``, the only OpenAI generator
        haystack 3 has) returns ``{"replies": [ChatMessage]}`` with no top-level
        ``meta`` list: model, usage and finish_reason sit on each reply's ``meta``.
        No ``haystack.model`` tag is set either, so all of them must come from
        there, onto model.invoke and cost.record."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {
                    "name": "llm",
                    "type": "OpenAIChatGenerator",
                    "output": {"replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)]},
                }
            ],
        )

        invoke = find_event(uploaded["events"], "model.invoke")
        assert invoke["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert invoke["payload"]["tokens_prompt"] == 12
        assert invoke["payload"]["tokens_completion"] == 1
        assert invoke["payload"]["tokens_total"] == 13
        assert invoke["payload"]["finish_reason"] == "stop"

        cost = find_event(uploaded["events"], "cost.record")
        assert cost["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert cost["payload"]["tokens_prompt"] == 12
        assert cost["payload"]["tokens_completion"] == 1
        assert cost["payload"]["tokens_total"] == 13
        adapter.disconnect()

    def test_model_from_output_meta(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {
                    "name": "llm",
                    "type": "ChatGenerator",
                    "output": {
                        "replies": ["ok"],
                        "meta": [
                            {
                                "model": "claude-3",
                                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                            }
                        ],
                    },
                }
            ],
        )
        assert find_event(uploaded["events"], "model.invoke")["payload"]["model"] == "claude-3"
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Non-generator components
# ---------------------------------------------------------------------------


class TestToolComponents:
    def test_tool_call_and_result(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {
                    "name": "my_retriever",
                    "type": "BM25Retriever",
                    "input": {"q": "find"},
                    "output": {"docs": ["d1"]},
                },
            ],
        )

        call = find_event(uploaded["events"], "tool.call")
        assert call["payload"]["tool_name"] == "my_retriever"
        assert call["payload"]["component_type"] == "BM25Retriever"
        assert call["payload"]["input"] == {"q": "find"}

        result = find_event(uploaded["events"], "tool.result")
        assert result["payload"]["output"] == {"docs": ["d1"]}
        assert result["payload"]["latency_ms"] > 0
        adapter.disconnect()

    def test_content_gating(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=False))
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {
                    "name": "r",
                    "type": "Retriever",
                    "input": "secret",
                    "output": "classified",
                },
            ],
        )
        assert "input" not in find_event(uploaded["events"], "tool.call")["payload"]
        assert "output" not in find_event(uploaded["events"], "tool.result")["payload"]
        adapter.disconnect()

    def test_component_error(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {"name": "broken", "type": "Custom", "error": "crashed"},
            ],
        )
        assert find_event(uploaded["events"], "tool.result")["payload"]["error"] == "crashed"
        adapter.disconnect()

    def test_prompt_builder_is_tool(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {
                    "name": "pb",
                    "type": "PromptBuilder",
                    "input": {"tpl": "hi"},
                    "output": {"prompt": "hi"},
                },
            ],
        )
        assert len(find_events(uploaded["events"], "tool.call")) == 1
        assert len([e for e in uploaded["events"] if e["event_type"] == "agent.code"]) == 0
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Full pipeline with multiple components
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_rag_pipeline(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            input_data={"query": "test"},
            output_data={"answer": "result"},
            components=[
                {"name": "retriever", "type": "BM25Retriever"},
                {"name": "prompt_builder", "type": "PromptBuilder"},
                {
                    "name": "llm",
                    "type": "OpenAIChatGenerator",
                    "model": "gpt-4o",
                    "output": {
                        "replies": ["answer"],
                        "meta": [{"usage": {"prompt_tokens": 20, "completion_tokens": 10}}],
                    },
                },
            ],
        )
        events = uploaded["events"]
        assert len(find_events(events, "agent.input")) == 1
        assert len(find_events(events, "agent.output")) == 1
        assert len(find_events(events, "tool.call")) == 2
        assert len(find_events(events, "model.invoke")) == 1
        assert len(find_events(events, "cost.record")) == 1
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Trace integrity
# ---------------------------------------------------------------------------


class TestTraceIntegrity:
    def test_shared_trace_id(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {"name": "r", "type": "Retriever"},
                {
                    "name": "g",
                    "type": "ChatGenerator",
                    "output": {
                        "replies": ["ok"],
                        "meta": [{"usage": {"prompt_tokens": 1, "completion_tokens": 1}}],
                    },
                },
            ],
        )
        assert len({e["trace_id"] for e in uploaded["events"]}) == 1
        adapter.disconnect()

    def test_monotonic_sequence_ids(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(adapter._tracer, components=[{"name": "c", "type": "T"}])
        seq = [e["sequence_id"] for e in uploaded["events"]]
        assert seq == sorted(seq)
        adapter.disconnect()

    def test_span_hierarchy(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {"name": "ret", "type": "Retriever"},
                {"name": "gen", "type": "ChatGenerator"},
            ],
        )
        events = uploaded["events"]
        root = find_event(events, "agent.input")["span_id"]
        assert find_event(events, "tool.call")["parent_span_id"] == root
        assert find_event(events, "model.invoke")["parent_span_id"] == root
        adapter.disconnect()

    def test_internal_spans_skipped(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        with adapter._tracer.trace("haystack.pipeline.run"):
            with adapter._tracer.trace("haystack.internal.something") as s:
                s.set_tag("x", "y")
        assert len(find_events(uploaded["events"], "tool.call")) == 0
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Tracer protocol
# ---------------------------------------------------------------------------


class TestTracerProtocol:
    def test_current_span(self, mock_client):
        adapter = _make_adapter(mock_client)
        assert isinstance(adapter._tracer.current_span(), _NullSpan)
        with adapter._tracer.trace("haystack.pipeline.run") as span:
            assert adapter._tracer.current_span() is span
        adapter.disconnect()

    def test_nested_parent_tracking(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        with adapter._tracer.trace("haystack.pipeline.run") as pipe:
            with adapter._tracer.trace("haystack.component.run") as comp:
                comp.set_tag("haystack.component.type", "Retriever")
                comp.set_tag("haystack.component.name", "r")
                assert comp._parent_span_id == pipe.span_id
        assert find_event(uploaded["events"], "tool.call")["parent_span_id"] == pipe.span_id
        adapter.disconnect()

    def test_span_protocol_methods(self, mock_client):
        adapter = _make_adapter(mock_client)
        with adapter._tracer.trace("haystack.pipeline.run") as span:
            assert span.raw_span() is None
            data = span.get_correlation_data_for_logs()
            assert data["span_id"] == span.span_id
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_pipelines(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        errors = []

        def _run(tid: int) -> None:
            try:
                _simulate_pipeline(
                    adapter._tracer,
                    input_data={"t": tid},
                    output_data={"r": tid},
                    components=[{"name": f"c_{tid}", "type": "T"}],
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        adapter.disconnect()

        assert len(find_events(uploaded["events"], "agent.input")) == 5
        assert len(find_events(uploaded["events"], "agent.output")) == 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_model_from_tag(self):
        assert _extract_model({"haystack.model": "gpt-4o"}) == "gpt-4o"

    def test_extract_model_from_meta(self):
        assert _extract_model({"haystack.component.output": {"meta": [{"model": "claude-3"}]}}) == "claude-3"

    def test_extract_model_none(self):
        assert _extract_model({}) is None

    def test_extract_usage(self):
        assert _extract_usage({"meta": [{"usage": {"prompt_tokens": 10}}]}) == {"prompt_tokens": 10}

    def test_extract_usage_none(self):
        assert _extract_usage({}) is None

    def test_nullspan_noop(self):
        ns = _NullSpan()
        ns.set_tag("k", "v")
        ns.set_content_tag("k", "v")
        assert ns.raw_span() is None
        assert ns.get_correlation_data_for_logs() == {}


# ---------------------------------------------------------------------------
# Disconnect leave-no-trace (LAY-3577 / T3)
# ---------------------------------------------------------------------------


class TestDisconnectLeaveNoTrace:
    def test_user_tracer_restored_identically(self, mock_client, mock_haystack_tracing):
        """The tracer installed before connect() must come back as the exact object."""
        user_tracer = Mock(spec=[])
        mock_haystack_tracing.tracer.actual_tracer = user_tracer

        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        assert isinstance(mock_haystack_tracing.tracer.actual_tracer, _LayerLensTracer)

        adapter.disconnect()
        assert mock_haystack_tracing.tracer.actual_tracer is user_tracer

    def test_disconnect_clears_adapter_hooks(self, mock_client, mock_haystack_tracing):
        original = Mock(spec=[])
        mock_haystack_tracing.tracer.actual_tracer = original

        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        adapter.disconnect()

        assert adapter._tracer is None
        assert adapter._original_tracer is None
        assert not isinstance(mock_haystack_tracing.tracer.actual_tracer, _LayerLensTracer)

    def test_double_disconnect_does_not_raise(self, mock_client, mock_haystack_tracing):
        user_tracer = Mock(spec=[])
        mock_haystack_tracing.tracer.actual_tracer = user_tracer

        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        adapter.disconnect()
        adapter.disconnect()

        assert not adapter.is_connected
        # Second disconnect must not clobber the restored tracer
        assert mock_haystack_tracing.tracer.actual_tracer is user_tracer

    def test_connect_disconnect_cycle(self, mock_client, mock_haystack_tracing):
        user_tracer = Mock(spec=[])
        mock_haystack_tracing.tracer.actual_tracer = user_tracer
        uploaded = capture_framework_trace(mock_client)

        adapter = HaystackAdapter(mock_client)

        adapter.connect()
        _simulate_pipeline(adapter._tracer, input_data={"q": "one"})
        adapter.disconnect()
        assert mock_haystack_tracing.tracer.actual_tracer is user_tracer

        adapter.connect()
        _simulate_pipeline(adapter._tracer, input_data={"q": "two"})
        adapter.disconnect()
        assert mock_haystack_tracing.tracer.actual_tracer is user_tracer

        # Both connected periods produced full traces
        assert len(find_events(uploaded["events"], "agent.input")) == 2


# ---------------------------------------------------------------------------
# Honest graph contract (Lever A) — the server graph engine builds a haystack
# node from the producer-declared ``payload.component_name``. Each named
# component must carry it (+ ``component_class``) so a >1-component pipeline
# renders >1 node, and an unnamed/generic component must stay BLANK (never a
# fabricated node identity).
# ---------------------------------------------------------------------------


class TestHonestGraphContract:
    def test_named_components_carry_honest_component_name(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            input_data={"query": "q"},
            output_data={"answer": "a"},
            components=[
                {"name": "retriever", "type": "BM25Retriever"},
                {"name": "prompt_builder", "type": "PromptBuilder"},
                {
                    "name": "llm",
                    "type": "OpenAIChatGenerator",
                    "model": "gpt-4o",
                    "output": {
                        "replies": ["a"],
                        "meta": [{"model": "gpt-4o", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}],
                    },
                },
            ],
        )
        events = uploaded["events"]

        # The generator's model.invoke carries the declared node id + class.
        invoke = find_event(events, "model.invoke")
        assert invoke["payload"]["component_name"] == "llm"
        assert invoke["payload"]["component_class"] == "OpenAIChatGenerator"

        # Non-generator components carry their declared names on tool.call too.
        calls = find_events(events, "tool.call")
        assert {c["payload"]["component_name"] for c in calls} == {"retriever", "prompt_builder"}
        for c in calls:
            assert c["payload"]["component_class"]

        # A pipeline of >1 named component yields >1 distinct honest node.
        node_names = {
            e["payload"]["component_name"]
            for e in events
            if isinstance(e.get("payload"), dict) and "component_name" in e["payload"]
        }
        assert node_names == {"retriever", "prompt_builder", "llm"}
        adapter.disconnect()

    def test_tool_result_also_carries_component_name(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[{"name": "retriever", "type": "BM25Retriever"}],
        )
        result = find_event(uploaded["events"], "tool.result")
        assert result["payload"]["component_name"] == "retriever"
        adapter.disconnect()

    def test_environment_config_lists_pipeline_components(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _simulate_pipeline(
            adapter._tracer,
            components=[
                {"name": "retriever", "type": "BM25Retriever"},
                {
                    "name": "llm",
                    "type": "ChatGenerator",
                    "output": {
                        "replies": ["ok"],
                        "meta": [{"model": "gpt-4o", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}],
                    },
                },
            ],
        )
        cfg = find_event(uploaded["events"], "environment.config")
        comps = cfg["payload"]["components"]
        names = {c["name"] if isinstance(c, dict) else c for c in comps}
        assert {"retriever", "llm"} <= names
        adapter.disconnect()

    def test_unnamed_component_stays_blank(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        # No haystack.component.name tag -> the adapter defaults it to "unknown";
        # a generic placeholder must NEVER surface as a fabricated node identity.
        with adapter._tracer.trace("haystack.pipeline.run"):
            with adapter._tracer.trace("haystack.component.run") as cs:
                cs.set_tag("haystack.component.type", "BM25Retriever")
        call = find_event(uploaded["events"], "tool.call")
        assert "component_name" not in call["payload"]
        # And no fabricated node leaks into environment.config either.
        cfgs = find_events(uploaded["events"], "environment.config")
        for cfg in cfgs:
            for c in cfg["payload"].get("components", []):
                name = c["name"] if isinstance(c, dict) else c
                assert name != "unknown"
        adapter.disconnect()


# ---------------------------------------------------------------------------
# haystack 3 Agent spans and the shared chat-generator span
#
# The tag shapes below are the ones real haystack-ai 3.4.0 runs hand the tracer
# (recorded with a tracer that dumps every span: an ``Agent`` over the recorded
# OpenAI tool-call and answer bodies, standalone and inside a ``Pipeline``; an
# ``LLMMessagesRouter``, a ``QueryExpander``, a ``FallbackChatGenerator`` and an
# ``LLMMetadataExtractor`` in a ``Pipeline``):
#
# * ``haystack.agent.run``: tags ``haystack.agent.max_steps`` / ``.tools`` (the
#   Tool objects) / ``.exit_conditions`` / ``.state_schema``, content tags
#   ``haystack.agent.input`` / ``.output``, tag ``haystack.agent.steps_taken``.
#   Its parent is the current span: the Agent's own ``haystack.component.run``
#   span inside a pipeline, none when it runs on its own.
# * ``haystack.agent.step`` (tag ``haystack.agent.step``: 0, 1, ...) with two
#   kinds of child: ``haystack.agent.step.llm`` (content tags ``...llm.input`` /
#   ``...llm.output`` = ``{"replies": [ChatMessage]}``, no other tag, so no
#   generator class) and ``haystack.agent.step.tool`` (tags ``haystack.tool.name``
#   / ``.description``, content tags ``...tool.input`` = the call arguments and
#   ``...tool.output``; ``haystack.tool.error`` when the tool failed). The tool
#   span runs on a worker thread under a copied context, with an explicit parent.
# * ``haystack.chat_generator.run``: tags ``haystack.component.name`` (always the
#   literal ``"chat_generator"``) and ``haystack.component.type`` (the generator
#   class), content tags ``haystack.component.input`` / ``.output``. The output
#   is set only by components that do not return the replies themselves
#   (routers, rankers, extractors, evaluators, expanders);
#   ``FallbackChatGenerator`` leaves it unset because its own component span
#   carries the replies. ``LLMMetadataExtractor`` and
#   ``LLMDocumentContentExtractor`` open it on worker threads that do NOT copy
#   the caller's context, passing the parent span explicitly.
#
# haystack-ai 2.30.1 opens only ``haystack.agent.run`` of these: its Agent runs
# the chat generator and the ToolInvoker as ``haystack.component.run`` spans that
# haystack itself names "chat_generator" and "tool_invoker".
# ---------------------------------------------------------------------------

#: ``ChatMessage.meta`` of a real ``OpenAIChatGenerator`` reply to the recorded
#: tool-call body (tests/fixtures/recorded/openai/tool_call.json), haystack-ai 3.4.0.
_REAL_TOOL_CALL_REPLY_META = {
    "model": "gpt-4o-mini-2024-07-18",
    "index": 0,
    "finish_reason": "tool_calls",
    "usage": {
        "completion_tokens": 14,
        "prompt_tokens": 54,
        "total_tokens": 68,
        "completion_tokens_details": {
            "accepted_prediction_tokens": 0,
            "audio_tokens": 0,
            "reasoning_tokens": 0,
            "rejected_prediction_tokens": 0,
            "text_tokens": None,
        },
        "prompt_tokens_details": {
            "audio_tokens": 0,
            "cache_write_tokens": None,
            "cached_tokens": 0,
            "image_tokens": None,
            "text_tokens": None,
        },
    },
}

_USER_MESSAGE = {"role": "user", "content": [{"text": "What's the weather in Paris?"}]}


class _Tool:
    """Stands in for ``haystack.tools.Tool`` in the ``haystack.agent.tools`` tag
    and the step's LLM input; the adapter reads only ``name``."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


_GET_WEATHER = _Tool("get_weather", "Get the current weather for a city.")


def _agent_tags() -> Dict[str, Any]:
    return {
        "haystack.agent.max_steps": 100,
        "haystack.agent.tools": [_GET_WEATHER],
        "haystack.agent.exit_conditions": ["text"],
        "haystack.agent.state_schema": {
            "messages": {
                "type": "list[haystack.dataclasses.chat_message.ChatMessage]",
                "handler": "haystack.components.agents.state.state_utils.merge_lists",
            }
        },
    }


def _chat_generator_tags(generator: str = "OpenAIChatGenerator") -> Dict[str, Any]:
    return {"haystack.component.name": "chat_generator", "haystack.component.type": generator}


def _run_tool_like_haystack(tracer: _LayerLensTracer, step: Any, *, error: Optional[str] = None) -> None:
    """``haystack.agent.step.tool`` as haystack 3 opens it: on a thread-pool worker,
    under ``contextvars.copy_context()``, with the step span as explicit parent."""
    ctx = contextvars.copy_context()

    def _invoke() -> None:
        tags = {"haystack.tool.name": _GET_WEATHER.name, "haystack.tool.description": _GET_WEATHER.description}
        with tracer.trace("haystack.agent.step.tool", tags=tags, parent_span=step) as span:
            span.set_content_tag("haystack.agent.step.tool.input", {"city": "Paris"})
            if error is None:
                span.set_content_tag("haystack.agent.step.tool.output", "sunny in Paris")
            else:
                span.set_tag("haystack.tool.error", True)
                span.set_content_tag("haystack.agent.step.tool.output", {"error": error})

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(ctx.run, _invoke).result()


def _run_agent_v3(
    tracer: _LayerLensTracer,
    *,
    fallback_inner_span: bool = False,
    fallback_failed_attempt: bool = False,
    tool_error: Optional[str] = None,
) -> None:
    """One haystack 3 ``Agent.run``: a step whose reply calls ``get_weather``, then
    a step whose reply answers. ``fallback_inner_span`` gives each LLM step the
    output-less ``haystack.chat_generator.run`` child a ``FallbackChatGenerator``
    chat generator opens; ``fallback_failed_attempt`` puts a failed attempt
    (another such span, which raised) before it."""
    replies = [
        _ChatReply("", _REAL_TOOL_CALL_REPLY_META),
        _ChatReply("pong", _REAL_CHAT_REPLY_META),
    ]
    # Agent._create_agent_span passes tracing.tracer.current_span() as the parent.
    with tracer.trace("haystack.agent.run", tags=_agent_tags(), parent_span=tracer.current_span()) as agent:
        agent.set_content_tag("haystack.agent.input", {"messages": [_USER_MESSAGE], "streaming_callback": None})
        for index, reply in enumerate(replies):
            with tracer.trace("haystack.agent.step", tags={"haystack.agent.step": index}, parent_span=agent) as step:
                with tracer.trace("haystack.agent.step.llm", parent_span=step) as llm:
                    llm.set_content_tag(
                        "haystack.agent.step.llm.input", {"messages": [_USER_MESSAGE], "tools": [_GET_WEATHER]}
                    )
                    if fallback_failed_attempt:
                        try:
                            with tracer.trace(
                                "haystack.chat_generator.run",
                                tags=_chat_generator_tags(),
                                parent_span=tracer.current_span(),
                            ) as failed:
                                failed.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
                                raise RuntimeError("Error code: 500 - server_error")
                        except RuntimeError:
                            pass  # FallbackChatGenerator logs it and tries its next generator
                    if fallback_inner_span:
                        # _trace_chat_generator_run: parent_span or tracer.current_span()
                        with tracer.trace(
                            "haystack.chat_generator.run",
                            tags=_chat_generator_tags(),
                            parent_span=tracer.current_span(),
                        ) as inner:
                            inner.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
                    llm.set_content_tag("haystack.agent.step.llm.output", {"replies": [reply]})
                if index == 0:
                    _run_tool_like_haystack(tracer, step, error=tool_error)
        agent.set_content_tag(
            "haystack.agent.output",
            {"messages": [_USER_MESSAGE], "step_count": 2, "exit_reason": "text", "last_message": replies[1]},
        )
        agent.set_tag("haystack.agent.steps_taken", 2)


def _run_agent_v2(tracer: _LayerLensTracer) -> None:
    """One haystack-ai 2.30.1 ``Agent.run``: ``Pipeline._run_component`` opens a
    ``haystack.component.run`` span for the chat generator ("chat_generator") and
    for the ToolInvoker ("tool_invoker"), with the agent span as parent."""
    with tracer.trace("haystack.agent.run", tags=_agent_tags(), parent_span=tracer.current_span()) as agent:
        agent.set_content_tag("haystack.agent.input", {"messages": [_USER_MESSAGE], "streaming_callback": None})
        for meta, text in ((_REAL_TOOL_CALL_REPLY_META, ""), (_REAL_CHAT_REPLY_META, "pong")):
            with tracer.trace("haystack.component.run", tags=_chat_generator_tags(), parent_span=agent) as gen:
                gen.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
                gen.set_content_tag("haystack.component.output", {"replies": [_ChatReply(text, meta)]})
            if text == "":
                with tracer.trace(
                    "haystack.component.run",
                    tags={"haystack.component.name": "tool_invoker", "haystack.component.type": "ToolInvoker"},
                    parent_span=agent,
                ) as invoker:
                    invoker.set_content_tag("haystack.component.input", {"messages": []})
                    invoker.set_content_tag("haystack.component.output", {"tool_messages": ["sunny in Paris"]})
        agent.set_content_tag("haystack.agent.output", {"messages": [_USER_MESSAGE]})
        agent.set_tag("haystack.agent.steps_taken", 2)


def _run_in_component(tracer: _LayerLensTracer, name: str, comp_type: str, body: Any, output: Any = None) -> None:
    """A pipeline running one component (tags as ``PipelineBase._create_component_span``
    sets them) whose ``run()`` is ``body``."""
    with tracer.trace("haystack.pipeline.run") as pipe:
        pipe.set_content_tag("haystack.pipeline.input_data", {name: {"messages": [_USER_MESSAGE]}})
        tags = {"haystack.component.name": name, "haystack.component.type": comp_type}
        with tracer.trace("haystack.component.run", tags=tags, parent_span=pipe) as comp:
            comp.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
            body(comp)
            if output is not None:
                comp.set_content_tag("haystack.component.output", output)
        pipe.set_content_tag("haystack.pipeline.output_data", {name: output})


#: The model the recorded OpenAI bodies report (the response model, not the requested one).
_MODEL = "gpt-4o-mini-2024-07-18"

_TWO_AGENT_CALLS = [
    (_MODEL, 54, 14, 68, "tool_calls"),
    (_MODEL, 12, 1, 13, "stop"),
]


class TestAgentRunOnItsOwn:
    """``Agent.run()`` outside a pipeline: ``haystack.agent.run`` is the run."""

    def test_uploads_one_trace_with_each_model_call_priced(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _run_agent_v3(adapter._tracer)
        adapter.disconnect()

        assert mock_client.traces.upload.call_count == 1
        events = uploaded["events"]
        assert model_calls(events) == _TWO_AGENT_CALLS
        assert_priced_once_per_call(events, [68, 13], _MODEL)

    def test_frames_the_run_and_reports_the_agent_config(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _run_agent_v3(adapter._tracer)
        adapter.disconnect()

        events = uploaded["events"]
        root = assert_one_closed_tree(events)
        inp = find_event(events, "agent.input")
        out = find_event(events, "agent.output")
        cfg = find_event(events, "environment.config")
        assert [e["span_id"] for e in (inp, cfg, out)] == [root, root, root]
        assert inp["payload"]["input"]["messages"] == [_USER_MESSAGE]
        assert out["payload"]["steps_taken"] == 2
        assert out["payload"]["output"]["exit_reason"] == "text"
        assert out["payload"]["latency_ms"] > 0
        assert cfg["payload"]["tools"] == ["get_weather"]
        assert cfg["payload"]["max_steps"] == 100
        assert cfg["payload"]["exit_conditions"] == ["text"]
        # An Agent run on its own has no name a developer gave it.
        assert not [e for e in events if "agent_name" in e["payload"]]

    def test_config_lists_the_tools_of_a_toolset(self, mock_client):
        """``haystack.agent.tools`` holds a ``Toolset`` (an iterable of tools) when the
        Agent was given one, alone or in a list with single tools."""

        class _Toolset:
            def __init__(self, *tools: _Tool) -> None:
                self._tools = list(tools)

            def __iter__(self) -> Any:
                return iter(self._tools)

        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer
        tags = {
            **_agent_tags(),
            "haystack.agent.tools": [_Toolset(_GET_WEATHER, _Tool("get_time", "Get the time.")), _Tool("search", "")],
        }
        with tracer.trace("haystack.agent.run", tags=tags, parent_span=tracer.current_span()):
            pass
        adapter.disconnect()

        cfg = find_event(uploaded["events"], "environment.config")
        assert cfg["payload"]["tools"] == ["get_weather", "get_time", "search"]

    def test_steps_keep_the_span_hierarchy(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _run_agent_v3(adapter._tracer)
        adapter.disconnect()

        events = uploaded["events"]
        root = assert_one_closed_tree(events)
        steps = find_events(events, "agent.step")
        assert [s["payload"]["step"] for s in steps] == [0, 1]
        assert all(s["parent_span_id"] == root for s in steps)
        invokes = find_events(events, "model.invoke")
        assert [i["parent_span_id"] for i in invokes] == [s["span_id"] for s in steps]
        assert find_event(events, "tool.call")["parent_span_id"] == steps[0]["span_id"]

    def test_tool_call_carries_the_real_tool_name_once(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _run_agent_v3(adapter._tracer)
        adapter.disconnect()

        events = uploaded["events"]
        calls = find_events(events, "tool.call")
        results = find_events(events, "tool.result")
        assert [c["payload"]["tool_name"] for c in calls] == ["get_weather"]
        assert [r["payload"]["tool_name"] for r in results] == ["get_weather"]
        assert calls[0]["payload"]["input"] == {"city": "Paris"}
        assert results[0]["payload"]["output"] == "sunny in Paris"
        assert results[0]["payload"]["latency_ms"] > 0
        assert "error" not in results[0]["payload"]

    def test_failed_tool_call_surfaces_its_error(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        _run_agent_v3(adapter._tracer, tool_error="weather service unreachable")
        adapter.disconnect()

        result = find_event(uploaded["events"], "tool.result")
        assert result["payload"]["error"] == "weather service unreachable"
        assert result["payload"]["error_type"] == "tool_error"

    @pytest.mark.parametrize("capture_content", [True, False])
    def test_failed_model_call_is_one_model_invoke_with_the_error(self, mock_client, capture_content):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=capture_content))
        tracer = adapter._tracer
        with pytest.raises(RuntimeError):
            with tracer.trace("haystack.agent.run", tags=_agent_tags(), parent_span=tracer.current_span()) as agent:
                with tracer.trace("haystack.agent.step", tags={"haystack.agent.step": 0}, parent_span=agent) as step:
                    with tracer.trace("haystack.agent.step.llm", parent_span=step) as llm:
                        llm.set_content_tag("haystack.agent.step.llm.input", {"messages": [_USER_MESSAGE]})
                        raise RuntimeError("Error code: 429 - rate_limit_exceeded")
        adapter.disconnect()

        events = uploaded["events"]
        invokes = find_events(events, "model.invoke")
        assert len(invokes) == 1
        assert "tokens_total" not in invokes[0]["payload"]
        assert not find_events(events, "cost.record")
        output = find_event(events, "agent.output")["payload"]
        # The exception class is the category that survives without content; the
        # message is content.
        for payload in (invokes[0]["payload"], output):
            assert payload["error_type"] == "RuntimeError"
            if capture_content:
                assert payload["error"] == "Error code: 429 - rate_limit_exceeded"
            else:
                assert "error" not in payload
        assert_one_closed_tree(events)

    def test_content_gating(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=False))
        _run_agent_v3(adapter._tracer)
        adapter.disconnect()

        events = uploaded["events"]
        for event_type, key in (
            ("agent.input", "input"),
            ("agent.output", "output"),
            ("model.invoke", "input"),
            ("model.invoke", "output"),
            ("tool.call", "input"),
            ("tool.result", "output"),
        ):
            gated = find_events(events, event_type)
            assert gated, f"no {event_type} emitted"
            assert all(key not in e["payload"] for e in gated), f"{event_type} leaked {key!r}"
        # The model calls keep everything that is not content.
        assert model_calls(events) == _TWO_AGENT_CALLS


class TestAgentInPipeline:
    """An Agent added to a ``Pipeline``: its ``haystack.agent.run`` span is a child
    of its own ``haystack.component.run`` span."""

    def _run(self, adapter: HaystackAdapter, name: str = "weather_agent", comp_type: str = "Agent") -> None:
        tracer = adapter._tracer
        _run_in_component(tracer, name, comp_type, lambda comp: _run_agent_v3(tracer), output={"messages": []})

    def test_model_calls_are_priced_once_each(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        self._run(adapter)
        adapter.disconnect()

        assert mock_client.traces.upload.call_count == 1
        events = uploaded["events"]
        assert model_calls(events) == _TWO_AGENT_CALLS
        assert_priced_once_per_call(events, [68, 13], _MODEL)

    def test_agent_events_hang_under_its_component(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        self._run(adapter)
        adapter.disconnect()

        events = uploaded["events"]
        root = assert_one_closed_tree(events)
        component_call = [c for c in find_events(events, "tool.call") if c["payload"]["tool_name"] == "weather_agent"]
        assert len(component_call) == 1
        component_span = component_call[0]["span_id"]

        agent_inputs = [e for e in find_events(events, "agent.input") if e["span_id"] != root]
        agent_outputs = [e for e in find_events(events, "agent.output") if e["span_id"] != root]
        assert len(agent_inputs) == len(agent_outputs) == 1
        agent_span = agent_inputs[0]["span_id"]
        assert agent_outputs[0]["span_id"] == agent_span
        assert agent_inputs[0]["parent_span_id"] == component_span
        assert [s["parent_span_id"] for s in find_events(events, "agent.step")] == [agent_span, agent_span]
        # The pipeline still frames the trace with exactly one root input/output.
        assert len([e for e in find_events(events, "agent.input") if e["span_id"] == root]) == 1
        assert len([e for e in find_events(events, "agent.output") if e["span_id"] == root]) == 1

    def test_agent_events_carry_the_component_name(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        self._run(adapter)
        adapter.disconnect()

        events = uploaded["events"]
        for event_type in ("model.invoke", "cost.record", "agent.step"):
            named = find_events(events, event_type)
            assert named and all(e["payload"].get("agent_name") == "weather_agent" for e in named), event_type
        tool_call = [c for c in find_events(events, "tool.call") if c["payload"]["tool_name"] == "get_weather"]
        assert [c["payload"].get("agent_name") for c in tool_call] == ["weather_agent"]
        agent_cfg = [c for c in find_events(events, "environment.config") if "tools" in c["payload"]]
        assert len(agent_cfg) == 1
        assert agent_cfg[0]["payload"]["agent_name"] == "weather_agent"
        assert agent_cfg[0]["payload"]["tools"] == ["get_weather"]

    def test_generic_component_name_names_no_agent(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        self._run(adapter, name="agent")
        adapter.disconnect()

        events = uploaded["events"]
        assert not [e for e in events if "agent_name" in e["payload"]]
        # The agent's own configuration is still reported.
        cfg = find_event(events, "environment.config")
        assert cfg["payload"]["tools"] == ["get_weather"]
        assert model_calls(events) == _TWO_AGENT_CALLS

    def test_agent_run_inside_another_component_is_not_named_after_it(self, mock_client):
        """A custom component that calls an Agent from its ``run()``: the
        component's name is not the agent's."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        self._run(adapter, name="trip_planner", comp_type="TripPlanner")
        adapter.disconnect()

        events = uploaded["events"]
        assert model_calls(events) == _TWO_AGENT_CALLS
        assert not [e for e in events if "agent_name" in e["payload"]]
        assert_one_closed_tree(events)


class TestChatGeneratorSpan:
    """``haystack.chat_generator.run``: a component calling an LLM through the
    shared generator utils (``_trace_chat_generator_run``)."""

    @staticmethod
    def _inner_call(tracer: _LayerLensTracer, *, output: bool = True, generator: str = "OpenAIChatGenerator") -> None:
        with tracer.trace(
            "haystack.chat_generator.run", tags=_chat_generator_tags(generator), parent_span=tracer.current_span()
        ) as span:
            span.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
            if output:
                span.set_content_tag(
                    "haystack.component.output", {"replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)]}
                )

    def test_router_call_is_recorded_once_and_attributed_to_the_router(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        tracer = adapter._tracer
        _run_in_component(
            tracer,
            "router",
            "LLMMessagesRouter",
            lambda comp: self._inner_call(tracer),
            output={"chat_generator_text": "pong", "pong": [_USER_MESSAGE]},
        )
        adapter.disconnect()

        events = uploaded["events"]
        assert_one_closed_tree(events)
        invokes = find_events(events, "model.invoke")
        assert model_calls(events) == [("gpt-4o-mini-2024-07-18", 12, 1, 13, "stop")]
        invoke = invokes[0]["payload"]
        assert invoke["component_name"] == "router"
        assert invoke["component_class"] == "OpenAIChatGenerator"
        assert invoke["input"] == {"messages": [_USER_MESSAGE]}
        assert invoke["output"][0]["content"] == [{"text": "pong"}]
        assert_priced_once_per_call(events, [13], _MODEL)
        # The router runs as a component: the call it made is the one model.invoke.
        router = find_event(events, "tool.call")
        assert router["payload"]["component_name"] == "router"
        assert invokes[0]["parent_span_id"] == router["span_id"]
        # haystack's literal "chat_generator" label is not a component a developer named.
        cfg = find_event(events, "environment.config")
        assert cfg["payload"]["components"] == [{"name": "router", "component_class": "LLMMessagesRouter"}]
        assert not [e for e in events if e["payload"].get("component_name") == "chat_generator"]

    def test_call_from_a_tool_like_component_is_recorded(self, mock_client):
        """``QueryExpander`` is not classified as a generator: it keeps its
        tool.call/tool.result, and its LLM call is recorded under it."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer
        _run_in_component(
            tracer, "expander", "QueryExpander", lambda comp: self._inner_call(tracer), output={"queries": ["q"]}
        )
        adapter.disconnect()

        events = uploaded["events"]
        assert_one_closed_tree(events)
        assert model_calls(events) == [("gpt-4o-mini-2024-07-18", 12, 1, 13, "stop")]
        assert_priced_once_per_call(events, [13], _MODEL)
        assert [c["payload"]["tool_name"] for c in find_events(events, "tool.call")] == ["expander"]

    def test_fallback_inner_call_without_output_is_not_counted_again(self, mock_client):
        """``FallbackChatGenerator`` returns the replies itself and leaves its inner
        span without output: its component span records the call, once."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer
        _run_in_component(
            tracer,
            "llm",
            "FallbackChatGenerator",
            lambda comp: self._inner_call(tracer, output=False),
            output={
                "replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)],
                "meta": {"successful_chat_generator_index": 0, "total_attempts": 1, "failed_chat_generators": []},
            },
        )
        adapter.disconnect()

        events = uploaded["events"]
        invokes = find_events(events, "model.invoke")
        assert len(invokes) == 1
        assert invokes[0]["payload"]["component_type"] == "FallbackChatGenerator"
        assert model_calls(events) == [("gpt-4o-mini-2024-07-18", 12, 1, 13, "stop")]
        assert_priced_once_per_call(events, [13], _MODEL)
        assert_one_closed_tree(events)

    def test_fallback_failed_attempt_is_its_own_model_invoke(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))
        tracer = adapter._tracer

        def _fallback(comp: Any) -> None:
            try:
                with tracer.trace("haystack.chat_generator.run", tags=_chat_generator_tags(), parent_span=comp) as span:
                    span.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
                    raise RuntimeError("Error code: 500 - server_error")
            except RuntimeError:
                pass  # FallbackChatGenerator logs it and tries the next generator
            self._inner_call(tracer, output=False)

        _run_in_component(
            tracer,
            "llm",
            "FallbackChatGenerator",
            _fallback,
            output={"replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)], "meta": {"total_attempts": 2}},
        )
        adapter.disconnect()

        events = uploaded["events"]
        invokes = find_events(events, "model.invoke")
        assert len(invokes) == 2
        failed, answered = invokes
        assert failed["payload"]["error"] == "Error code: 500 - server_error"
        assert failed["payload"]["error_type"] == "RuntimeError"
        assert failed["payload"]["component_class"] == "OpenAIChatGenerator"
        assert "tokens_total" not in failed["payload"]
        assert answered["payload"]["component_type"] == "FallbackChatGenerator"
        assert answered["payload"]["tokens_total"] == 13
        assert_priced_once_per_call(events, [13], _MODEL)
        assert_one_closed_tree(events)

    def test_agent_with_a_fallback_chat_generator(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _run_agent_v3(adapter._tracer, fallback_inner_span=True)
        adapter.disconnect()

        events = uploaded["events"]
        assert model_calls(events) == _TWO_AGENT_CALLS
        assert_priced_once_per_call(events, [68, 13], _MODEL)
        assert_one_closed_tree(events)

    def test_agent_fallback_failed_attempts_hang_off_their_step(self, mock_client):
        """The failed attempt's span sits under the step's LLM span, which emits
        nothing of its own when an attempt beneath it is reported: its
        model.invoke must hang off the step, a span the trace carries."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _run_agent_v3(adapter._tracer, fallback_inner_span=True, fallback_failed_attempt=True)
        adapter.disconnect()

        events = uploaded["events"]
        assert_one_closed_tree(events)
        steps = [s["span_id"] for s in find_events(events, "agent.step")]
        invokes = find_events(events, "model.invoke")
        failed = [i for i in invokes if "error_type" in i["payload"]]
        answered = [i for i in invokes if "error_type" not in i["payload"]]
        assert [f["parent_span_id"] for f in failed] == steps
        assert model_calls(answered) == _TWO_AGENT_CALLS
        assert [a["parent_span_id"] for a in answered] == steps
        assert_priced_once_per_call(events, [68, 13], _MODEL)

    @staticmethod
    def _wrapped_call(tracer: _LayerLensTracer) -> None:
        """A chat generator of the developer's own that calls another one through
        haystack's shared span and, as the docs of ``_trace_chat_generator_run``
        ask, sets the reply on it; the caller then sets the same reply on its own
        span around the wrapper."""
        reply = {"replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)]}
        with tracer.trace(
            "haystack.chat_generator.run",
            tags=_chat_generator_tags("WrappingChatGenerator"),
            parent_span=tracer.current_span(),
        ) as outer:
            outer.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
            with tracer.trace(
                "haystack.chat_generator.run", tags=_chat_generator_tags(), parent_span=tracer.current_span()
            ) as inner:
                inner.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
                inner.set_content_tag("haystack.component.output", reply)
            outer.set_content_tag("haystack.component.output", reply)

    def test_wrapped_generator_call_is_recorded_once(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer
        _run_in_component(
            tracer, "router", "LLMMessagesRouter", lambda comp: self._wrapped_call(tracer), output={"pong": []}
        )
        adapter.disconnect()

        events = uploaded["events"]
        invokes = find_events(events, "model.invoke")
        assert model_calls(events) == [("gpt-4o-mini-2024-07-18", 12, 1, 13, "stop")]
        # The innermost span with the reply reports the call.
        assert invokes[0]["payload"]["component_class"] == "OpenAIChatGenerator"
        assert_priced_once_per_call(events, [13], _MODEL)
        assert_one_closed_tree(events)

    def test_agent_step_with_a_wrapped_generator_is_recorded_once(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer
        with tracer.trace("haystack.agent.run", tags=_agent_tags(), parent_span=tracer.current_span()) as agent:
            with tracer.trace("haystack.agent.step", tags={"haystack.agent.step": 0}, parent_span=agent) as step:
                with tracer.trace("haystack.agent.step.llm", parent_span=step) as llm:
                    llm.set_content_tag("haystack.agent.step.llm.input", {"messages": [_USER_MESSAGE]})
                    self._wrapped_call(tracer)
                    llm.set_content_tag(
                        "haystack.agent.step.llm.output", {"replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)]}
                    )
            agent.set_tag("haystack.agent.steps_taken", 1)
        adapter.disconnect()

        events = uploaded["events"]
        assert model_calls(events) == [("gpt-4o-mini-2024-07-18", 12, 1, 13, "stop")]
        assert_priced_once_per_call(events, [13], _MODEL)
        assert_one_closed_tree(events)

    def test_calls_on_worker_threads_land_in_the_pipeline_trace(self, mock_client):
        """``LLMMetadataExtractor`` runs one call per document on a thread pool
        that does not copy the caller's context, passing the parent span."""
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer

        def _extract(comp: Any) -> None:
            def _one(_doc: str) -> None:
                with tracer.trace("haystack.chat_generator.run", tags=_chat_generator_tags(), parent_span=comp) as span:
                    span.set_content_tag("haystack.component.input", {"messages": [_USER_MESSAGE]})
                    span.set_content_tag(
                        "haystack.component.output", {"replies": [_ChatReply("pong", _REAL_CHAT_REPLY_META)]}
                    )

            threads = [threading.Thread(target=_one, args=(doc,)) for doc in ("doc-1", "doc-2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        _run_in_component(tracer, "extractor", "LLMMetadataExtractor", _extract, output={"documents": []})
        adapter.disconnect()

        assert mock_client.traces.upload.call_count == 1
        events = uploaded["events"]
        assert model_calls(events) == [("gpt-4o-mini-2024-07-18", 12, 1, 13, "stop")] * 2
        assert_priced_once_per_call(events, [13, 13], _MODEL)
        assert all(e["payload"]["component_name"] == "extractor" for e in find_events(events, "model.invoke"))
        assert_one_closed_tree(events)


class TestHaystack2Agent:
    """The haystack-ai 2.30.1 Agent span shape."""

    def test_agent_on_its_own_uploads_its_model_calls(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        _run_agent_v2(adapter._tracer)
        adapter.disconnect()

        assert mock_client.traces.upload.call_count == 1
        events = uploaded["events"]
        assert model_calls(events) == _TWO_AGENT_CALLS
        assert_priced_once_per_call(events, [68, 13], _MODEL)
        assert_one_closed_tree(events)
        cfg = find_event(events, "environment.config")
        assert cfg["payload"]["tools"] == ["get_weather"]
        assert cfg["payload"]["components"] == [
            {"name": "chat_generator", "component_class": "OpenAIChatGenerator"},
            {"name": "tool_invoker", "component_class": "ToolInvoker"},
        ]

    def test_agent_in_pipeline_hangs_under_its_component(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        tracer = adapter._tracer
        _run_in_component(tracer, "weather_agent", "Agent", lambda comp: _run_agent_v2(tracer), output={"messages": []})
        adapter.disconnect()

        events = uploaded["events"]
        root = assert_one_closed_tree(events)
        assert model_calls(events) == _TWO_AGENT_CALLS
        assert_priced_once_per_call(events, [68, 13], _MODEL)
        nested = [e for e in find_events(events, "agent.input") if e["span_id"] != root]
        assert len(nested) == 1
        assert nested[0]["payload"]["agent_name"] == "weather_agent"
        assert all(i["parent_span_id"] == nested[0]["span_id"] for i in find_events(events, "model.invoke"))


class TestSpanProtocolSetTags:
    """haystack 3's ``CompactionHook`` calls ``tracer.current_span().set_tags(tags=...)``
    (a method of haystack's ``Span``) on every LLM step."""

    def test_set_tags_sets_each_tag(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client)
        with adapter._tracer.trace("haystack.pipeline.run"):
            with adapter._tracer.trace("haystack.component.run") as cs:
                cs.set_tags(tags={"haystack.component.name": "retriever", "haystack.component.type": "BM25Retriever"})
        adapter.disconnect()

        call = find_event(uploaded["events"], "tool.call")
        assert call["payload"]["component_name"] == "retriever"
        assert call["payload"]["component_class"] == "BM25Retriever"

    def test_null_span_accepts_set_tags(self, mock_client):
        adapter = _make_adapter(mock_client)
        span = adapter._tracer.current_span()
        assert isinstance(span, _NullSpan)
        span.set_tags(tags={"haystack.agent.hook.compaction.triggered": False})
        adapter.disconnect()


class _Unprintable:
    """Pipeline output whose ``str()`` raises: the adapter cannot serialize it."""

    def __str__(self) -> str:
        raise ValueError("cannot render")


class TestRunEndsEvenWhenEmittingFails:
    def test_pipeline_run_is_released_and_flushed(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = _make_adapter(mock_client, config=CaptureConfig(capture_content=True))

        def _pipeline() -> Any:
            _simulate_pipeline(adapter._tracer, input_data={"q": "hi"}, output_data={"answer": _Unprintable()})
            return _current_run.get()

        # Run in a copied context so a leaked run cannot reach the other tests.
        leaked = contextvars.copy_context().run(_pipeline)
        adapter.disconnect()

        assert leaked is None, "the pipeline's run stayed bound after its span closed"
        assert mock_client.traces.upload.call_count == 1
        assert find_event(uploaded["events"], "agent.input")
