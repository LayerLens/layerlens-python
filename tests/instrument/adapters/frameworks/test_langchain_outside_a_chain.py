"""The LangChain handler records a chat model, an LLM, a tool or a retriever called outside a chain.

Only a chain's start began a trace, so a handler passed to a bare ``ChatOpenAI(...).invoke`` or to a
tool called on its own, outside any ``trace`` / ``trace_context`` block, recorded nothing and said
nothing (docs E2E 2026-11-10, L3 AD-22), although the docs say the handler can be passed to any
component that takes callbacks. Such a call now records its own trace, as a chain does; inside a
``trace`` / ``trace_context`` block it goes on recording into that block's trace.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest
from langchain_core.tools import tool
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.language_models.llms import LLM
from langchain_core.language_models.chat_models import BaseChatModel

from layerlens.instrument import trace_context
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

from .conftest import find_event, find_events, capture_framework_trace


class _Chat(BaseChatModel):
    """A chat model that answers "hi" and reports its usage as ChatOpenAI does."""

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def _generate(
        self, messages: List[BaseMessage], stop: Optional[List[str]] = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="hi"), generation_info={"finish_reason": "stop"})],
            llm_output={
                "model_name": "gpt-4o-mini",
                "token_usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
            },
        )


class _Completion(LLM):
    @property
    def _llm_type(self) -> str:
        return "fake-llm"

    def _call(self, prompt: str, stop: Optional[List[str]] = None, run_manager: Any = None, **kw: Any) -> str:
        return "a completion"


class _Docs(BaseRetriever):
    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> List[Document]:
        return [Document(page_content=f"about {query}")]


@tool
def get_weather(city: str) -> str:
    """The weather in a city."""
    return f"sunny in {city}"


def _handler(mock_client: Any) -> LangChainCallbackHandler:
    return LangChainCallbackHandler(mock_client, capture_config=CaptureConfig(capture_content=True))


def test_a_chat_model_called_on_its_own_is_recorded_with_its_tokens_and_cost(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    reply = _Chat().invoke("Say hi", config={"callbacks": [_handler(mock_client)]})
    assert reply.content == "hi"
    events = uploaded["events"]
    invoke = find_event(events, "model.invoke")["payload"]
    assert (invoke["model"], invoke["tokens_prompt"], invoke["tokens_completion"]) == ("gpt-4o-mini", 9, 3)
    assert invoke["output_message"] == "hi"
    cost = find_event(events, "cost.record")["payload"]
    assert cost["model"] == "gpt-4o-mini" and cost["cost_usd"] > 0
    assert mock_client.traces.upload.call_count == 1


def test_an_llm_called_on_its_own_is_recorded(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    assert _Completion().invoke("Complete this", config={"callbacks": [_handler(mock_client)]}) == "a completion"
    assert find_event(uploaded["events"], "model.invoke")["payload"]["output_message"] == "a completion"


def test_a_tool_called_on_its_own_is_recorded(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    assert get_weather.invoke({"city": "Paris"}, config={"callbacks": [_handler(mock_client)]}) == "sunny in Paris"
    events = uploaded["events"]
    assert find_event(events, "tool.call")["payload"]["name"] == "get_weather"
    assert find_event(events, "tool.result")["payload"]["output"] == "sunny in Paris"


def test_a_retriever_called_on_its_own_is_recorded(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    docs = _Docs().invoke("cats", config={"callbacks": [_handler(mock_client)]})
    assert [d.page_content for d in docs] == ["about cats"]
    result = find_event(uploaded["events"], "tool.result")["payload"]
    assert result["output"] == [{"page_content": "about cats", "metadata": {}}]


def test_each_call_on_its_own_is_its_own_trace(mock_client: Any) -> None:
    capture_framework_trace(mock_client)
    handler = _handler(mock_client)
    _Chat().invoke("one", config={"callbacks": [handler]})
    get_weather.invoke({"city": "Rome"}, config={"callbacks": [handler]})
    assert mock_client.traces.upload.call_count == 2


@pytest.mark.parametrize("call", ["chat model", "tool"])
def test_outside_a_block_the_call_is_its_own_trace_and_inside_one_it_joins_it(mock_client: Any, call: str) -> None:
    uploaded = capture_framework_trace(mock_client)
    handler = _handler(mock_client)

    def make_the_call() -> None:
        if call == "chat model":
            _Chat().invoke("Say hi", config={"callbacks": [handler]})
        else:
            get_weather.invoke({"city": "Paris"}, config={"callbacks": [handler]})

    make_the_call()
    assert mock_client.traces.upload.call_count == 1
    with trace_context(mock_client):
        make_the_call()
        assert mock_client.traces.upload.call_count == 1, "a call inside the block uploaded its own trace"
    assert mock_client.traces.upload.call_count == 2
    kind = "model.invoke" if call == "chat model" else "tool.result"
    assert len(find_events(uploaded["events"], kind)) == 2
