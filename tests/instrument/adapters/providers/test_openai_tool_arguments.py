"""Tool-call arguments on the OpenAI provider follow ``capture_content``.

OpenAI sends a tool call's ``arguments`` as a JSON string; the trace carries the
decoded object in ``tool.call`` and in ``model.invoke.output_message``. Those
arguments are content: with ``capture_content`` off the collector strips both
fields, so they must not be decoded at all, because decoding a malformed string
logs a WARNING carrying a snippet of it. With content captured, a malformed call
is decoded once for both fields, so it logs one WARNING, not two.

Every case runs a real ``openai.OpenAI`` client over ``httpx.MockTransport`` on the
three surfaces a tool call arrives on: Chat Completions (the recorded
``openai/tool_call`` body), the Responses API (a ``function_call`` output item) and
a streamed chat (the recorded call sent as SSE chunks), plus a real
``openai.AsyncOpenAI`` client for the two chat surfaces, which take the adapter's
async paths. Azure OpenAI, OpenRouter and LiteLLM reuse these hooks; their own
suites cover them.
"""

from __future__ import annotations

import json
import asyncio
import logging
from typing import Any, Dict, List

import httpx
import pytest

import openai
from layerlens.instrument import trace
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.openai import OpenAIProvider

from ...conftest import find_event, find_events
from ..._recorded import load_recorded, mock_transport

SENTINEL = "LL-SENTINEL-4267"
#: Arguments the model cut short (not valid JSON), carrying a sentinel to search for.
MALFORMED = '{"city": "' + SENTINEL
VALID = '{"city":"Paris"}'

CHAT = "chat"
RESPONSES = "responses"
CHAT_STREAM = "chat-stream"
ASYNC_CHAT = "async-chat"
ASYNC_CHAT_STREAM = "async-chat-stream"
SURFACES = [CHAT, RESPONSES, CHAT_STREAM, ASYNC_CHAT, ASYNC_CHAT_STREAM]


def _recorded_body(*arguments: str) -> Dict[str, Any]:
    """The recorded chat.completions tool-call body, one call per *arguments*.

    The first call is the recorded one; any further call copies it with a new id,
    as OpenAI returns parallel tool calls. Only the in-memory copy is changed.
    """
    body: Dict[str, Any] = load_recorded("openai", "tool_call")["interactions"][0]["response"]["json"]
    recorded_call = body["choices"][0]["message"]["tool_calls"][0]
    calls = []
    for i, args in enumerate(arguments):
        call = json.loads(json.dumps(recorded_call))
        if i:
            call["id"] = f"{recorded_call['id']}_{i}"
        call["function"]["arguments"] = args
        calls.append(call)
    body["choices"][0]["message"]["tool_calls"] = calls
    return body


def _responses_body(arguments: str) -> Dict[str, Any]:
    """A Responses API body whose only output is the recorded call, as a ``function_call`` item."""
    call = _recorded_body(arguments)["choices"][0]["message"]["tool_calls"][0]
    return {
        "id": "resp_0f1e2d3c4b5a",
        "object": "response",
        "created_at": 1782168713,
        "status": "completed",
        "model": "gpt-4.1-2025-04-14",
        "output": [
            {
                "type": "function_call",
                "id": "fc_0f1e2d3c4b5a",
                "call_id": call["id"],
                "name": call["function"]["name"],
                "arguments": arguments,
                "status": "completed",
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 54,
            "output_tokens": 14,
            "total_tokens": 68,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _chat_stream_body(arguments: str) -> str:
    """The recorded call as a chat.completions stream, in OpenAI's SSE framing.

    The first chunk names the call, its arguments follow in two fragments, then the
    finish reason and a usage chunk.
    """
    body = _recorded_body(arguments)
    call = body["choices"][0]["message"]["tool_calls"][0]
    head = {k: body[k] for k in ("id", "created", "model", "service_tier", "system_fingerprint")}
    head["object"] = "chat.completion.chunk"

    def chunk(delta: Dict[str, Any], finish_reason: Any = None) -> Dict[str, Any]:
        return {**head, "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}]}

    half = len(arguments) // 2
    opening = {"index": 0, "id": call["id"], "type": "function", "function": {"name": "get_weather", "arguments": ""}}
    chunks = [
        chunk({"role": "assistant", "content": None, "tool_calls": [opening], "refusal": None}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments[:half]}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments[half:]}}]}),
        chunk({}, finish_reason="tool_calls"),
        {**head, "choices": [], "usage": body["usage"]},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _transport(surface: str, *arguments: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if surface == RESPONSES:
            return httpx.Response(200, json=_responses_body(*arguments))
        if surface in (CHAT_STREAM, ASYNC_CHAT_STREAM):
            return httpx.Response(
                200, content=_chat_stream_body(*arguments), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=_recorded_body(*arguments))

    return httpx.MockTransport(handler)


def _call(mock_client: Any, surface: str, capture_content: bool, *arguments: str) -> None:
    """One traced call on *surface* whose reply requests a tool call per *arguments*."""
    config = CaptureConfig(capture_content=capture_content)
    messages = [{"role": "user", "content": "What's the weather in Paris?"}]
    provider = OpenAIProvider()

    if surface in (ASYNC_CHAT, ASYNC_CHAT_STREAM):
        aclient = openai.AsyncOpenAI(
            api_key="test-key", http_client=httpx.AsyncClient(transport=_transport(surface, *arguments))
        )
        provider.connect(aclient)

        @trace(mock_client, capture_config=config)
        async def async_agent() -> str:
            if surface == ASYNC_CHAT_STREAM:
                stream = await aclient.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True)
                async for _ in stream:
                    pass
            else:
                await aclient.chat.completions.create(model="gpt-4o-mini", messages=messages)
            return "done"

        try:
            asyncio.run(async_agent())
        finally:
            provider.disconnect()
        return

    client = openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=_transport(surface, *arguments)))
    provider.connect(client)

    @trace(mock_client, capture_config=config)
    def agent() -> str:
        if surface == RESPONSES:
            client.responses.create(model="gpt-4.1", input=messages[0]["content"])
        elif surface == CHAT_STREAM:
            for _ in client.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True):
                pass
        else:
            client.chat.completions.create(model="gpt-4o-mini", messages=messages)
        return "done"

    try:
        agent()
    finally:
        provider.disconnect()


def _malformed_arguments_warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "malformed tool_call JSON arguments" in r.getMessage()
    ]


@pytest.mark.parametrize("surface", SURFACES)
def test_without_capture_content_arguments_are_neither_decoded_nor_logged(mock_client, capture_trace, caplog, surface):
    with caplog.at_level(logging.WARNING):
        _call(mock_client, surface, False, MALFORMED)

    events = capture_trace["events"]
    # The call is still traced, without its arguments.
    tool_call = find_event(events, "tool.call")["payload"]
    assert tool_call["tool_name"] == "get_weather"
    assert tool_call["id"] == "call_N0cYZsPSiRm7o3khCvuy6J3M"
    assert "arguments" not in tool_call
    assert "output_message" not in find_event(events, "model.invoke")["payload"]
    assert SENTINEL not in json.dumps(events)
    # Decoding the malformed string would have logged a snippet of it.
    assert _malformed_arguments_warnings(caplog) == []
    assert not any(SENTINEL in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("surface", SURFACES)
def test_with_capture_content_a_malformed_call_logs_one_warning_and_keeps_the_string(
    mock_client, capture_trace, caplog, surface
):
    with caplog.at_level(logging.WARNING):
        _call(mock_client, surface, True, MALFORMED)

    events = capture_trace["events"]
    warnings = _malformed_arguments_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert MALFORMED in warnings[0]
    # The string the model sent, in both places the call appears.
    assert find_event(events, "tool.call")["payload"]["arguments"] == MALFORMED
    output = find_event(events, "model.invoke")["payload"]["output_message"]
    assert [tc["arguments"] for tc in output["tool_calls"]] == [MALFORMED]


@pytest.mark.parametrize("surface", SURFACES)
def test_with_capture_content_arguments_are_an_object_in_both_places(mock_client, capture_trace, caplog, surface):
    with caplog.at_level(logging.WARNING):
        _call(mock_client, surface, True, VALID)

    events = capture_trace["events"]
    assert find_event(events, "tool.call")["payload"]["arguments"] == {"city": "Paris"}
    output = find_event(events, "model.invoke")["payload"]["output_message"]
    assert [tc["arguments"] for tc in output["tool_calls"]] == [{"city": "Paris"}]
    assert _malformed_arguments_warnings(caplog) == []


def test_each_malformed_call_logs_its_own_warning(mock_client, capture_trace, caplog):
    # Parallel tool calls, two of them malformed with the same string: one WARNING
    # per call, neither one per response nor one per distinct string.
    with caplog.at_level(logging.WARNING):
        _call(mock_client, CHAT, True, MALFORMED, VALID, MALFORMED)

    warnings = _malformed_arguments_warnings(caplog)
    assert len(warnings) == 2, warnings
    calls = [e["payload"] for e in find_events(capture_trace["events"], "tool.call")]
    assert [c["id"] for c in calls] == [
        "call_N0cYZsPSiRm7o3khCvuy6J3M",
        "call_N0cYZsPSiRm7o3khCvuy6J3M_1",
        "call_N0cYZsPSiRm7o3khCvuy6J3M_2",
    ]
    assert [c["arguments"] for c in calls] == [MALFORMED, {"city": "Paris"}, MALFORMED]


def _traced_with_content(mock_client: Any, client: openai.OpenAI, call: Any) -> Any:
    """Run *call* under a trace that captures content, with *client* instrumented."""
    provider = OpenAIProvider()
    provider.connect(client)

    @trace(mock_client, capture_config=CaptureConfig(capture_content=True))
    def agent() -> Any:
        return call()

    try:
        return agent()
    finally:
        provider.disconnect()


def test_arguments_sent_as_an_object_are_traced_as_sent(mock_client, capture_trace, caplog):
    # Some OpenAI-compatible servers send ``arguments`` as a JSON object instead of a
    # string, and the SDK keeps the dict. There is nothing to decode: the call must
    # not fail, and the object is traced as it came.
    body = _recorded_body(VALID)
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {"city": "Paris"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    client = openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport))
    messages = [{"role": "user", "content": "What's the weather in Paris?"}]

    with caplog.at_level(logging.WARNING):
        name = _traced_with_content(
            mock_client,
            client,
            lambda: (
                client.chat.completions.create(model="gpt-4o-mini", messages=messages)
                .choices[0]
                .message.tool_calls[0]
                .function.name
            ),
        )

    assert name == "get_weather"
    events = capture_trace["events"]
    assert find_event(events, "tool.call")["payload"]["arguments"] == {"city": "Paris"}
    output = find_event(events, "model.invoke")["payload"]["output_message"]
    assert [tc["arguments"] for tc in output["tool_calls"]] == [{"city": "Paris"}]
    assert _malformed_arguments_warnings(caplog) == []


def test_a_reply_without_an_output_message_is_traced_unchanged(mock_client, capture_trace):
    # An embeddings reply has no output message (and no tool calls) to decode.
    transport, _ = mock_transport(load_recorded("openai", "embeddings"))
    client = openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport))

    count = _traced_with_content(
        mock_client,
        client,
        lambda: len(client.embeddings.create(model="text-embedding-3-small", input="hello").data),
    )

    assert count == 1
    invoke = find_event(capture_trace["events"], "model.invoke")["payload"]
    assert invoke["name"] == "openai.embeddings.create"
    assert invoke["output_message"] is None
    assert find_events(capture_trace["events"], "tool.call") == []
