"""A model called on its own keeps its open run until its end, however many calls overlap.

The handler kept each such run by its run id until its end came, at most 256 of them: the oldest was
dropped past that, so that a call whose end never comes could not hold its trace forever. More than
256 calls in flight on one handler (a busy server sharing it) dropped runs still waiting for their end,
and those calls recorded nothing (CodeRabbit on #103, 3dc3c7e). A call's end fails to come when its
task is cancelled: ``agenerate`` re-raises the cancellation from its ``gather`` without ``on_llm_error``.
So a run is now dropped when the task the call started in has finished and its end still has not
come, after a grace period for an end LangChain shields from that cancellation. A call cancelled inside
a task that goes on (``asyncio.timeout``) never ends either: past 256 open runs, a run with no callback
for an hour is dropped. Its age counts from its last callback, so a call that streams stays.
"""

from __future__ import annotations

import gc
import json
import time
import uuid
import asyncio
import weakref
import contextlib
from typing import Any, Dict, List, Optional

import pytest
from langchain_core.outputs import LLMResult, ChatResult, ChatGeneration
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.callbacks import CallbackManagerForLLMRun, AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel

from layerlens.instrument._collector import TraceCollector
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks import langchain as lc
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

_GATE: Dict[str, Any] = {}


class _GatedModel(BaseChatModel):
    """Answers once ``_GATE["open"]`` is set, so calls can be held in flight together."""

    @property
    def _llm_type(self) -> str:
        return "gated"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        _GATE["started"] += 1
        await _GATE["open"].wait()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


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


def _gate() -> None:
    _GATE["open"] = asyncio.Event()
    _GATE["started"] = 0


def _flushed(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """The event types of each trace flushed, in order. The upload queue (64 a client) is not tested here."""
    traces: List[List[str]] = []
    flush = TraceCollector.flush

    def counting_flush(self: TraceCollector) -> None:
        if not self._sealed and self._events:
            traces.append([e["event_type"] for e in self._events])
        flush(self)

    monkeypatch.setattr(TraceCollector, "flush", counting_flush)
    return traces


def test_more_overlapping_calls_than_the_old_bound_are_each_traced(
    mock_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    traces = _flushed(monkeypatch)
    handler = _handler(mock_client)
    model = _GatedModel()
    calls = 300  # the old bound was 256

    async def main() -> int:
        _gate()
        tasks = [asyncio.ensure_future(model.ainvoke("q", config={"callbacks": [handler]})) for _ in range(calls)]
        while _GATE["started"] < calls:
            await asyncio.sleep(0)
        open_runs = len(handler._model_runs)
        _GATE["open"].set()
        await asyncio.gather(*tasks)
        return open_runs

    assert asyncio.run(main()) == calls
    assert len(traces) == calls and all("model.invoke" in t for t in traces)
    assert not handler._model_runs


def test_a_cancelled_calls_run_is_dropped_once_its_task_has_ended(
    mock_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc, "_UNENDED_RUN_GRACE_SECONDS", 0.0)
    handler = _handler(mock_client)
    model = _GatedModel()

    async def main() -> List[int]:
        _gate()
        task = asyncio.ensure_future(model.ainvoke("q", config={"callbacks": [handler]}))
        while _GATE["started"] < 1:
            await asyncio.sleep(0)
        seen = [len(handler._model_runs)]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for _ in range(5):  # the drop is scheduled with call_later(0)
            await asyncio.sleep(0)
        seen.append(len(handler._model_runs))
        return seen

    assert asyncio.run(main()) == [1, 0]


def test_a_long_lived_task_does_not_keep_ended_runs(mock_client: Any) -> None:
    traces = _uploads(mock_client)
    handler = _handler(mock_client)
    model = _GatedModel()
    kept: List["weakref.ReferenceType[Any]"] = []
    keep = handler._keep_model_run

    def keep_and_watch(key: str, run: Any) -> None:
        kept.append(weakref.ref(run))
        keep(key, run)

    handler._keep_model_run = keep_and_watch  # type: ignore[method-assign]

    async def main() -> List[bool]:
        _gate()
        _GATE["open"].set()
        for _ in range(3):
            await model.ainvoke("q", config={"callbacks": [handler]})
        gc.collect()
        return [ref() is None for ref in kept]  # this task is still running

    assert asyncio.run(main()) == [True, True, True]
    assert len(traces) == 3


def test_past_the_bound_a_run_open_too_long_is_dropped_even_while_its_task_lives(
    mock_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call cancelled inside a task that goes on (``asyncio.timeout``, ``wait_for`` on 3.12+) never
    ends its run, and its task does not end either: past the bound, a run that old is dropped."""
    monkeypatch.setattr(lc, "_MAX_OPEN_MODEL_RUNS", 1)
    monkeypatch.setattr(lc, "_STALE_RUN_SECONDS", 0.0)
    handler = _handler(mock_client)
    serialized: Dict[str, Any] = {"name": "gated"}

    async def main() -> List[List[str]]:
        first, second = uuid.uuid4(), uuid.uuid4()
        handler.on_chat_model_start(serialized, [[]], run_id=first, parent_run_id=None)  # its end never comes
        open_after_first = list(handler._model_runs)
        handler.on_chat_model_start(serialized, [[]], run_id=second, parent_run_id=None)
        return [open_after_first, list(handler._model_runs)]

    first_open, then_open = asyncio.run(main())
    assert len(first_open) == 1 and len(then_open) == 1 and then_open != first_open


def test_past_the_bound_a_streaming_call_older_than_the_age_is_kept_and_traced(
    mock_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run's age counts from its last callback: a call that still streams tokens is never stale,
    however long ago it started, and its end records its trace."""
    traces = _flushed(monkeypatch)
    monkeypatch.setattr(lc, "_MAX_OPEN_MODEL_RUNS", 1)
    monkeypatch.setattr(lc, "_STALE_RUN_SECONDS", 0.05)
    handler = _handler(mock_client)
    serialized: Dict[str, Any] = {"name": "streaming"}
    streaming, other = uuid.uuid4(), uuid.uuid4()

    handler.on_chat_model_start(serialized, [[]], run_id=streaming, parent_run_id=None)
    time.sleep(0.1)  # older than the age since its start
    handler.on_llm_new_token("o", run_id=streaming, parent_run_id=None)
    handler.on_chat_model_start(serialized, [[]], run_id=other, parent_run_id=None)  # past the bound
    assert str(streaming) in handler._model_runs
    handler.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="ok"))]]),
        run_id=streaming,
        parent_run_id=None,
    )
    assert len(traces) == 1 and "model.invoke" in traces[0]


def test_the_age_scan_skips_a_run_that_ended_meanwhile(mock_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another thread's call can end between the scan's snapshot of the keys and its look at one."""
    monkeypatch.setattr(lc, "_STALE_RUN_SECONDS", 0.0)
    handler = _handler(mock_client)

    class _EndedMeanwhile(dict):  # type: ignore[type-arg]
        def __iter__(self) -> Any:
            yield "ended-meanwhile"
            yield from super().__iter__()

    handler._model_runs = _EndedMeanwhile()
    handler.on_chat_model_start({"name": "m"}, [[]], run_id=uuid.uuid4(), parent_run_id=None)
    handler._drop_stale_model_runs(but="")
    assert list(dict.keys(handler._model_runs)) == []
