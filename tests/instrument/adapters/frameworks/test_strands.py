"""Tests for Strands Agents adapter.

Uses real strands hook event types to test the hook-based adapter.
Tests call hook handler methods directly with properly constructed event objects.
"""

from __future__ import annotations

import gc
import asyncio
import logging
import weakref
import threading
import contextvars
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

strands_mod = pytest.importorskip("strands")
from strands.hooks import HookRegistry  # noqa: E402
from strands.hooks.events import (  # noqa: E402
    AfterToolCallEvent,
    AfterModelCallEvent,
    BeforeToolCallEvent,
    AfterInvocationEvent,
    BeforeModelCallEvent,
    AgentInitializedEvent,
    BeforeInvocationEvent,
)

from layerlens.instrument._capture_config import CaptureConfig  # noqa: E402
from layerlens.instrument.adapters.frameworks.strands import (
    StrandsAdapter,
)  # noqa: E402

from .conftest import find_event, find_events, capture_framework_trace  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cycle(input_tokens: int = 0, output_tokens: int = 0) -> Mock:
    """Create a mock Strands cycle with per-call token usage."""
    cycle = Mock()
    cycle.usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
    }
    # Real cycles carry a string stop reason; a Mock here leaks into the
    # cost.record payload and breaks attestation hashing (unserializable),
    # which the adapter's broad exception handler then swallows silently.
    cycle.stop_reason = "end_turn"
    return cycle


def _make_agent(
    name: str = "TestAgent",
    model_id: str = "us.anthropic.claude-sonnet-4-20250514",
    tool_names: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> Mock:
    agent = Mock()
    agent.name = name
    type(agent).__name__ = "Agent"
    agent.model = Mock()
    agent.model.config = {"model_id": model_id}
    agent.tool_names = tool_names or []
    agent.system_prompt = system_prompt
    # event_loop_metrics — cycles populated by _simulate_invocation
    agent.event_loop_metrics = Mock()
    agent.event_loop_metrics.agent_invocations = []
    agent.hooks = HookRegistry()
    return agent


def _make_result(
    stop_reason: str = "end_turn",
    message: Any = "Final answer",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Mock:
    result = Mock()
    result.stop_reason = stop_reason
    result.message = message
    result.metrics = Mock()
    result.metrics.accumulated_usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }
    return result


def _make_model_stop_response(stop_reason: str = "end_turn") -> Any:
    resp = AfterModelCallEvent.ModelStopResponse(
        message=Mock(),
        stop_reason=stop_reason,
    )
    return resp


def _simulate_invocation(
    adapter: StrandsAdapter,
    agent: Any,
    messages: Any = "Hello!",
    tool_calls: Optional[list] = None,
    model_tokens: Optional[Dict[str, int]] = None,
    result: Optional[Any] = None,
) -> None:
    """Simulate a full Strands agent invocation lifecycle via hook calls."""
    # The adapter only traces agents it is attached to, as with
    # Agent(hooks=[adapter]); a no-op when it already is.
    agent.hooks.add_hook(adapter)

    # BeforeInvocation
    before_inv = BeforeInvocationEvent(agent=agent, invocation_state={}, messages=messages)
    adapter._on_before_invocation(before_inv)

    # Model call
    before_model = BeforeModelCallEvent(agent=agent, invocation_state={})
    adapter._on_before_model(before_model)

    stop_reason = "end_turn" if not tool_calls else "tool_use"
    after_model = AfterModelCallEvent(
        agent=agent,
        invocation_state={},
        stop_response=_make_model_stop_response(stop_reason),
    )
    adapter._on_after_model(after_model)

    # Tool calls
    if tool_calls:
        for tc in tool_calls:
            tool_use = {
                "name": tc["name"],
                "toolUseId": tc.get("id", "tc-1"),
                "input": tc.get("input", {}),
            }
            tool_result = tc.get(
                "result",
                {
                    "toolUseId": tc.get("id", "tc-1"),
                    "status": "success",
                    "content": [{"text": "ok"}],
                },
            )
            before_tool = BeforeToolCallEvent(
                agent=agent,
                selected_tool=Mock(name=tc["name"]),
                tool_use=tool_use,
                invocation_state={},
            )
            adapter._on_before_tool(before_tool)

            after_tool = AfterToolCallEvent(
                agent=agent,
                selected_tool=Mock(name=tc["name"]),
                tool_use=tool_use,
                invocation_state={},
                result=tool_result,
                exception=tc.get("exception"),
            )
            adapter._on_after_tool(after_tool)

    # Set up per-cycle token data on agent (simulates what Strands does
    # AFTER AfterModelCallEvent but BEFORE AfterInvocationEvent)
    if model_tokens:
        invocation = Mock()
        invocation.cycles = [
            _make_cycle(
                input_tokens=model_tokens.get("input", 0),
                output_tokens=model_tokens.get("output", 0),
            )
        ]
        agent.event_loop_metrics.agent_invocations = [invocation]

    # AfterInvocation
    if result is None:
        result = _make_result(
            input_tokens=model_tokens.get("input", 0) if model_tokens else 0,
            output_tokens=model_tokens.get("output", 0) if model_tokens else 0,
        )
    after_inv = AfterInvocationEvent(agent=agent, invocation_state={}, result=result)
    adapter._on_after_invocation(after_inv)


#: The seven lifecycle events the adapter hooks.
_HOOK_EVENTS = (
    AgentInitializedEvent,
    BeforeInvocationEvent,
    AfterInvocationEvent,
    BeforeModelCallEvent,
    AfterModelCallEvent,
    BeforeToolCallEvent,
    AfterToolCallEvent,
)


def _hooks_for(registry: HookRegistry, event_type: type) -> List[Any]:
    """The callbacks *registry* dispatches for *event_type*.

    Read through the public ``get_callbacks_for`` so the answer doesn't depend
    on how strands stores them: bare callbacks up to 1.42.0,
    ``_CallbackEntry(callback=..., order=...)`` from 1.43.0. Dispatch reads only
    the event's type and ``should_reverse_callbacks``, so an uninitialised
    instance is enough to ask.
    """
    return list(registry.get_callbacks_for(object.__new__(event_type)))


def _adapter_hooks(registry: HookRegistry, adapter: StrandsAdapter) -> List[Any]:
    return [cb for et in _HOOK_EVENTS for cb in _hooks_for(registry, et) if getattr(cb, "__self__", None) is adapter]


def _register(adapter: StrandsAdapter, agent: Any, how: str) -> None:
    """Attach *adapter* to *agent* in one of the two ways a customer can."""
    if how == "connect(target=agent)":
        adapter.connect(target=agent)
    else:
        # What Agent(hooks=[adapter]) does: HookRegistry.add_hook -> register_hooks.
        adapter.connect()
        agent.hooks.add_hook(adapter)


_REGISTRATIONS = pytest.mark.parametrize("how", ["connect(target=agent)", "hooks=[adapter]"])


class _BareCallbackRegistry:
    """strands <= 1.42.0 ``HookRegistry`` storage: event type -> list of bare
    callbacks. 1.43.0 wrapped each one in ``_CallbackEntry``; the matrix row
    runs a release with the newer layout, so the older one is kept here."""

    def __init__(self) -> None:
        self._registered_callbacks: Dict[type, List[Any]] = {}

    def add_callback(self, event_type: type, callback: Any) -> None:
        self._registered_callbacks.setdefault(event_type, []).append(callback)


class _OpaqueRegistry:
    """A hook registry whose storage the adapter can't recognise."""

    def __init__(self) -> None:
        self._hooks: List[Any] = []

    @property
    def hooks(self) -> List[Any]:
        return [callback for _, callback in self._hooks]

    def add_callback(self, event_type: type, callback: Any) -> None:
        self._hooks.append((event_type, callback))

    def invoke(self, event: Any) -> None:
        for event_type, callback in self._hooks:
            if isinstance(event, event_type):
                callback(event)


class _RenamedEntryRegistry:
    """strands' ``_registered_callbacks`` dict of lists, but each entry keeps
    the callback under a field the adapter doesn't know: the kind of change
    1.43.0 made when it introduced ``_CallbackEntry``."""

    def __init__(self) -> None:
        self._registered_callbacks: Dict[type, List[Any]] = {}

    @property
    def hooks(self) -> List[Any]:
        return [entry.fn for entries in self._registered_callbacks.values() for entry in entries]

    def add_callback(self, event_type: type, callback: Any) -> None:
        self._registered_callbacks.setdefault(event_type, []).append(SimpleNamespace(fn=callback))

    def invoke(self, event: Any) -> None:
        for entry in self._registered_callbacks.get(type(event), []):
            entry.fn(event)


class _UnhashableRegistry(_OpaqueRegistry):
    """Can't key a weak dict: defining ``__eq__`` drops ``__hash__``, as making
    ``HookRegistry`` an ``eq=True`` dataclass would."""

    def __eq__(self, other: object) -> bool:
        return self is other


_ADAPTER_LOGGER = "layerlens.instrument.adapters.frameworks.strands"


def _adapter_warnings(caplog: Any) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _ADAPTER_LOGGER and r.levelno == logging.WARNING]


def _disconnect_from_another_thread(adapter: StrandsAdapter) -> None:
    """disconnect() as a concurrent caller makes it: from its own thread, so
    from its own context, while a run is in progress in the caller's."""
    thread = threading.Thread(target=adapter.disconnect)
    thread.start()
    thread.join()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_sets_connected(self, mock_client):
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        assert adapter.is_connected
        adapter.disconnect()
        assert not adapter.is_connected

    def test_connect_with_target_registers_hooks(self, mock_client):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        adapter.connect(target=agent)
        assert len(_adapter_hooks(agent.hooks, adapter)) == 7
        adapter.disconnect()

    def test_disconnect_deregisters_hooks(self, mock_client):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        adapter.connect(target=agent)
        adapter.disconnect()
        assert _adapter_hooks(agent.hooks, adapter) == []

    def test_disconnect_clears_state(self, mock_client):
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        adapter.disconnect()
        # Run-scoped state now lives in the per-run RunState (ContextVar),
        # not instance scalars — the invariant is "no run active" (LAY-3576).
        assert adapter._get_run() is None

    def test_register_hooks_protocol(self, mock_client):
        """Adapter implements HookProvider protocol (register_hooks)."""
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        registry = HookRegistry()
        adapter.register_hooks(registry)
        assert registry.has_callbacks()
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Invocation lifecycle
# ---------------------------------------------------------------------------


class TestInvocationLifecycle:
    def test_invocation_emits_input_and_output(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        adapter.connect()

        agent = _make_agent(name="MyAgent", model_id="claude-sonnet")
        _simulate_invocation(adapter, agent, messages="What is AI?")

        events = uploaded["events"]
        agent_in = find_event(events, "agent.input")
        assert agent_in["payload"]["agent_name"] == "MyAgent"
        assert agent_in["payload"]["model"] == "claude-sonnet"
        assert agent_in["payload"]["input"] == "What is AI?"

        agent_out = find_event(events, "agent.output")
        assert agent_out["payload"]["agent_name"] == "MyAgent"
        assert agent_out["payload"]["duration_ns"] > 0
        assert agent_out["payload"]["stop_reason"] == "end_turn"

        adapter.disconnect()

    def test_invocation_flushes_trace(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(adapter, agent)

        assert uploaded.get("trace_id") is not None
        assert uploaded["attestation"].get("root_hash") is not None

        adapter.disconnect()

    def test_input_gated_by_capture_content(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=False))
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(adapter, agent, messages="secret input")

        events = uploaded["events"]
        agent_in = find_event(events, "agent.input")
        assert "input" not in agent_in["payload"]

        adapter.disconnect()


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------


class TestModelCalls:
    def test_model_invoke_emits_timing_and_model(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent(model_id="claude-sonnet")
        _simulate_invocation(adapter, agent, model_tokens={"input": 100, "output": 50})

        events = uploaded["events"]
        model_evt = find_event(events, "model.invoke")
        assert model_evt["payload"]["model"] == "claude-sonnet"
        # Tokens are NOT on model.invoke — they come via cost.record
        assert "tokens_prompt" not in model_evt["payload"]

        adapter.disconnect()

    def test_per_cycle_cost_record(self, mock_client):
        """Per-cycle cost.record events are emitted from _on_after_invocation."""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent(model_id="claude-sonnet")
        _simulate_invocation(adapter, agent, model_tokens={"input": 100, "output": 50})

        events = uploaded["events"]
        cost_evt = find_event(events, "cost.record")
        assert cost_evt["payload"]["tokens_prompt"] == 100
        assert cost_evt["payload"]["tokens_completion"] == 50
        assert cost_evt["payload"]["tokens_total"] == 150
        assert cost_evt["payload"]["model"] == "claude-sonnet"

        # cost.record should be parented to the model span
        model_evt = find_event(events, "model.invoke")
        assert cost_evt["parent_span_id"] == model_evt["span_id"]

        adapter.disconnect()

    def test_model_error(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        agent = _make_agent()
        adapter.connect(target=agent)

        before_inv = BeforeInvocationEvent(agent=agent, invocation_state={}, messages="test")
        adapter._on_before_invocation(before_inv)

        before_model = BeforeModelCallEvent(agent=agent, invocation_state={})
        adapter._on_before_model(before_model)

        after_model = AfterModelCallEvent(
            agent=agent,
            invocation_state={},
            exception=RuntimeError("API timeout"),
        )
        adapter._on_after_model(after_model)

        after_inv = AfterInvocationEvent(agent=agent, invocation_state={})
        adapter._on_after_invocation(after_inv)

        events = uploaded["events"]
        model_evt = find_event(events, "model.invoke")
        assert model_evt["payload"]["error"] == "API timeout"
        assert model_evt["payload"]["error_type"] == "RuntimeError"

        adapter.disconnect()


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


class TestToolCalls:
    def test_tool_call_and_result(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(
            adapter,
            agent,
            tool_calls=[
                {
                    "name": "web_search",
                    "id": "tc-123",
                    "input": {"query": "AI safety"},
                    "result": {
                        "toolUseId": "tc-123",
                        "status": "success",
                        "content": [{"text": "Found 5 results"}],
                    },
                }
            ],
        )

        events = uploaded["events"]
        tool_call = find_event(events, "tool.call")
        assert tool_call["payload"]["tool_name"] == "web_search"
        assert tool_call["payload"]["input"] == {"query": "AI safety"}
        assert tool_call["span_name"] == "tool:web_search"

        tool_result = find_event(events, "tool.result")
        assert tool_result["payload"]["tool_name"] == "web_search"
        assert tool_result["payload"]["status"] == "success"

        adapter.disconnect()

    def test_tool_error(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        agent = _make_agent()
        adapter.connect(target=agent)

        before_inv = BeforeInvocationEvent(agent=agent, invocation_state={}, messages="test")
        adapter._on_before_invocation(before_inv)

        before_model = BeforeModelCallEvent(agent=agent, invocation_state={})
        adapter._on_before_model(before_model)
        after_model = AfterModelCallEvent(
            agent=agent,
            invocation_state={},
            stop_response=_make_model_stop_response("tool_use"),
        )
        adapter._on_after_model(after_model)

        tool_use = {"name": "broken_tool", "toolUseId": "tc-err", "input": {}}
        before_tool = BeforeToolCallEvent(agent=agent, selected_tool=Mock(), tool_use=tool_use, invocation_state={})
        adapter._on_before_tool(before_tool)

        after_tool = AfterToolCallEvent(
            agent=agent,
            selected_tool=Mock(),
            tool_use=tool_use,
            invocation_state={},
            result={"toolUseId": "tc-err", "status": "error", "content": []},
            exception=ValueError("bad input"),
        )
        adapter._on_after_tool(after_tool)

        after_inv = AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result())
        adapter._on_after_invocation(after_inv)

        events = uploaded["events"]
        tool_result = find_event(events, "tool.result")
        assert tool_result["payload"]["error"] == "bad input"
        assert tool_result["payload"]["error_type"] == "ValueError"
        assert tool_result["payload"]["status"] == "error"

        adapter.disconnect()

    def test_tool_content_gated(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=False))
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(
            adapter,
            agent,
            tool_calls=[
                {
                    "name": "search",
                    "id": "tc-1",
                    "input": {"secret": "data"},
                    "result": {
                        "toolUseId": "tc-1",
                        "status": "success",
                        "content": [{"text": "secret result"}],
                    },
                }
            ],
        )

        events = uploaded["events"]
        tool_call = find_event(events, "tool.call")
        assert "input" not in tool_call["payload"]
        tool_result = find_event(events, "tool.result")
        assert "output" not in tool_result["payload"]

        adapter.disconnect()

    def test_multiple_tool_calls(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(
            adapter,
            agent,
            tool_calls=[
                {"name": "search", "id": "tc-1", "input": {"q": "a"}},
                {"name": "calculator", "id": "tc-2", "input": {"expr": "2+2"}},
            ],
        )

        events = uploaded["events"]
        tool_calls = find_events(events, "tool.call")
        assert len(tool_calls) == 2
        assert tool_calls[0]["payload"]["tool_name"] == "search"
        assert tool_calls[1]["payload"]["tool_name"] == "calculator"

        adapter.disconnect()


# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------


class TestAgentConfig:
    def test_config_emitted_on_invocation(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        adapter.connect()

        agent = _make_agent(
            name="SmartAgent",
            model_id="claude-sonnet",
            tool_names=["search", "calculator"],
            system_prompt="Be helpful",
        )
        _simulate_invocation(adapter, agent)

        events = uploaded["events"]
        config = find_event(events, "environment.config")
        assert config["payload"]["agent_name"] == "SmartAgent"
        assert config["payload"]["model"] == "claude-sonnet"
        assert config["payload"]["tools"] == ["search", "calculator"]
        assert config["payload"]["system_prompt"] == "Be helpful"

        adapter.disconnect()

    def test_config_emitted_once(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent(name="Agent1")
        _simulate_invocation(adapter, agent)

        # Second invocation — config should not re-emit
        uploaded2 = capture_framework_trace(mock_client)
        _simulate_invocation(adapter, agent)

        events2 = uploaded2["events"]
        configs = find_events(events2, "environment.config")
        assert len(configs) == 0

        adapter.disconnect()

    def test_system_prompt_gated_by_capture_content(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=False))
        adapter.connect()

        agent = _make_agent(system_prompt="Secret prompt")
        _simulate_invocation(adapter, agent)

        events = uploaded["events"]
        config = find_event(events, "environment.config")
        assert "system_prompt" not in config["payload"]

        adapter.disconnect()


# ---------------------------------------------------------------------------
# Trace integrity
# ---------------------------------------------------------------------------


class TestTraceIntegrity:
    def test_all_events_share_trace_id(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(
            adapter,
            agent,
            model_tokens={"input": 100, "output": 50},
            tool_calls=[{"name": "search", "id": "tc-1", "input": {}}],
        )

        events = uploaded["events"]
        trace_ids = {e["trace_id"] for e in events}
        assert len(trace_ids) == 1

        adapter.disconnect()

    def test_sequence_ids_monotonic(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(adapter, agent, model_tokens={"input": 10, "output": 5})

        events = uploaded["events"]
        seq_ids = [e["sequence_id"] for e in events]
        assert seq_ids == sorted(seq_ids)

        adapter.disconnect()

    def test_span_hierarchy(self, mock_client):
        """Model and tool events should be children of the run span."""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent()
        _simulate_invocation(
            adapter,
            agent,
            model_tokens={"input": 10, "output": 5},
            tool_calls=[{"name": "search", "id": "tc-1", "input": {}}],
        )

        events = uploaded["events"]
        run_input = find_event(events, "agent.input")
        run_span_id = run_input["span_id"]

        model_evt = find_event(events, "model.invoke")
        assert model_evt["parent_span_id"] == run_span_id

        tool_evt = find_event(events, "tool.call")
        assert tool_evt["parent_span_id"] == run_span_id

        adapter.disconnect()


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_handlers_dont_crash_on_none_result(self, mock_client):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        adapter.connect(target=agent)

        before_inv = BeforeInvocationEvent(agent=agent, invocation_state={})
        adapter._on_before_invocation(before_inv)

        after_inv = AfterInvocationEvent(agent=agent, invocation_state={})
        adapter._on_after_invocation(after_inv)

        adapter.disconnect()

    def test_no_events_when_no_collector(self, mock_client):
        """Calling handlers without a collector should silently no-op."""
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        agent = _make_agent()
        after_model = AfterModelCallEvent(agent=agent, invocation_state={})
        adapter._on_after_model(after_model)
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Cost-emit isolation (P2 / LAY-3573)
# ---------------------------------------------------------------------------


class TestCostEmitIsolation:
    """A failure while emitting per-cycle ``cost.record`` events must degrade
    to "no cost.record" — it must NOT take down the run's ``agent.output`` or
    skip the flush, silently dropping the whole trace.

    Regression for the phase-4 finding: ``_on_after_invocation`` ran the cost
    emit in the same ``try`` as ``_fire("agent.output")`` and the
    ``_end_trace()`` flush, so one unserializable cost payload (e.g. a
    non-string cycle ``stop_reason`` that breaks attestation hashing) was
    swallowed by the blanket ``except`` and the entire trace was lost.
    """

    def test_cost_emit_failure_still_emits_output_and_flushes(self, mock_client, monkeypatch):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        def boom(_agent):
            raise RuntimeError("cost payload broke attestation hashing")

        monkeypatch.setattr(adapter, "_emit_per_cycle_tokens", boom)

        agent = _make_agent(name="ResilientAgent", model_id="claude-sonnet")
        _simulate_invocation(adapter, agent, model_tokens={"input": 100, "output": 50})

        events = uploaded["events"]
        # The run output survives the cost-emit failure...
        agent_out = find_event(events, "agent.output")
        assert agent_out["payload"]["agent_name"] == "ResilientAgent"
        # ...and the trace flushed instead of being dropped.
        assert mock_client.traces.upload.called
        assert uploaded.get("trace_id") is not None
        assert uploaded["attestation"].get("root_hash") is not None
        # The failed cost emit degraded cleanly to no cost.record.
        assert find_events(events, "cost.record") == []

        adapter.disconnect()

    def test_unserializable_cost_payload_does_not_drop_trace(self, mock_client):
        """The real trigger documented on ``_make_cycle``: a non-string cycle
        ``stop_reason`` leaks into the ``cost.record`` payload and breaks
        attestation hashing at emit time. The trace must still upload."""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent(name="RealisticAgent", model_id="claude-sonnet")
        adapter.connect(target=agent)

        before_inv = BeforeInvocationEvent(agent=agent, invocation_state={}, messages="hi")
        adapter._on_before_invocation(before_inv)
        before_model = BeforeModelCallEvent(agent=agent, invocation_state={})
        adapter._on_before_model(before_model)
        after_model = AfterModelCallEvent(
            agent=agent,
            invocation_state={},
            stop_response=_make_model_stop_response("end_turn"),
        )
        adapter._on_after_model(after_model)

        # Plant a cycle with real tokens (so the cost branch runs) and an
        # unserializable stop_reason — ``compute_hash`` raises ``TypeError`` on
        # any type its ``_json_default`` doesn't recognize.
        bad_cycle = {
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "stop_reason": object(),
        }
        invocation = Mock()
        invocation.cycles = [bad_cycle]
        agent.event_loop_metrics.agent_invocations = [invocation]

        after_inv = AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result())
        adapter._on_after_invocation(after_inv)

        events = uploaded["events"]
        assert find_event(events, "agent.output")["payload"]["agent_name"] == "RealisticAgent"
        assert mock_client.traces.upload.called
        assert uploaded.get("trace_id") is not None
        # F-L1-003: the unserializable stop_reason that leaked into the cost.record
        # payload is coerced to a string (matching the upload path's default=str) so
        # the event still hashes — the cost.record now SURVIVES instead of being
        # dropped on the hash failure, and emit() never raises into the host app.
        costs = find_events(events, "cost.record")
        assert len(costs) == 1
        assert isinstance(costs[0]["payload"]["stop_reason"], str)

        adapter.disconnect()


# ---------------------------------------------------------------------------
# Disconnect leave-no-trace invariants (LAY-3577 / T3)
# ---------------------------------------------------------------------------


class TestDisconnectLeaveNoTrace:
    """disconnect() must remove only the adapter's own hook callbacks from
    every hook registry it was added to (``connect(target=agent)`` or
    ``Agent(hooks=[adapter])``), leaving user hooks registered and functional,
    and must be safe to call repeatedly. A run already in progress finishes
    and uploads first. A hook it can't remove is reported and never traces
    (LAY-4255)."""

    @_REGISTRATIONS
    def test_user_hook_survives_disconnect(self, mock_client, how):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        calls = []

        def sentinel(event):
            calls.append(event)

        agent.hooks.add_callback(BeforeInvocationEvent, sentinel)
        _register(adapter, agent, how)
        adapter.disconnect()

        # Still registered in the agent's hook registry...
        assert _hooks_for(agent.hooks, BeforeInvocationEvent) == [sentinel]
        # ...and still functional.
        evt = BeforeInvocationEvent(agent=agent, invocation_state={})
        agent.hooks.invoke_callbacks(evt)
        assert calls == [evt]

    @_REGISTRATIONS
    def test_adapter_hooks_removed_after_disconnect(self, mock_client, caplog, how):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        _register(adapter, agent, how)
        # The check below can see the adapter's hooks, so its empty result
        # after disconnect() means removed, not invisible.
        assert len(_adapter_hooks(agent.hooks, adapter)) == 7

        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            adapter.disconnect()

        assert _adapter_hooks(agent.hooks, adapter) == []
        assert _adapter_warnings(caplog) == []  # a clean removal reports nothing
        # Hooks fired after disconnect must not reach the adapter.
        agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent, invocation_state={}))
        assert adapter._get_run() is None
        assert uploaded["events"] == []

    @_REGISTRATIONS
    def test_double_disconnect_is_safe(self, mock_client, caplog, how):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        calls = []

        def sentinel(event):
            calls.append(event)

        agent.hooks.add_callback(BeforeInvocationEvent, sentinel)
        _register(adapter, agent, how)
        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            adapter.disconnect()
            adapter.disconnect()  # must not raise, or report hooks it already removed

        assert _adapter_hooks(agent.hooks, adapter) == []
        assert _hooks_for(agent.hooks, BeforeInvocationEvent) == [sentinel]
        assert _adapter_warnings(caplog) == []

    def test_reconnect_cycle(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent(name="CycleAgent")

        adapter.connect(target=agent)
        adapter.disconnect()
        adapter.connect(target=agent)

        # Re-registered after reconnect, once: hooks left over from the first
        # connect would make every event fire twice.
        assert len(_adapter_hooks(agent.hooks, adapter)) == 7

        # Events flow again after reconnect.
        _simulate_invocation(adapter, agent, messages="cycle input")
        agent_in = find_event(uploaded["events"], "agent.input")
        assert agent_in["payload"]["agent_name"] == "CycleAgent"

        # Second disconnect cleans up again.
        adapter.disconnect()
        assert _adapter_hooks(agent.hooks, adapter) == []

    def test_disconnect_removes_hooks_from_every_agent(self, mock_client):
        # One adapter passed as hooks=[adapter] to several agents (one per request, say).
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        agents = [_make_agent(name=f"Agent{i}") for i in range(3)]
        for agent in agents:
            agent.hooks.add_hook(adapter)
            assert len(_adapter_hooks(agent.hooks, adapter)) == 7

        adapter.disconnect()

        for agent in agents:
            assert _adapter_hooks(agent.hooks, adapter) == []

    def test_bare_callback_registry_layout(self, mock_client):
        """strands <= 1.42.0 stored bare callbacks; those must come out too."""
        registry = _BareCallbackRegistry()

        def sentinel(event):
            pass

        registry.add_callback(BeforeInvocationEvent, sentinel)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        adapter.register_hooks(registry)

        def adapter_entries() -> List[Any]:
            return [
                cb
                for cbs in registry._registered_callbacks.values()
                for cb in cbs
                if getattr(cb, "__self__", None) is adapter
            ]

        assert len(adapter_entries()) == 7

        adapter.disconnect()

        assert adapter_entries() == []
        assert registry._registered_callbacks[BeforeInvocationEvent] == [sentinel]

    @pytest.mark.parametrize("registry_type", [_OpaqueRegistry, _RenamedEntryRegistry])
    def test_unremovable_hooks_are_reported_and_never_trace(self, mock_client, caplog, registry_type):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        registry = registry_type()
        adapter.register_hooks(registry)
        agent = _make_agent()

        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            adapter.disconnect()

        # The leak is surfaced, not swallowed...
        assert len(_adapter_warnings(caplog)) == 1
        # ...the hooks are still registered...
        assert len(registry.hooks) == 7
        # ...and a whole invocation through them traces nothing.
        registry.invoke(BeforeInvocationEvent(agent=agent, invocation_state={}))
        registry.invoke(AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result()))
        assert adapter._get_run() is None
        assert uploaded["events"] == []

    def test_untrackable_registry_is_reported_and_never_traces(self, mock_client, caplog):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        registry = _UnhashableRegistry()
        agent = _make_agent()

        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            adapter.register_hooks(registry)  # runs inside Agent(...): must not raise

        # Registered (it traces while connected), and the gap is reported up front.
        assert len(registry.hooks) == 7
        assert len(_adapter_warnings(caplog)) == 1

        adapter.disconnect()
        registry.invoke(BeforeInvocationEvent(agent=agent, invocation_state={}))
        registry.invoke(AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result()))
        assert adapter._get_run() is None
        assert uploaded["events"] == []

    def test_disconnect_mid_dispatch_does_not_skip_user_hooks(self, mock_client):
        """The async dispatcher walks the registry's list lazily, awaiting
        between callbacks. Removing the adapter's hook (ahead of the cursor)
        must not shift the list under it and skip the next user hook."""
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        adapter.connect(target=agent)  # the adapter's hook runs first
        calls = []

        def disconnect_now(event):
            calls.append("disconnect_now")
            adapter.disconnect()

        def later(event):
            calls.append("later")

        agent.hooks.add_callback(BeforeInvocationEvent, disconnect_now)
        agent.hooks.add_callback(BeforeInvocationEvent, later)

        asyncio.run(agent.hooks.invoke_callbacks_async(BeforeInvocationEvent(agent=agent, invocation_state={})))

        assert calls == ["disconnect_now", "later"]
        assert _hooks_for(agent.hooks, BeforeInvocationEvent) == [disconnect_now, later]

    def test_run_in_flight_at_disconnect_still_uploads(self, mock_client):
        """disconnect() from another thread while an invocation runs: that run
        finishes and uploads (its context is left clean), then the adapter's
        hooks leave the agent."""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent(name="InFlight")
        adapter.connect(target=agent)

        def invocation() -> Any:
            agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent, invocation_state={}))
            _disconnect_from_another_thread(adapter)
            agent.hooks.invoke_callbacks(AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result()))
            return adapter._get_run()

        # Its own context, like the task or thread the agent runs in.
        assert contextvars.copy_context().run(invocation) is None

        assert mock_client.traces.upload.call_count == 1
        assert find_event(uploaded["events"], "agent.output")["payload"]["agent_name"] == "InFlight"
        assert _adapter_hooks(agent.hooks, adapter) == []

    def test_two_runs_in_flight_on_one_agent_both_upload(self, mock_client):
        """Two invocations of one agent overlap (separate tasks) when
        disconnect() lands: both finish and upload, so the hooks stay until
        the second one ends."""
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        adapter.connect(target=agent)
        before = BeforeInvocationEvent(agent=agent, invocation_state={})
        after = AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result())
        first, second = contextvars.copy_context(), contextvars.copy_context()

        first.run(agent.hooks.invoke_callbacks, before)
        second.run(agent.hooks.invoke_callbacks, before)
        _disconnect_from_another_thread(adapter)
        first.run(agent.hooks.invoke_callbacks, after)
        second.run(agent.hooks.invoke_callbacks, after)

        assert mock_client.traces.upload.call_count == 2
        assert first.run(adapter._get_run) is None
        assert second.run(adapter._get_run) is None
        assert _adapter_hooks(agent.hooks, adapter) == []

    def test_context_traces_again_after_reconnect(self, mock_client):
        """A long-lived task (a queue worker, say) that was mid-run at
        disconnect() must trace again after connect(): the run that was in
        flight must not be left behind in its context."""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        first = _make_agent(name="First")
        adapter.connect(target=first)

        def worker() -> None:
            first.hooks.invoke_callbacks(BeforeInvocationEvent(agent=first, invocation_state={}))
            _disconnect_from_another_thread(adapter)
            first.hooks.invoke_callbacks(AfterInvocationEvent(agent=first, invocation_state={}, result=_make_result()))

            second = _make_agent(name="Second")
            adapter.connect(target=second)
            second.hooks.invoke_callbacks(BeforeInvocationEvent(agent=second, invocation_state={}))
            second.hooks.invoke_callbacks(
                AfterInvocationEvent(agent=second, invocation_state={}, result=_make_result())
            )

        contextvars.copy_context().run(worker)

        assert mock_client.traces.upload.call_count == 2
        names = [e["payload"].get("agent_name") for e in find_events(uploaded["events"], "agent.input")]
        assert names == ["First", "Second"]
        adapter.disconnect()

    def test_reconnect_while_a_run_is_in_flight_keeps_one_set_of_hooks(self, mock_client):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()
        adapter.connect(target=agent)

        def invocation() -> None:
            agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent, invocation_state={}))
            _disconnect_from_another_thread(adapter)
            adapter.connect(target=agent)  # the hooks never left this agent
            assert len(_adapter_hooks(agent.hooks, adapter)) == 7
            agent.hooks.invoke_callbacks(AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result()))

        contextvars.copy_context().run(invocation)

        assert mock_client.traces.upload.call_count == 1
        assert len(_adapter_hooks(agent.hooks, adapter)) == 7  # attached again, so they stay
        adapter.disconnect()
        assert _adapter_hooks(agent.hooks, adapter) == []

    def test_agent_detached_mid_run_stays_detached_after_connect(self, mock_client):
        """connect() without a target doesn't re-attach an agent disconnect()
        detached. While the agent's earlier run is still finishing (so its
        hooks are still there), a new invocation of it isn't traced."""
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        agent = _make_agent()
        agent.hooks.add_hook(adapter)
        before = BeforeInvocationEvent(agent=agent, invocation_state={})
        after = AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result())

        first, second = contextvars.copy_context(), contextvars.copy_context()
        first.run(agent.hooks.invoke_callbacks, before)
        _disconnect_from_another_thread(adapter)
        adapter.connect()
        second.run(agent.hooks.invoke_callbacks, before)
        second.run(agent.hooks.invoke_callbacks, after)
        assert mock_client.traces.upload.call_count == 0

        first.run(agent.hooks.invoke_callbacks, after)  # the earlier run finishes and uploads
        assert mock_client.traces.upload.call_count == 1
        assert _adapter_hooks(agent.hooks, adapter) == []
        adapter.disconnect()

    def test_hook_called_after_its_removal_starts_no_run(self, mock_client):
        """strands lists an event's hooks when dispatch begins, so a hook
        removed mid-dispatch is still called that once. Called for an agent
        the adapter is no longer attached to, it must not start a run: the
        AfterInvocation hook is gone, so the run would be stranded."""
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent()

        def toggle(event):
            _disconnect_from_another_thread(adapter)
            adapter.connect()  # tracing back on, but not for this agent

        agent.hooks.add_callback(BeforeInvocationEvent, toggle)  # runs before the adapter's hook
        adapter.connect()
        agent.hooks.add_hook(adapter)

        def invocation() -> Any:
            agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent, invocation_state={}))
            agent.hooks.invoke_callbacks(AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result()))
            return adapter._get_run()

        assert contextvars.copy_context().run(invocation) is None
        assert mock_client.traces.upload.call_count == 0
        adapter.disconnect()

    def test_attaching_twice_registers_hooks_once(self, mock_client):
        # Built with hooks=[adapter], then also passed to connect(target=...).
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        agent = _make_agent()
        agent.hooks.add_hook(adapter)

        adapter.connect(target=agent)

        assert len(_adapter_hooks(agent.hooks, adapter)) == 7
        adapter.disconnect()
        assert _adapter_hooks(agent.hooks, adapter) == []

    def test_attached_before_connect_warns_and_traces_once_connected(self, mock_client, caplog):
        adapter = StrandsAdapter(mock_client)
        agent = _make_agent(name="Early")

        with caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
            agent.hooks.add_hook(adapter)  # Agent(hooks=[adapter]) before adapter.connect()
            # Said once per adapter, not once per agent: a service that keeps
            # building per-request agents while tracing is off isn't flooded.
            _make_agent(name="AlsoEarly").hooks.add_hook(adapter)
        assert len(_adapter_warnings(caplog)) == 1

        def invoke() -> None:
            agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent, invocation_state={}))
            agent.hooks.invoke_callbacks(AfterInvocationEvent(agent=agent, invocation_state={}, result=_make_result()))

        contextvars.copy_context().run(invoke)
        assert mock_client.traces.upload.call_count == 0  # not connected: not traced

        adapter.connect()
        contextvars.copy_context().run(invoke)
        assert mock_client.traces.upload.call_count == 1
        adapter.disconnect()

    def test_adapter_does_not_keep_registries_alive(self, mock_client):
        """A long-lived adapter passed as hooks=[adapter] to per-request agents
        must not hold each agent's hook registry in memory until disconnect()."""
        adapter = StrandsAdapter(mock_client)
        adapter.connect()
        registry = HookRegistry()
        registry.add_hook(adapter)
        ref = weakref.ref(registry)

        del registry
        gc.collect()

        assert ref() is None
        adapter.disconnect()


# ---------------------------------------------------------------------------
# Honest graph contract (Lever A) — LAY agent-graph
# ---------------------------------------------------------------------------


class TestHonestGraphContract:
    """The LayerLens app builds the trace's agent GRAPH from producer-DECLARED
    honest identity (``payload.agent_name``) and ``agent.handoff`` edges.

    Strands stamps every unnamed agent with the framework default
    ``"Strands Agents"`` (``strands.agent.agent._DEFAULT_AGENT_NAME``) — a
    generic literal on the identity denylist. Surfacing it as ``agent_name``
    is a fabricated node. This adapter must emit ``agent_name`` ONLY when the
    developer set a real, distinctive name; otherwise the payload stays blank
    (an honest ``—`` in the Agent column). A Strands *swarm* handoff is the
    developer calling the built-in ``handoff_to_agent(agent_name, message)``
    tool — an honest, producer-declared transition the adapter must surface as
    an ``agent.handoff`` edge.
    """

    def test_named_agent_carries_honest_name_on_input_output_and_model(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent(name="ResearchAgent", model_id="claude-sonnet")
        _simulate_invocation(adapter, agent, model_tokens={"input": 10, "output": 5})

        events = uploaded["events"]
        assert find_event(events, "agent.input")["payload"]["agent_name"] == "ResearchAgent"
        assert find_event(events, "agent.output")["payload"]["agent_name"] == "ResearchAgent"
        # model.invoke must ALSO carry the honest node identity so the graph
        # engine can attribute the model call to the right agent node.
        assert find_event(events, "model.invoke")["payload"]["agent_name"] == "ResearchAgent"

        adapter.disconnect()

    def test_swarm_handoff_tool_emits_agent_handoff_edge(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        adapter.connect()

        # A real Strands swarm handoff: the "router" agent calls the built-in
        # handoff_to_agent tool to pass control to the "billing" agent.
        agent = _make_agent(name="router")
        _simulate_invocation(
            adapter,
            agent,
            tool_calls=[
                {
                    "name": "handoff_to_agent",
                    "id": "h-1",
                    "input": {"agent_name": "billing", "message": "handle the refund"},
                }
            ],
        )

        ho = find_event(uploaded["events"], "agent.handoff")
        assert ho["payload"]["from_agent"] == "router"
        assert ho["payload"]["to_agent"] == "billing"

        adapter.disconnect()

    def test_default_framework_name_renders_verbatim_ateam_parity(self, mock_client):
        """ateam parity (#3): an unnamed Strands agent carries the framework default
        ``"Strands Agents"``, which is now surfaced VERBATIM as agent_name so the
        trace renders like ateam, instead of a blank column. (Handoff endpoints stay
        on the honest helper — see _emit_handoff.)"""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client)
        adapter.connect()

        agent = _make_agent(name="Strands Agents", model_id="claude-sonnet")
        _simulate_invocation(adapter, agent, model_tokens={"input": 10, "output": 5})

        events = uploaded["events"]
        assert find_event(events, "agent.input")["payload"].get("agent_name") == "Strands Agents"
        assert find_event(events, "agent.output")["payload"].get("agent_name") == "Strands Agents"

    def test_no_handoff_for_unnamed_source_agent(self, mock_client):
        """A handoff whose source agent has no honest name must NOT fabricate a
        ``from_agent`` — an honest edge needs both honest endpoints."""
        uploaded = capture_framework_trace(mock_client)
        adapter = StrandsAdapter(mock_client, capture_config=CaptureConfig(capture_content=True))
        adapter.connect()

        agent = _make_agent(name="Strands Agents")  # framework default → generic
        _simulate_invocation(
            adapter,
            agent,
            tool_calls=[
                {
                    "name": "handoff_to_agent",
                    "id": "h-2",
                    "input": {"agent_name": "billing", "message": "go"},
                }
            ],
        )

        assert find_events(uploaded["events"], "agent.handoff") == []

        adapter.disconnect()
