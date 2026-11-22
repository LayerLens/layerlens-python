"""Recorded-real-response replay for the Haystack framework (LAY-3614).

Drives a REAL Haystack ``Pipeline`` whose ``OpenAIChatGenerator`` is backed by an
``httpx.MockTransport`` serving the captured OpenAI chat.completion response,
with the real ``HaystackAdapter`` attached. This exercises the full path — real
provider response shape -> real Haystack pipeline/component tracing spans -> real
adapter -> emitted events — which the unit doubles (hand-built tag dicts) never
combine with a real provider body.

The generator is the chat one because haystack-ai 3.0 removed the text
``OpenAIGenerator`` (``haystack.components.generators.openai``);
``OpenAIChatGenerator`` exists on the 2.x line too, so this replay runs on both.

The adapter instruments Haystack by swapping in its own
``haystack.tracing.tracer.actual_tracer``, so the natural run unit is a
``Pipeline.run()`` (a bare generator never opens a ``haystack.pipeline.run``
span, so nothing would flush). The pipeline's component span carries the real
chat generator output, ``{"replies": [ChatMessage]}``: unlike the text
generator's output there is no top-level ``meta`` list, so the model id, usage
and finish_reason the adapter reports must come off the reply's own
``ChatMessage.meta``.

The MockTransport is injected through Haystack's documented, serialization-safe
``http_client_kwargs=`` seam: ``init_http_client`` forwards the dict to
``httpx.Client(**kwargs)``, and ``httpx.Client`` accepts ``transport=`` — so the
real OpenAI SDK client inside the generator does its real routing +
deserialization against the recorded body.

The strong tell that the real provider shape flowed through: ``model.invoke``
reports ``gpt-4o-mini-2024-07-18`` (the model echoed in the recorded *response*
body), not the ``gpt-4o-mini`` we *requested* — the adapter read it off the real
parsed reply ``meta``, not off our config.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import httpx
import pytest

from tests._optional_deps import importorskip

importorskip("haystack")  # skips in the base venv (not installed there)

from haystack import Pipeline  # noqa: E402
from haystack.utils import Secret
from haystack.dataclasses import ChatMessage
from haystack.components.generators.chat import FallbackChatGenerator
from haystack.components.generators.chat.openai import OpenAIChatGenerator

import layerlens.instrument.adapters.frameworks.haystack as _mod  # noqa: E402
from layerlens.instrument.adapters.frameworks.haystack import HaystackAdapter

from .conftest import find_event, find_events, capture_framework_trace
from ..._recorded import load_recorded, mock_transport


@pytest.fixture(autouse=True)
def _arm_haystack_flag():
    # test_haystack.py runs in the same matrix pytest process; re-arm the module
    # flag so connect() sees the truthy value the real installed import sets,
    # independent of cross-file run order (and of any state the legacy file left).
    prev = _mod._HAS_HAYSTACK
    _mod._HAS_HAYSTACK = True
    yield
    _mod._HAS_HAYSTACK = prev


def _pipeline(fixture: Dict[str, Any]) -> Tuple[Pipeline, List[httpx.Request]]:
    transport, requests = mock_transport(fixture)
    # Haystack's OpenAIChatGenerator builds its OpenAI client via init_http_client,
    # which calls httpx.Client(**http_client_kwargs); httpx.Client accepts a
    # custom transport, so this is the public seam for the MockTransport.
    generator = OpenAIChatGenerator(
        api_key=Secret.from_token("test-key"),
        model="gpt-4o-mini",
        http_client_kwargs={"transport": transport},
    )
    pipe = Pipeline()
    pipe.add_component("llm", generator)
    return pipe, requests


class TestHaystackRecorded:
    def test_pipeline_over_recorded_openai(self, mock_client):
        fixture = load_recorded("openai", "default")
        uploaded = capture_framework_trace(mock_client)

        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        pipe, requests = _pipeline(fixture)
        result = pipe.run({"llm": {"messages": [ChatMessage.from_user("Reply with exactly: pong")]}})
        adapter.disconnect()

        # The fixture is a recorded chat.completions response; the replay is only
        # meaningful if the chat generator called that same endpoint, once.
        assert [r.url.path for r in requests] == ["/v1/chat/completions"]
        assert result["llm"]["replies"][0].text == "pong"

        events = uploaded["events"]

        # The real component span carries the real reply's meta parsed off the
        # recorded chat.completion body — model id is the *response* model, not
        # the requested one.
        mi = find_event(events, "model.invoke")
        assert mi["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert mi["payload"]["component_type"] == "OpenAIChatGenerator"
        assert mi["payload"]["tokens_prompt"] == 12
        assert mi["payload"]["tokens_completion"] == 1
        assert mi["payload"]["tokens_total"] == 13
        assert mi["payload"]["finish_reason"] == "stop"

        # cost.record echoes the same real usage from the recorded response.
        cost = find_event(events, "cost.record")
        assert cost["payload"]["framework"] == "haystack"
        assert cost["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert cost["payload"]["tokens_prompt"] == 12
        assert cost["payload"]["tokens_completion"] == 1
        assert cost["payload"]["tokens_total"] == 13

        # The pipeline lifecycle frames the run with its own input/output pair.
        out = find_event(events, "agent.output")
        assert out["payload"]["framework"] == "haystack"

    def test_fallback_chat_generator_over_recorded_openai(self, mock_client):
        """``FallbackChatGenerator`` returns ``{"replies": [ChatMessage], "meta": dict}``:
        its top-level ``meta`` is a dict of fallback bookkeeping, not the text
        generator's per-reply list, so the call's model and usage must still come
        from the reply — once, on the fallback component's own model.invoke."""
        fixture = load_recorded("openai", "default")
        uploaded = capture_framework_trace(mock_client)

        transport, _ = mock_transport(fixture)
        fallback = FallbackChatGenerator(
            chat_generators=[
                OpenAIChatGenerator(
                    api_key=Secret.from_token("test-key"),
                    model="gpt-4o-mini",
                    http_client_kwargs={"transport": transport},
                )
            ]
        )
        adapter = HaystackAdapter(mock_client)
        adapter.connect()
        pipe = Pipeline()
        pipe.add_component("llm", fallback)
        result = pipe.run({"llm": {"messages": [ChatMessage.from_user("Reply with exactly: pong")]}})
        adapter.disconnect()

        assert result["llm"]["replies"][0].text == "pong"

        events = uploaded["events"]
        mi = find_event(events, "model.invoke")
        assert mi["payload"]["component_type"] == "FallbackChatGenerator"
        assert mi["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert mi["payload"]["tokens_prompt"] == 12
        assert mi["payload"]["tokens_completion"] == 1
        assert mi["payload"]["tokens_total"] == 13
        assert mi["payload"]["finish_reason"] == "stop"

        # One provider call -> one cost.record (the 13 tokens are not counted twice).
        costs = find_events(events, "cost.record")
        assert len(costs) == 1
        assert costs[0]["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert costs[0]["payload"]["tokens_total"] == 13
