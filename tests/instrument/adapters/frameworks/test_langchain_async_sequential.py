"""Every LangChain run in an async task records its trace, not only the first one.

LangChain reports an async run's end (``on_chain_end``, ``on_llm_end``, ``on_retriever_end`` and their
errors, all ``@shielded``) from a task of its own, with a copy of the caller's context. The run ended
there, but the ContextVars its start had set in the caller's context stayed set. The caller's next
LangChain run in the same task found that ended run and its sealed collector, joined them, and
recorded nothing, without a warning (docs re-run 2026-11-10, L8 B04: one ``asyncio.run`` awaiting
``chain.ainvoke``, ``chain.astream``, ``llm.ainvoke``, ``llm.astream`` recorded the first only). Runs
in tasks of their own (``gather``, ``create_task``) were not affected. A run's start, which LangChain
calls in the caller's context, now puts back what the context held before an ended run; and
``get_trace_context()`` / ``inject_headers()`` never hand out a sealed trace's ids.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, List

from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from layerlens.instrument import trace, get_trace_context
from layerlens.instrument._w3c import inject_headers
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler


def _uploads(mock_client: Any) -> List[List[str]]:
    """The event types of each trace uploaded through ``mock_client``, in order."""
    traces: List[List[str]] = []

    def capture(path: str) -> Any:
        with open(path) as f:
            traces.append([e["event_type"] for e in json.load(f)[0]["events"]])
        return None

    mock_client.traces.upload.side_effect = capture
    return traces


def _handler(mock_client: Any) -> LangChainCallbackHandler:
    return LangChainCallbackHandler(mock_client, capture_config=CaptureConfig(capture_content=True))


def _chain() -> Any:
    return RunnableLambda(lambda q: q) | FakeListChatModel(responses=["a", "b", "c", "d"])


def test_two_awaited_chain_runs_in_one_task_are_two_traces_and_one_inside_a_trace(mock_client: Any) -> None:
    traces = _uploads(mock_client)
    handler = _handler(mock_client)
    chain = _chain()

    async def main() -> None:
        await chain.ainvoke("one", config={"callbacks": [handler]})
        await chain.ainvoke("two", config={"callbacks": [handler]})

    asyncio.run(main())
    assert len(traces) == 2 and all("model.invoke" in t for t in traces), traces

    @trace(mock_client)
    async def agent() -> None:
        await chain.ainvoke("three", config={"callbacks": [handler]})
        await chain.ainvoke("four", config={"callbacks": [handler]})

    asyncio.run(agent())
    assert len(traces) == 3 and traces[2].count("model.invoke") == 2, traces


def test_an_awaited_run_then_a_stream_then_a_new_handler_are_three_traces(mock_client: Any) -> None:
    traces = _uploads(mock_client)
    chain = _chain()

    async def main() -> None:
        await chain.ainvoke("one", config={"callbacks": [_handler(mock_client)]})
        [chunk async for chunk in chain.astream("two", config={"callbacks": [_handler(mock_client)]})]
        await chain.ainvoke("three", config={"callbacks": [_handler(mock_client)]})

    asyncio.run(main())
    assert len(traces) == 3 and all("model.invoke" in t for t in traces), traces


def test_an_async_run_then_a_sync_run_in_the_same_task_are_two_traces(mock_client: Any) -> None:
    traces = _uploads(mock_client)
    handler = _handler(mock_client)
    chain = _chain()

    async def main() -> None:
        await chain.ainvoke("one", config={"callbacks": [handler]})
        chain.invoke("two", config={"callbacks": [handler]})

    asyncio.run(main())
    assert len(traces) == 2 and all("model.invoke" in t for t in traces), traces


def test_models_called_on_their_own_after_an_async_chain_run_are_traced(mock_client: Any) -> None:
    traces = _uploads(mock_client)
    handler = _handler(mock_client)
    model = FakeListChatModel(responses=["x", "y"])

    async def main() -> None:
        await _chain().ainvoke("one", config={"callbacks": [handler]})
        await model.ainvoke("two", config={"callbacks": [handler]})
        [chunk async for chunk in model.astream("three", config={"callbacks": [handler]})]

    asyncio.run(main())
    assert len(traces) == 3, traces
    assert all("model.invoke" in t for t in traces), traces


def test_after_an_async_run_the_task_is_in_no_trace(mock_client: Any) -> None:
    _uploads(mock_client)
    handler = _handler(mock_client)

    async def main() -> Any:
        await _chain().ainvoke("one", config={"callbacks": [handler]})
        return get_trace_context(), inject_headers({})

    context, headers = asyncio.run(main())
    assert context is None
    assert "traceparent" not in headers
