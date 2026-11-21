"""Recorded-real-response replay for haystack ``Agent`` runs and for the components
that call an LLM through haystack 3's shared chat-generator span.

Drives REAL haystack objects (``Agent``, ``Pipeline``, ``OpenAIChatGenerator``,
``FallbackChatGenerator``, ``LLMMessagesRouter``, ``LLMMetadataExtractor``, a
real ``@tool``) whose OpenAI client is backed by an ``httpx.MockTransport``
serving the captured OpenAI chat.completion bodies, with the real
``HaystackAdapter`` attached. An Agent run is two provider calls: the recorded
``get_weather`` tool call (``tool_call.json``, 54/14/68 tokens) and, once the real
tool has run, the recorded answer (``default.json``, 12/1/13 tokens).

haystack-ai 3 traces an Agent with ``haystack.agent.run`` / ``.step`` /
``.step.llm`` / ``.step.tool`` spans, and the LLM call a router, ranker,
extractor, evaluator or expander makes inside its own ``run()`` with
``haystack.chat_generator.run``. haystack-ai 2.x has only ``haystack.agent.run``
of these (its Agent runs the generator and the ToolInvoker as pipeline component
spans), so the tests that need the other spans are marked haystack-3-only; the
rest hold on both lines.

The strong tell that the real provider shape flowed through: every model call
reports ``gpt-4o-mini-2024-07-18``, the model echoed in the recorded *response*
body, not the ``gpt-4o-mini`` we requested.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List, Tuple

import httpx
import pytest

from tests._optional_deps import importorskip

importorskip("haystack")  # skips in the base venv (not installed there)

import haystack  # noqa: E402
from haystack import Document, Pipeline  # noqa: E402
from haystack.tools import tool  # noqa: E402
from haystack.utils import Secret  # noqa: E402
from haystack.dataclasses import ChatMessage  # noqa: E402
from haystack.components.agents import Agent  # noqa: E402
from haystack.components.routers import LLMMessagesRouter  # noqa: E402
from haystack.components.extractors import LLMMetadataExtractor  # noqa: E402
from haystack.components.generators.chat import FallbackChatGenerator  # noqa: E402
from haystack.components.generators.chat.openai import OpenAIChatGenerator  # noqa: E402

import layerlens.instrument.adapters.frameworks.haystack as _mod  # noqa: E402
from layerlens.instrument._capture_config import CaptureConfig  # noqa: E402
from layerlens.instrument.adapters.frameworks.haystack import HaystackAdapter  # noqa: E402

from .conftest import (  # noqa: E402
    find_event,
    find_events,
    model_calls,
    assert_one_closed_tree,
    capture_framework_trace,
    assert_priced_once_per_call,
)
from ..._recorded import load_recorded, mock_transport  # noqa: E402

_HAYSTACK_3 = int(haystack.__version__.split(".")[0]) >= 3
requires_haystack_3 = pytest.mark.skipif(
    not _HAYSTACK_3,
    reason="haystack-ai 2.x opens no haystack.agent.step* or haystack.chat_generator.run spans",
)

_QUESTION = "What's the weather in Paris?"
_MODEL = "gpt-4o-mini-2024-07-18"
_TOOL_CALL = (_MODEL, 54, 14, 68, "tool_calls")
_ANSWER = (_MODEL, 12, 1, 13, "stop")


@pytest.fixture(autouse=True)
def _arm_haystack_flag():
    # test_haystack.py runs in the same matrix pytest process; re-arm the module
    # flag so connect() sees the truthy value the real installed import sets.
    prev = _mod._HAS_HAYSTACK
    _mod._HAS_HAYSTACK = True
    yield
    _mod._HAS_HAYSTACK = prev


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"sunny in {city}"


def _replay(*scenarios: str) -> Tuple[httpx.MockTransport, List[httpx.Request]]:
    """A MockTransport answering request N with the recorded body of ``scenarios[N]``."""
    interactions = [i for s in scenarios for i in load_recorded("openai", s)["interactions"]]
    return mock_transport({"interactions": interactions})


def _generator(transport: httpx.BaseTransport, **kwargs: Any) -> OpenAIChatGenerator:
    # OpenAIChatGenerator forwards http_client_kwargs to httpx.Client(**kwargs):
    # the public seam for the MockTransport (see test_haystack_recorded.py).
    return OpenAIChatGenerator(
        api_key=Secret.from_token("test-key"),
        model="gpt-4o-mini",
        http_client_kwargs={"transport": transport},
        **kwargs,
    )


def _failing_transport() -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "upstream overloaded", "type": "server_error"}})

    return httpx.MockTransport(_handler)


def _adapter(mock_client: Any, capture_content: bool = False) -> HaystackAdapter:
    adapter = HaystackAdapter(mock_client, capture_config=CaptureConfig(capture_content=capture_content))
    adapter.connect()
    return adapter


def _weather_agent(transport: httpx.BaseTransport, **kwargs: Any) -> Agent:
    return Agent(chat_generator=_generator(transport), tools=[get_weather], **kwargs)


class TestAgentOnItsOwn:
    def test_model_calls_are_priced_and_the_run_is_framed(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("tool_call", "default")
        adapter = _adapter(mock_client, capture_content=True)
        result = _weather_agent(transport).run(messages=[ChatMessage.from_user(_QUESTION)])
        adapter.disconnect()

        assert [r.url.path for r in requests] == ["/v1/chat/completions"] * 2
        assert result["last_message"].text == "pong"
        assert mock_client.traces.upload.call_count == 1

        events = uploaded["events"]
        assert model_calls(events) == [_TOOL_CALL, _ANSWER]
        assert_priced_once_per_call(events, [68, 13], _MODEL)

        root = assert_one_closed_tree(events)
        inp = find_event(events, "agent.input")
        out = find_event(events, "agent.output")
        cfg = find_event(events, "environment.config")
        assert [e["span_id"] for e in (inp, cfg, out)] == [root, root, root]
        assert inp["payload"]["input"]["messages"][0]["content"] == [{"text": _QUESTION}]
        assert out["payload"]["steps_taken"] == 2
        assert cfg["payload"]["tools"] == ["get_weather"]
        assert cfg["payload"]["max_steps"] == 100
        assert cfg["payload"]["exit_conditions"] == ["text"]
        # Nothing names an Agent that runs on its own.
        assert not [e for e in events if "agent_name" in e["payload"]]

    @requires_haystack_3
    def test_steps_and_the_one_tool_call(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        transport, _ = _replay("tool_call", "default")
        adapter = _adapter(mock_client, capture_content=True)
        _weather_agent(transport).run(messages=[ChatMessage.from_user(_QUESTION)])
        adapter.disconnect()

        events = uploaded["events"]
        root = assert_one_closed_tree(events)
        steps = find_events(events, "agent.step")
        assert [s["payload"]["step"] for s in steps] == [0, 1]
        assert all(s["parent_span_id"] == root for s in steps)
        assert [i["parent_span_id"] for i in find_events(events, "model.invoke")] == [s["span_id"] for s in steps]

        calls = find_events(events, "tool.call")
        results = find_events(events, "tool.result")
        assert [c["payload"]["tool_name"] for c in calls] == ["get_weather"]
        assert [r["payload"]["tool_name"] for r in results] == ["get_weather"]
        assert calls[0]["payload"]["input"] == {"city": "Paris"}
        assert results[0]["payload"]["output"] == "sunny in Paris"
        assert calls[0]["parent_span_id"] == steps[0]["span_id"]

    def test_run_async(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        transport, _ = _replay("tool_call", "default")
        adapter = _adapter(mock_client)
        agent = _weather_agent(transport)
        result = asyncio.run(agent.run_async(messages=[ChatMessage.from_user(_QUESTION)]))
        adapter.disconnect()

        assert result["last_message"].text == "pong"
        assert mock_client.traces.upload.call_count == 1
        events = uploaded["events"]
        assert model_calls(events) == [_TOOL_CALL, _ANSWER]
        assert_priced_once_per_call(events, [68, 13], _MODEL)
        assert_one_closed_tree(events)

    @requires_haystack_3
    # CompactionHook is marked experimental in haystack-ai 3.4.
    @pytest.mark.filterwarnings("ignore::haystack.utils.experimental.ExperimentalWarning")
    def test_compaction_hook_runs_with_the_adapter_connected(self, mock_client):
        """``CompactionHook`` tags the current span with ``set_tags`` before every
        LLM step; the adapter's span must take it, or the Agent run fails."""
        from haystack.hooks.compaction import CompactionHook, SlidingWindowCompactor

        uploaded = capture_framework_trace(mock_client)
        transport, _ = _replay("tool_call", "default")
        hook = CompactionHook(compactor=SlidingWindowCompactor(), context_window=400_000)
        adapter = _adapter(mock_client)
        result = _weather_agent(transport, hooks={"before_llm": [hook]}).run(
            messages=[ChatMessage.from_user(_QUESTION)]
        )
        adapter.disconnect()

        assert result["last_message"].text == "pong"
        assert model_calls(uploaded["events"]) == [_TOOL_CALL, _ANSWER]

    def test_concurrent_run_async_calls_are_separate_traces(self, mock_client):
        """Two ``Agent.run_async()`` calls in flight on one event loop each upload their
        own closed trace with their own model call. With the current span kept per
        thread, the second Agent opened its span under the first one's, joined that
        run, and its model call was lost: one upload, one model call."""
        body = load_recorded("openai", "default")["interactions"][0]["response"]["json"]

        def agent(delay: float) -> Agent:
            async def handler(request: httpx.Request) -> httpx.Response:
                await asyncio.sleep(delay)  # hand the loop to the other Agent mid-call
                return httpx.Response(200, json=body)

            return Agent(chat_generator=_generator(httpx.MockTransport(handler)))

        uploads: List[Dict[str, Any]] = []

        def _capture(path: str) -> None:
            with open(path) as f:
                uploads.append(json.load(f)[0])

        mock_client.traces.upload.side_effect = _capture
        adapter = _adapter(mock_client, capture_content=True)

        async def both() -> List[Dict[str, Any]]:
            return await asyncio.gather(
                agent(0.05).run_async(messages=[ChatMessage.from_user("first")]),
                agent(0.01).run_async(messages=[ChatMessage.from_user("second")]),
            )

        results = asyncio.run(both())
        adapter.disconnect()

        assert [r["last_message"].text for r in results] == ["pong", "pong"]
        assert len(uploads) == 2, f"expected one upload per run_async(), got {len(uploads)}"
        assert len({u["trace_id"] for u in uploads}) == 2
        questions = []
        for upload in uploads:
            events = upload["events"]
            assert model_calls(events) == [_ANSWER]
            root = assert_one_closed_tree(events)
            inp = find_event(events, "agent.input")
            assert inp["span_id"] == root
            questions.append(inp["payload"]["input"]["messages"][0]["content"][0]["text"])
        assert sorted(questions) == ["first", "second"]


class TestAgentInPipeline:
    def _run(self, mock_client: Any, name: str) -> List[Dict[str, Any]]:
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("tool_call", "default")
        adapter = _adapter(mock_client)
        pipe = Pipeline()
        pipe.add_component(name, _weather_agent(transport))
        result = pipe.run({name: {"messages": [ChatMessage.from_user(_QUESTION)]}})
        adapter.disconnect()

        assert [r.url.path for r in requests] == ["/v1/chat/completions"] * 2
        assert result[name]["last_message"].text == "pong"
        assert mock_client.traces.upload.call_count == 1
        return uploaded["events"]

    def test_model_calls_are_priced_and_the_agent_hangs_under_its_component(self, mock_client):
        events = self._run(mock_client, "weather_agent")

        assert model_calls(events) == [_TOOL_CALL, _ANSWER]
        assert_priced_once_per_call(events, [68, 13], _MODEL)

        root = assert_one_closed_tree(events)
        component = [c for c in find_events(events, "tool.call") if c["payload"]["tool_name"] == "weather_agent"]
        assert len(component) == 1
        nested = [e for e in find_events(events, "agent.input") if e["span_id"] != root]
        assert len(nested) == 1
        assert nested[0]["parent_span_id"] == component[0]["span_id"]
        assert nested[0]["payload"]["agent_name"] == "weather_agent"
        agent_cfg = [c for c in find_events(events, "environment.config") if "tools" in c["payload"]]
        assert [c["payload"]["tools"] for c in agent_cfg] == [["get_weather"]]

    @requires_haystack_3
    def test_agent_calls_carry_the_component_name(self, mock_client):
        events = self._run(mock_client, "weather_agent")

        for event_type in ("model.invoke", "cost.record", "agent.step"):
            named = find_events(events, event_type)
            assert named and all(e["payload"].get("agent_name") == "weather_agent" for e in named), event_type
        # The agent's tool call is reported once, apart from the Agent component itself.
        calls = sorted(c["payload"]["tool_name"] for c in find_events(events, "tool.call"))
        assert calls == ["get_weather", "weather_agent"]
        weather = [c for c in find_events(events, "tool.call") if c["payload"]["tool_name"] == "get_weather"]
        assert weather[0]["payload"]["agent_name"] == "weather_agent"

    def test_generic_component_name_names_no_agent(self, mock_client):
        events = self._run(mock_client, "agent")

        assert model_calls(events) == [_TOOL_CALL, _ANSWER]
        assert_priced_once_per_call(events, [68, 13], _MODEL)
        assert not [e for e in events if "agent_name" in e["payload"]]
        agent_cfg = [c for c in find_events(events, "environment.config") if "tools" in c["payload"]]
        assert [c["payload"]["tools"] for c in agent_cfg] == [["get_weather"]]
        assert_one_closed_tree(events)


@requires_haystack_3
class TestChatGeneratorSpan:
    def test_router_call_is_recorded_once(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("default")
        adapter = _adapter(mock_client)
        router = LLMMessagesRouter(
            chat_generator=_generator(transport), output_names=["pong"], output_patterns=["pong"]
        )
        pipe = Pipeline()
        pipe.add_component("router", router)
        result = pipe.run({"router": {"messages": [ChatMessage.from_user("Reply with exactly: pong")]}})
        adapter.disconnect()

        assert len(requests) == 1
        assert result["router"]["chat_generator_text"] == "pong"
        events = uploaded["events"]
        assert model_calls(events) == [_ANSWER]
        assert_priced_once_per_call(events, [13], _MODEL)
        invoke = find_event(events, "model.invoke")
        assert invoke["payload"]["component_name"] == "router"
        assert invoke["payload"]["component_class"] == "OpenAIChatGenerator"
        assert invoke["parent_span_id"] == find_event(events, "tool.call")["span_id"]
        assert_one_closed_tree(events)

    def test_extractor_calls_on_worker_threads_are_recorded(self, mock_client):
        """``LLMMetadataExtractor`` makes one call per document on a thread pool that
        does not carry the caller's context."""
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("default", "default")
        adapter = _adapter(mock_client)
        extractor = LLMMetadataExtractor(
            prompt="Extract the entities from: {{ document.content }}",
            chat_generator=_generator(transport),
            max_workers=2,
        )
        pipe = Pipeline()
        pipe.add_component("extractor", extractor)
        pipe.run({"extractor": {"documents": [Document(content="Paris is in France."), Document(content="Rome.")]}})
        adapter.disconnect()

        assert len(requests) == 2
        assert mock_client.traces.upload.call_count == 1
        events = uploaded["events"]
        assert model_calls(events) == [_ANSWER, _ANSWER]
        assert_priced_once_per_call(events, [13, 13], _MODEL)
        assert all(i["payload"]["component_name"] == "extractor" for i in find_events(events, "model.invoke"))
        assert_one_closed_tree(events)

    def test_agent_with_a_fallback_chat_generator(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("tool_call", "default")
        adapter = _adapter(mock_client)
        agent = Agent(
            chat_generator=FallbackChatGenerator(chat_generators=[_generator(transport)]), tools=[get_weather]
        )
        result = agent.run(messages=[ChatMessage.from_user(_QUESTION)])
        adapter.disconnect()

        assert len(requests) == 2
        assert result["last_message"].text == "pong"
        events = uploaded["events"]
        assert model_calls(events) == [_TOOL_CALL, _ANSWER]
        assert_priced_once_per_call(events, [68, 13], _MODEL)
        assert_one_closed_tree(events)

    def test_agent_fallback_failed_attempts_hang_off_their_steps(self, mock_client):
        """Each step's first generator fails, the second answers: four provider
        calls, the two failures reported once each under their step."""
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("tool_call", "default")
        adapter = _adapter(mock_client)
        fallback = FallbackChatGenerator(
            chat_generators=[_generator(_failing_transport(), max_retries=0), _generator(transport)]
        )
        result = Agent(chat_generator=fallback, tools=[get_weather]).run(messages=[ChatMessage.from_user(_QUESTION)])
        adapter.disconnect()

        assert len(requests) == 2
        assert result["last_message"].text == "pong"
        events = uploaded["events"]
        assert_one_closed_tree(events)
        steps = [s["span_id"] for s in find_events(events, "agent.step")]
        invokes = find_events(events, "model.invoke")
        failed = [i for i in invokes if "error_type" in i["payload"]]
        answered = [i for i in invokes if "error_type" not in i["payload"]]
        assert [f["payload"]["error_type"] for f in failed] == ["InternalServerError", "InternalServerError"]
        assert [f["parent_span_id"] for f in failed] == steps
        assert model_calls(answered) == [_TOOL_CALL, _ANSWER]
        assert [a["parent_span_id"] for a in answered] == steps
        assert_priced_once_per_call(events, [68, 13], _MODEL)

    def test_fallback_failed_attempt_is_reported_once(self, mock_client):
        uploaded = capture_framework_trace(mock_client)
        transport, requests = _replay("default")
        adapter = _adapter(mock_client, capture_content=True)
        fallback = FallbackChatGenerator(
            chat_generators=[_generator(_failing_transport(), max_retries=0), _generator(transport)]
        )
        pipe = Pipeline()
        pipe.add_component("llm", fallback)
        result = pipe.run({"llm": {"messages": [ChatMessage.from_user("Reply with exactly: pong")]}})
        adapter.disconnect()

        assert result["llm"]["meta"]["total_attempts"] == 2
        assert len(requests) == 1
        events = uploaded["events"]
        failed, answered = find_events(events, "model.invoke")
        assert "upstream overloaded" in failed["payload"]["error"]
        assert failed["payload"]["error_type"] == "InternalServerError"  # the real openai exception
        assert failed["payload"]["component_class"] == "OpenAIChatGenerator"
        assert "tokens_total" not in failed["payload"]
        assert answered["payload"]["component_type"] == "FallbackChatGenerator"
        assert model_calls([answered]) == [_ANSWER]
        assert_priced_once_per_call(events, [13], _MODEL)
        assert_one_closed_tree(events)
