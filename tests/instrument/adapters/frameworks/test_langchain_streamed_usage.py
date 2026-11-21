"""A streamed LangChain model call records its tokens and its cost.

The handler read token usage and the model name only from ``LLMResult.llm_output``. A streamed call
has none: LangChain merges the chunks into one message, and the usage is that message's
``usage_metadata`` (``ChatOpenAI(stream_usage=True)`` and every model that reports usage the
standard way), the model name its ``response_metadata``. So ``.stream()`` recorded a model.invoke
without tokens or model, and no cost.record, where ``.invoke()`` recorded both (docs E2E 2026-11-10,
L7 AD-05); LangGraph inherited it.
"""

from __future__ import annotations

from typing import Any, List, Iterator, Optional

import pytest
from langchain_core.outputs import ChatResult, ChatGeneration, ChatGenerationChunk
from langchain_core.messages import AIMessage, BaseMessage, AIMessageChunk
from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.chat_models import BaseChatModel

from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

from .conftest import find_event, find_events, capture_framework_trace

USAGE = {
    "input_tokens": 10,
    "output_tokens": 3,
    "total_tokens": 13,
    "input_token_details": {"cache_read": 4},
    "output_token_details": {"reasoning": 2},
}


class _StreamingChat(BaseChatModel):
    """Streams "Hel", "lo" and then the usage, as ChatOpenAI(stream_usage=True) does."""

    @property
    def _llm_type(self) -> str:
        return "fake-streaming-chat"

    def _generate(
        self, messages: List[BaseMessage], stop: Optional[List[str]] = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        message = AIMessage(content="Hello", usage_metadata=USAGE, response_metadata={"model_name": "gpt-4o-mini"})  # type: ignore[arg-type]
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self, messages: List[BaseMessage], stop: Optional[List[str]] = None, run_manager: Any = None, **kw: Any
    ) -> Iterator[ChatGenerationChunk]:
        for text in ("Hel", "lo"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata=USAGE,  # type: ignore[arg-type]
                response_metadata={"model_name": "gpt-4o-mini", "finish_reason": "stop"},
            )
        )


def _recorded(mock_client: Any, read: str) -> List[Any]:
    uploaded = capture_framework_trace(mock_client)
    handler = LangChainCallbackHandler(mock_client, capture_config=CaptureConfig(capture_content=True))
    chain = RunnableLambda(lambda question: question) | _StreamingChat()
    config: Any = {"callbacks": [handler]}
    if read == "stream":
        assert "".join(str(c.content) for c in chain.stream("hi", config=config)) == "Hello"
    else:
        assert chain.invoke("hi", config=config).content == "Hello"
    events: List[Any] = uploaded["events"]
    return events


@pytest.mark.parametrize("read", ["stream", "invoke"])
def test_the_call_records_its_tokens_model_and_cost(mock_client: Any, read: str) -> None:
    events = _recorded(mock_client, read)
    invoke = find_event(events, "model.invoke")["payload"]
    assert invoke["model"] == "gpt-4o-mini"
    assert (invoke["tokens_prompt"], invoke["tokens_completion"], invoke["tokens_total"]) == (10, 3, 13)
    costs = find_events(events, "cost.record")
    assert len(costs) == 1
    cost = costs[0]["payload"]
    assert (cost["model"], cost["tokens_prompt"], cost["tokens_completion"]) == ("gpt-4o-mini", 10, 3)
    assert cost["cost_usd"] > 0
    if read == "stream":
        # How many chunks LangChain hands out (and so counts) differs between its versions.
        assert invoke["streaming"] is True and invoke["streamed_chunks"] >= 3 and invoke["ttft_ms"] >= 0
        assert invoke["output_message"] == "Hello"
