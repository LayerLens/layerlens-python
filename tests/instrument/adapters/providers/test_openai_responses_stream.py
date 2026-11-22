"""BUG-12: an OpenAI Responses API stream recorded no token usage and no cost.

``responses.create(stream=True)`` was aggregated as a Chat Completions stream, reading
``chunk.usage``, ``chunk.model`` and ``chunk.choices[0].delta`` off events that carry none of them:
the stored ``model.invoke`` held only the request's model, and there was no ``cost.record``. A
Responses stream ends with ``response.completed`` (or ``.incomplete`` / ``.failed``), whose
``response`` is the whole ``Response``; it is now read as the non-stream call's response is.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Iterator, AsyncIterator

from layerlens.instrument import trace
from openai.types.responses import (
    Response,
    ResponseCreatedEvent,
    ResponseCompletedEvent,
    ResponseTextDeltaEvent,
    ResponseIncompleteEvent,
)
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.openai import OpenAIProvider

from ...conftest import find_event, find_events
from .test_openai_bugfix import make_responses_object

MODEL = "gpt-4o-mini"


def _in_progress() -> Response:
    started = make_responses_object(model=MODEL)
    return started.model_copy(update={"id": "resp_test", "status": "in_progress", "output": [], "usage": None})


def _events(final: Any) -> List[Any]:
    """The events openai's SDK yields for a Responses stream, ending with ``final`` (or not)."""
    events: List[Any] = [ResponseCreatedEvent(type="response.created", sequence_number=0, response=_in_progress())]
    for i, piece in enumerate(["Bl", "ue."], start=1):
        events.append(
            ResponseTextDeltaEvent(
                type="response.output_text.delta",
                sequence_number=i,
                item_id="msg_1",
                output_index=0,
                content_index=0,
                delta=piece,
                logprobs=[],
            )
        )
    if final is not None:
        events.append(final)
    return events


def _completed() -> ResponseCompletedEvent:
    done = make_responses_object(text="Blue.", model=MODEL, input_tokens=18, output_tokens=7, total_tokens=25)
    return ResponseCompletedEvent(type="response.completed", sequence_number=3, response=done)


def _incomplete() -> ResponseIncompleteEvent:
    cut = make_responses_object(text="Bl", model=MODEL, input_tokens=18, output_tokens=16, total_tokens=34)
    cut = cut.model_copy(update={"status": "incomplete"})
    return ResponseIncompleteEvent(type="response.incomplete", sequence_number=3, response=cut)


def _sync_client(events: List[Any]) -> Any:
    def create(**kwargs: Any) -> Iterator[Any]:  # noqa: ARG001
        return iter(events)

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    OpenAIProvider().connect(client)
    return client


def _run(mock_client: Any, events: List[Any]) -> str:
    client = _sync_client(events)

    @trace(mock_client, capture_config=CaptureConfig.full())
    def agent() -> str:
        text = []
        for event in client.responses.create(model=MODEL, input="What colour is the sky?", stream=True):
            if event.type == "response.output_text.delta":
                text.append(event.delta)
        return "".join(text)

    return agent()


def _model_and_cost(capture_trace: Any) -> Any:
    events = capture_trace["events"]
    return find_event(events, "model.invoke")["payload"], find_events(events, "cost.record")


def test_a_completed_stream_records_its_usage_and_a_cost(mock_client: Any, capture_trace: Any) -> None:
    assert _run(mock_client, _events(_completed())) == "Blue."
    invoke, costs = _model_and_cost(capture_trace)
    assert invoke["name"] == "openai.responses.create"
    assert invoke["usage"]["prompt_tokens"] == 18 and invoke["usage"]["completion_tokens"] == 7
    assert invoke["usage"]["total_tokens"] == 25
    assert invoke["response_model"] == MODEL and invoke["response_id"] == "resp_test"
    assert invoke["output_message"]["content"] == "Blue."
    assert len(costs) == 1 and costs[0]["payload"]["cost_usd"] > 0


def test_an_incomplete_stream_records_the_usage_it_reports(mock_client: Any, capture_trace: Any) -> None:
    _run(mock_client, _events(_incomplete()))
    invoke, costs = _model_and_cost(capture_trace)
    assert invoke["usage"]["completion_tokens"] == 16 and invoke["usage"]["total_tokens"] == 34
    assert len(costs) == 1 and costs[0]["payload"]["cost_usd"] > 0


def test_a_stream_cut_before_its_last_event_keeps_what_its_deltas_gave(mock_client: Any, capture_trace: Any) -> None:
    # No usage arrived, so none is recorded and nothing is priced: a cost from no tokens
    # would be made up.
    assert _run(mock_client, _events(None)) == "Blue."
    invoke, costs = _model_and_cost(capture_trace)
    assert invoke["output_message"]["content"] == "Blue."
    assert invoke["response_model"] == MODEL and invoke["response_id"] == "resp_test"
    assert "usage" not in invoke or not invoke["usage"].get("total_tokens")
    assert costs == []


def test_an_async_stream_records_its_usage_and_a_cost(mock_client: Any, capture_trace: Any) -> None:
    events = _events(_completed())

    async def stream() -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def create(**kwargs: Any) -> AsyncIterator[Any]:  # noqa: ARG001
        return stream()

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    OpenAIProvider().connect(client)

    @trace(mock_client, capture_config=CaptureConfig.full())
    async def agent() -> str:
        text = []
        async for event in await client.responses.create(model=MODEL, input="What colour is the sky?", stream=True):
            if event.type == "response.output_text.delta":
                text.append(event.delta)
        return "".join(text)

    assert asyncio.run(agent()) == "Blue."
    invoke, costs = _model_and_cost(capture_trace)
    assert invoke["usage"]["total_tokens"] == 25 and invoke["response_id"] == "resp_test"
    assert len(costs) == 1 and costs[0]["payload"]["cost_usd"] > 0
