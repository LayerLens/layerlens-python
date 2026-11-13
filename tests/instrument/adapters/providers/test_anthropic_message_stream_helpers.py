"""``messages.stream()`` read through the anthropic SDK's own helpers records the call.

The adapter counted the events of a ``messages.stream()`` only as the caller iterated the stream
itself. ``stream.text_stream``, ``get_final_message()``, ``get_final_text()`` and ``until_done()``,
the ways the anthropic SDK documents to read one, reach the SDK's stream directly, so the adapter
saw no events and recorded no ``model.invoke`` and no ``cost.record`` (docs E2E 2026-11-10, L3
AD-15). Each case here reads a streamed reply from a real ``anthropic`` client over a mock
transport, as Anthropic sends it, one way or another, sync and async; iterating the stream itself,
which was recorded already, is covered by the streaming tests.
"""

from __future__ import annotations

import json
import asyncio
import importlib
from typing import Any, Dict, List, Tuple, Callable, Awaitable

import httpx
import pytest
import anthropic

from layerlens.instrument import trace
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.anthropic import AnthropicProvider

from ...conftest import find_event, find_events

# anthropic 1.0 moved from httpx to httpx2 (same API) and only accepts an httpx2 client.
_HTTP = importlib.import_module("httpx2") if int(anthropic.__version__.split(".")[0]) >= 1 else httpx

MODEL = "claude-haiku-4-5-20251001"
MESSAGES = [{"role": "user", "content": "Hello"}]
PARTS = ["Hel", "lo ", "there"]


def _sse() -> str:
    events: List[Tuple[str, Dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream_1",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
    ]
    for part in PARTS:
        events.append(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}},
            )
        )
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 6},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def _reply(request: Any) -> Any:  # noqa: ARG001
    return _HTTP.Response(200, content=_sse(), headers={"content-type": "text/event-stream"})


def _sync_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key="test-key", http_client=_HTTP.Client(transport=_HTTP.MockTransport(_reply)))


def _async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key="test-key", http_client=_HTTP.AsyncClient(transport=_HTTP.MockTransport(_reply))
    )


def _next_until_done(stream: Any) -> str:
    texts = []
    while True:
        try:
            event = next(stream)
        except StopIteration:
            return "".join(texts)
        if event.type == "content_block_delta":
            texts.append(event.delta.text)


def _two_events_then_final(stream: Any) -> str:
    events = iter(stream)
    next(events)
    next(events)
    return str(stream.get_final_message().content[0].text)


def _until_done(stream: Any) -> str:
    stream.until_done()
    return "Hello there"


SYNC_READS: Dict[str, Callable[[Any], str]] = {
    "next(stream)": _next_until_done,
    "text_stream": lambda stream: "".join(stream.text_stream),
    "get_final_message()": lambda stream: str(stream.get_final_message().content[0].text),
    "get_final_text()": lambda stream: str(stream.get_final_text()),
    "until_done()": _until_done,
    "two events, then get_final_message()": _two_events_then_final,
}


async def _atext_stream(stream: Any) -> str:
    return "".join([text async for text in stream.text_stream])


async def _afinal_message(stream: Any) -> str:
    return str((await stream.get_final_message()).content[0].text)


async def _afinal_text(stream: Any) -> str:
    return str(await stream.get_final_text())


async def _auntil_done(stream: Any) -> str:
    await stream.until_done()
    return "Hello there"


ASYNC_READS: Dict[str, Callable[[Any], Awaitable[str]]] = {
    "text_stream": _atext_stream,
    "get_final_message()": _afinal_message,
    "get_final_text()": _afinal_text,
    "until_done()": _auntil_done,
}


def _assert_recorded(events: List[Dict[str, Any]], capture_content: bool) -> None:
    invoke = find_event(events, "model.invoke")["payload"]
    assert invoke["usage"]["input_tokens"] == 12 and invoke["usage"]["output_tokens"] == 6
    assert (invoke["prompt_tokens"], invoke["completion_tokens"]) == (12, 6)
    assert invoke["response_id"] == "msg_stream_1" and invoke["stop_reason"] == "end_turn"
    assert invoke["ttft_ms"] >= 0
    if capture_content:
        assert invoke["output_message"] == {"type": "text", "text": "Hello there"}
    else:
        assert "output_message" not in invoke
    costs = find_events(events, "cost.record")
    assert len(costs) == 1 and costs[0]["payload"]["cost_usd"] > 0
    assert len(find_events(events, "model.invoke")) == 1


@pytest.mark.parametrize("capture_content", [True, False], ids=["content", "no-content"])
@pytest.mark.parametrize("read", list(SYNC_READS), ids=list(SYNC_READS))
def test_a_stream_read_any_documented_way_is_recorded(
    mock_client: Any, capture_trace: Dict[str, Any], read: str, capture_content: bool
) -> None:
    client = _sync_client()
    provider = AnthropicProvider()
    provider.connect(client)

    @trace(mock_client, capture_config=CaptureConfig(capture_content=capture_content))
    def agent() -> str:
        with client.messages.stream(model=MODEL, max_tokens=20, messages=MESSAGES) as stream:
            return SYNC_READS[read](stream)

    try:
        assert agent() == "Hello there"
    finally:
        provider.disconnect()
    _assert_recorded(capture_trace["events"], capture_content)


@pytest.mark.parametrize("read", list(ASYNC_READS), ids=list(ASYNC_READS))
def test_an_async_stream_read_any_documented_way_is_recorded(
    mock_client: Any, capture_trace: Dict[str, Any], read: str
) -> None:
    client = _async_client()
    provider = AnthropicProvider()
    provider.connect(client)

    @trace(mock_client, capture_config=CaptureConfig(capture_content=True))
    async def agent() -> str:
        async with client.messages.stream(model=MODEL, max_tokens=20, messages=MESSAGES) as stream:
            return await ASYNC_READS[read](stream)

    try:
        assert asyncio.run(agent()) == "Hello there"
    finally:
        provider.disconnect()
    _assert_recorded(capture_trace["events"], True)
