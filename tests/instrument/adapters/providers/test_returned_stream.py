"""S2: a stream returned by a ``@trace`` function was recorded nowhere.

The trace was sealed and uploaded when the function returned, before its caller read the stream,
so the call's ``model.invoke`` and ``cost.record`` had no trace to go to (live: the trace held only
``agent.identity``, ``agent.input`` and ``agent.output``). A trace whose code hands out a stream it
opened now waits for that stream: it is uploaded once the stream has been read to its end, and
holds the stream's events. A stream closed early, dropped unread, or still open when the process
exits lets the trace upload with what it has. ``trace_context`` blocks work the same way.
"""

from __future__ import annotations

import gc
import sys
import time
import queue
import asyncio
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Callable, Iterator, Generator, AsyncIterator
from contextlib import contextmanager

import pytest

from layerlens.instrument import span, trace, _upload, _collector, trace_context
from layerlens.instrument._context import _current_collector
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.openai import OpenAIProvider

from ...conftest import find_event, find_events
from .test_openai_responses_stream import MODEL, _events, _completed

PROMPT = "What colour is the sky?"

# A stream dropped unread releases its trace when it is collected: at once under CPython's
# reference counting, which these tests rely on; at some later collection elsewhere.
needs_refcounting = pytest.mark.skipif(
    sys.implementation.name != "cpython", reason="relies on CPython collecting a dropped stream at once"
)


def _client() -> Any:
    def create(**kwargs: Any) -> Iterator[Any]:  # noqa: ARG001
        return iter(_events(_completed()))

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    OpenAIProvider().connect(client)
    return client


def _read(stream: Iterator[Any]) -> str:
    return "".join(e.delta for e in stream if e.type == "response.output_text.delta")


def _types(upload: Dict[str, Any]) -> List[str]:
    return [e["event_type"] for e in upload["events"]]


def _assert_holds_the_call(upload: Dict[str, Any]) -> None:
    events = upload["events"]
    assert {"agent.input", "agent.output", "model.invoke", "cost.record"} <= set(_types(upload))
    assert {e["trace_id"] for e in events} == {upload["trace_id"]}
    invoke = find_event(events, "model.invoke")["payload"]
    assert invoke["usage"]["total_tokens"] == 25 and invoke["response_id"] == "resp_test"
    costs = find_events(events, "cost.record")
    assert len(costs) == 1 and costs[0]["payload"]["cost_usd"] > 0


def test_a_stream_returned_by_the_traced_function_is_recorded_once_the_caller_reads_it(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    client = _client()

    @trace(mock_client, capture_config=CaptureConfig.full())
    def answer() -> Iterator[Any]:
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    stream = answer()
    assert capture_trace_list == [], "the trace was uploaded before its stream was read"
    assert _read(stream) == "Blue."
    assert len(capture_trace_list) == 1
    _assert_holds_the_call(capture_trace_list[0])


def test_an_async_stream_returned_by_the_traced_function_is_recorded_too(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    async def stream() -> AsyncIterator[Any]:
        for event in _events(_completed()):
            yield event

    async def create(**kwargs: Any) -> AsyncIterator[Any]:  # noqa: ARG001
        return stream()

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    OpenAIProvider().connect(client)

    @trace(mock_client, capture_config=CaptureConfig.full())
    async def answer() -> AsyncIterator[Any]:
        return await client.responses.create(model=MODEL, input=PROMPT, stream=True)

    async def main() -> str:
        returned = await answer()
        assert capture_trace_list == [], "the trace was uploaded before its stream was read"
        return "".join([e.delta async for e in returned if e.type == "response.output_text.delta"])

    assert asyncio.run(main()) == "Blue."
    assert len(capture_trace_list) == 1
    _assert_holds_the_call(capture_trace_list[0])


@needs_refcounting
def test_a_stream_dropped_unread_or_closed_early_lets_its_trace_upload(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    client = _client()

    @trace(mock_client)
    def answer() -> Any:
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    dropped = answer()
    assert capture_trace_list == [], "the trace did not wait for its stream"
    del dropped
    gc.collect()
    assert len(capture_trace_list) == 1
    closed = answer()
    next(closed)
    assert len(capture_trace_list) == 1, "the trace did not wait for its stream"
    closed.close()
    assert len(capture_trace_list) == 2
    # Neither stream was read to its end, so neither recorded a call; each trace still has its own.
    for upload in capture_trace_list:
        assert "agent.input" in _types(upload) and "model.invoke" not in _types(upload)


def test_a_trace_still_waiting_for_its_stream_at_exit_is_uploaded_by_the_exit(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    client = _client()

    @trace(mock_client)
    def answer() -> Iterator[Any]:
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    kept = answer()  # never read, still referenced when the process exits
    assert capture_trace_list == []
    _upload._flush_at_exit()
    assert len(capture_trace_list) == 1 and "agent.input" in _types(capture_trace_list[0])
    assert kept is not None


def test_a_stream_read_inside_another_trace_is_recorded_in_the_one_that_opened_it(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    client = _client()

    @trace(mock_client, name="opens", capture_config=CaptureConfig.full())
    def opens() -> Iterator[Any]:
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    @trace(mock_client, name="reads")
    def reads() -> str:
        return _read(opens())

    assert reads() == "Blue."
    by_name = {find_event(u["events"], "agent.input")["payload"]["name"]: u for u in capture_trace_list}
    assert sorted(by_name) == ["opens", "reads"]
    _assert_holds_the_call(by_name["opens"])
    assert "model.invoke" not in _types(by_name["reads"])


def test_a_trace_context_waits_for_a_stream_opened_in_it(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    client = _client()
    with trace_context(mock_client, capture_config=CaptureConfig.full()):
        stream = client.responses.create(model=MODEL, input=PROMPT, stream=True)
    assert capture_trace_list == [], "the trace was uploaded before its stream was read"
    assert _read(stream) == "Blue."
    assert len(capture_trace_list) == 1
    assert {"model.invoke", "cost.record"} <= set(_types(capture_trace_list[0]))


def test_a_stream_read_outside_the_span_it_was_opened_in_is_recorded_in_that_span(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # CodeRabbit on #99: in the same trace, the events went under the span current when the stream
    # was read, not the one the call was made in, as the docs say.
    client = _client()

    @trace(mock_client, capture_config=CaptureConfig.full())
    def answer() -> str:
        with span("llm") as llm:
            opened.append(llm)
            stream = client.responses.create(model=MODEL, input=PROMPT, stream=True)
        with span("render"):
            return _read(stream)

    opened: List[str] = []
    assert answer() == "Blue."
    events = capture_trace_list[0]["events"]
    # A model call gets a span of its own, under the span current when it is recorded.
    for event_type in ("model.invoke", "cost.record"):
        assert find_event(events, event_type)["parent_span_id"] == opened[0], event_type


@needs_refcounting
def test_agent_output_records_the_reply_the_caller_read_not_the_stream_object(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # It recorded str(stream), "<generator object …>": the function's output is what the stream
    # replied, known once the caller has read it. A stream that never replied records none.
    client = _client()

    @trace(mock_client, capture_config=CaptureConfig.full())
    def answer() -> Any:
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    assert _read(answer()) == "Blue."
    events = capture_trace_list[0]["events"]
    output = find_event(events, "agent.output")
    assert output["payload"]["output"] == find_event(events, "model.invoke")["payload"]["output_message"]
    assert output["payload"]["output"]["content"] == "Blue." and output["payload"]["status"] == "ok"
    assert output["sequence_id"] > find_event(events, "model.invoke")["sequence_id"]

    dropped = answer()
    del dropped
    gc.collect()
    kept = answer()
    _upload._flush_at_exit()
    assert kept is not None and len(capture_trace_list) == 3
    for upload in capture_trace_list[1:]:
        assert find_event(upload["events"], "agent.output")["payload"]["output"] is None

    # The reply is content: without content capture, agent.output carries none of it.
    @trace(mock_client, capture_config=CaptureConfig(capture_content=False))
    def quiet() -> Any:
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    assert _read(quiet()) == "Blue."
    payload = find_event(capture_trace_list[3]["events"], "agent.output")["payload"]
    assert "output" not in payload and payload["status"] == "ok"


def test_a_stream_read_to_its_end_and_then_returned_records_its_reply(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # CodeRabbit on #99: the stream had already ended when the function returned it, so
    # agent.output recorded the stream object rather than its reply.
    client = _client()

    @trace(mock_client, capture_config=CaptureConfig.full())
    def answer() -> Any:
        stream = client.responses.create(model=MODEL, input=PROMPT, stream=True)
        assert _read(stream) == "Blue."
        return stream

    answer()
    events = capture_trace_list[0]["events"]
    assert (
        find_event(events, "agent.output")["payload"]["output"]
        == find_event(events, "model.invoke")["payload"]["output_message"]
    )


@needs_refcounting
def test_a_finished_stream_kept_by_the_program_does_not_keep_its_trace_in_memory(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # CodeRabbit on #99: the stream's finalizer held the trace (and its events) until the stream
    # itself was collected, so a program that keeps finished streams kept every trace.
    import weakref

    client = _client()
    with trace_context(mock_client) as collector:
        stream = client.responses.create(model=MODEL, input=PROMPT, stream=True)
    assert _read(stream) == "Blue." and len(capture_trace_list) == 1
    finished = weakref.ref(collector)
    del collector
    gc.collect()
    assert finished() is None, "the uploaded trace is still in memory while its finished stream is kept"
    assert stream is not None


@contextmanager
def _no_automatic_collection() -> Generator[None, None, None]:
    """Only the collections a test runs: an automatic one could free a stream left in a cycle first."""
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _leave_in_a_cycle(value: Any) -> None:
    """Make ``value`` reachable only through a reference cycle, which only a collection frees."""
    cycle: Dict[str, Any] = {"value": value}
    cycle["cycle"] = cycle


def _collect_holding_the_lock_of(collector: Any) -> bool:
    """Run a collection on a thread that holds ``collector``'s lock, as an allocation inside the
    collector can; True if it returned. A release that takes that lock again never returns."""
    returned = threading.Event()

    def collect() -> None:
        with collector._lock:  # noqa: SLF001
            gc.collect()
        returned.set()

    threading.Thread(target=collect, daemon=True).start()
    if returned.wait(5):
        return True
    # Hung: keep the exit hook that later tests run away from the lock that thread holds forever.
    with _collector._waiting_lock:  # noqa: SLF001
        _collector._waiting_for_streams.discard(collector)  # noqa: SLF001
    return False


def _uploaded(count: int, uploads: List[Dict[str, Any]]) -> bool:
    """Whether ``count`` traces are uploaded within 5 s: a handed-off release uploads on its own thread."""
    deadline = time.monotonic() + 5
    while len(uploads) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return len(uploads) == count


def _answer_keeping_its_trace(mock_client: Any, collectors: List[Any]) -> Callable[[], Any]:
    """A ``@trace`` function returning an unread stream; ``collectors`` gets the trace it opened."""
    client = _client()

    @trace(mock_client)
    def answer() -> Any:
        collectors.append(_current_collector.get())
        return client.responses.create(model=MODEL, input=PROMPT, stream=True)

    return answer


@pytest.mark.parametrize("read_once", [False, True], ids=["unread", "read-once"])
def test_a_stream_collected_while_its_trace_is_locked_releases_it_without_a_hang(
    read_once: bool, mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # CodeRabbit on #99: a collection runs a dropped stream's release (its finalizer, or the
    # `finally` of a started one) inside whatever its thread was doing, which can hold the trace's
    # lock; the release took that lock again, and the program hung.
    collectors: List[Any] = []
    answer = _answer_keeping_its_trace(mock_client, collectors)
    with _no_automatic_collection():
        stream = answer()
        if read_once:
            next(stream)
        _leave_in_a_cycle(stream)
        del stream
        assert capture_trace_list == [], "the trace did not wait for its stream"
        assert _collect_holding_the_lock_of(collectors[0]), "the stream's release hung on its trace's lock"
    # The release is done on the releasing thread, which uploads the trace with no further call.
    assert _uploaded(1, capture_trace_list), "the trace was not uploaded once its stream was collected"
    assert {"agent.input", "agent.output"} <= set(_types(capture_trace_list[0]))


@pytest.mark.parametrize("finish", ["flush", "exit"])
def test_flush_and_the_exit_do_a_release_a_collection_handed_off(
    finish: str, monkeypatch: pytest.MonkeyPatch, mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # A release handed off during a collection waits for the releasing thread; flush() and the exit
    # do it themselves, so a trace whose stream was collected is not left behind. No thread here.
    monkeypatch.setattr(_collector, "_handed_off", queue.SimpleQueue())
    monkeypatch.setattr(_collector, "_start_releasing", lambda: None)
    answer = _answer_keeping_its_trace(mock_client, [])
    with _no_automatic_collection():
        stream = answer()
        _leave_in_a_cycle(stream)
        del stream
        gc.collect()
    assert capture_trace_list == [], "the release ran inside the collection"
    if finish == "flush":
        assert _upload.flush(timeout=5)
    else:
        _upload._flush_at_exit()
    assert len(capture_trace_list) == 1
