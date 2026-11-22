"""The LangChain handler records an async run: ``ainvoke``, ``astream``, a chain or a bare model.

LangChain calls a sync handler's callbacks from an async run in an executor thread, each in a copy
of the context (``run_in_executor`` with ``copy_context().run``), unless the handler says
``run_inline``. The run the handler began at the root's start lived in one of those copies, so the
callbacks after it saw no run and no trace: ``await chain.ainvoke(..., callbacks=[handler])``
uploaded nothing, where ``invoke`` uploaded the trace. The handler now runs inline, in the run's
own context, as its callbacks are quick and never wait on I/O.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from layerlens.instrument._context import _current_run, _current_collector
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

from .conftest import find_events, capture_framework_trace


def _handler(mock_client: Any) -> LangChainCallbackHandler:
    return LangChainCallbackHandler(mock_client, capture_config=CaptureConfig(capture_content=True))


def test_an_async_chain_run_is_recorded(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    chain = RunnableLambda(lambda q: q) | FakeListChatModel(responses=["ok"])
    reply = asyncio.run(chain.ainvoke("hi", config={"callbacks": [_handler(mock_client)]}))
    assert reply.content == "ok"
    assert mock_client.traces.upload.call_count == 1
    kinds = [e["event_type"] for e in uploaded["events"]]
    assert kinds.count("agent.input") >= 1 and "model.invoke" in kinds and "agent.output" in kinds


def test_an_async_bare_model_call_and_stream_are_recorded(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    handler = _handler(mock_client)
    model = FakeListChatModel(responses=["one", "two"])

    async def main() -> Any:
        first = await model.ainvoke("hi", config={"callbacks": [handler]})
        streamed = [chunk.content async for chunk in model.astream("again", config={"callbacks": [handler]})]
        # Nothing of the two runs is left in the caller's context for a later call to reuse:
        # an async model reports its end from a task of its own.
        return f"{first.content} {''.join(str(c) for c in streamed)}", _current_run.get(), _current_collector.get()

    assert asyncio.run(main()) == ("one two", None, None)
    assert mock_client.traces.upload.call_count == 2
    invokes = [e["payload"] for e in find_events(uploaded["events"], "model.invoke")]
    assert [i["output_message"] for i in invokes] == ["one", "two"]
    assert invokes[1]["streaming"] is True


def test_concurrent_async_runs_are_separate_traces(mock_client: Any) -> None:
    traces: List[List[str]] = []

    def capture(path: str) -> Any:
        import json

        with open(path) as f:
            events = json.load(f)[0]["events"]
        traces.append(
            [str((e.get("payload") or {}).get("input", "")) for e in events if e["event_type"] == "agent.input"]
        )
        return None

    mock_client.traces.upload.side_effect = capture
    handler = _handler(mock_client)

    async def slow(question: str) -> str:
        await asyncio.sleep(0.01)
        return question

    chain = RunnableLambda(slow)

    async def main() -> None:
        await asyncio.gather(*(chain.ainvoke(f"q{i}", config={"callbacks": [handler]}) for i in range(3)))

    asyncio.run(main())
    assert len(traces) == 3
    assert sorted(t[0] for t in traces) == ["q0", "q1", "q2"]
    assert all(len(set(t)) == 1 for t in traces), traces
