"""Recorded-real-response replay for the LangChain framework (LAY-3614).

Drives a REAL ``langchain_openai.ChatOpenAI`` over ``httpx.MockTransport``
serving the captured OpenAI response, through a real ``prompt | llm`` chain with
the real ``LangChainCallbackHandler`` attached. This exercises the full path —
real provider response shape -> real LangChain callback objects -> real adapter
-> emitted events — which the matrix layer (fake models) and the hand-built unit
doubles never combine. Reuses the openai corpus fixture (the framework consumes
the provider's response).
"""

from __future__ import annotations

import httpx

from tests._optional_deps import importorskip

importorskip("langchain_openai")  # skips in envs without langchain-openai

from langchain_openai import ChatOpenAI  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402

from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

from .conftest import find_event, capture_framework_trace
from ..._recorded import load_recorded, mock_transport


def _chat(fixture):
    transport, _ = mock_transport(fixture)
    return ChatOpenAI(model="gpt-4o-mini", api_key="test-key", http_client=httpx.Client(transport=transport))


class TestLangChainRecorded:
    def test_chain_over_recorded_openai(self, mock_client):
        fixture = load_recorded("openai", "default")
        uploaded = capture_framework_trace(mock_client)
        handler = LangChainCallbackHandler(mock_client)

        chain = ChatPromptTemplate.from_messages([("user", "{q}")]) | _chat(fixture)
        result = chain.invoke({"q": "Reply with exactly: pong"}, config={"callbacks": [handler]})
        assert result.content == "pong"

        events = uploaded["events"]
        # The real LangChain callback objects carry the real OpenAI usage/model.
        mi = find_event(events, "model.invoke")
        assert mi["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert mi["payload"]["tokens_prompt"] == 12
        assert mi["payload"]["tokens_completion"] == 1
        assert mi["payload"]["tokens_total"] == 13
        assert mi["payload"]["finish_reason"] == "stop"

        # LangChain emits framework-flat token accounting (no provider pricing).
        cost = find_event(events, "cost.record")
        assert cost["payload"]["framework"] == "langchain"
        assert cost["payload"]["tokens_total"] == 13

    def test_chain_over_recorded_tool_call(self, mock_client):
        # The default-fixture test above never exercises on_llm_end's tool_calls
        # extraction (langchain.py:363-381 -> 435-452): its response has no
        # tool_calls. test_langchain.py::TestModelRequestedToolCalls hand-builds the
        # message each langchain-openai version returns; this drives a REAL
        # tool-calling OpenAI response through the real ChatOpenAI parser + the real
        # handler, so it follows whatever shape the installed version produces.
        fixture = load_recorded("openai", "tool_call")
        uploaded = capture_framework_trace(mock_client)
        # capture_content=True so the tool-call arguments (content) are retained;
        # id/tool_name/model are structural and survive either way.
        handler = LangChainCallbackHandler(mock_client, capture_config=CaptureConfig(capture_content=True))

        chain = ChatPromptTemplate.from_messages([("user", "{q}")]) | _chat(fixture)
        chain.invoke({"q": "What's the weather in Paris?"}, config={"callbacks": [handler]})

        events = uploaded["events"]
        mi = find_event(events, "model.invoke")
        assert mi["payload"]["finish_reason"] == "tool_calls"
        assert mi["payload"]["tokens_total"] == 68
        assert mi["payload"]["model"] == "gpt-4o-mini-2024-07-18"

        # The bite: a real tool.call reconstructed from the response's tool_calls.
        # VACUITY CONTROL — the default-fixture test emits NO tool.call, so this
        # find_event raises unless the recorded tool_calls shape flowed through
        # on_llm_end's extraction (the only coverage of that path).
        tc = find_event(events, "tool.call")
        assert tc["payload"]["tool_name"] == "get_weather"
        assert tc["payload"]["id"] == "call_N0cYZsPSiRm7o3khCvuy6J3M"
        # The wire carries '{"city":"Paris"}'. langchain-openai < 1.7 hands the adapter
        # that string, 1.7+ the parsed dict; the event carries the object either way.
        assert tc["payload"]["arguments"] == {"city": "Paris"}
        assert tc["payload"]["model"] == "gpt-4o-mini-2024-07-18"
