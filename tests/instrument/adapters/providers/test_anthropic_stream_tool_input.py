"""Streamed Anthropic tool input reaches the trace without breaking the caller's loop.

Anthropic streams a tool call's ``input`` as JSON fragments (``input_json_delta``).
The adapter joins them and the trace carries the decoded object, decoded where the
events are emitted: once per call and only when the trace captures content, because
the input is content and a string that does not decode is logged, in part. Input
Python cannot decode (nested deeper than its JSON decoder recurses, or an integer
past its 4300-digit limit) stays the string the model sent; it must never raise
into the caller's loop over a stream that succeeded.

Each case serves the recorded ``anthropic/tool_call`` reply, re-sent as the SSE
events Anthropic streams it in, to a real ``anthropic.Anthropic`` client over
``httpx.MockTransport``, through both streaming surfaces: ``messages.create(
stream=True)``, which hands the caller the raw events, and the ``messages.stream()``
context manager, whose SDK accumulator parses the partial JSON itself.
"""

from __future__ import annotations

import json
import logging
import importlib
from typing import Any, Dict, List, Tuple, Optional

import httpx
import pytest
import anthropic

from layerlens.instrument import trace
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.anthropic import AnthropicProvider

from ...conftest import find_event, find_events
from ..._recorded import load_recorded

# anthropic 1.0 moved from httpx to httpx2 (same API) and only accepts an httpx2 client.
_HTTP = importlib.import_module("httpx2") if int(anthropic.__version__.split(".")[0]) >= 1 else httpx

SENTINEL = "LL-SENTINEL-4267"
#: Valid JSON nested deeper than Python's JSON decoder recurses.
DEEP = "[" * 50_000 + "]" * 50_000
#: Valid JSON whose integer is longer than ``int()`` converts by default.
BIG_INT = '{"n": ' + "1" * 5000 + "}"
#: Input the model cut short: not valid JSON.
TRUNCATED = '{"city": "' + SENTINEL

MODEL = "claude-haiku-4-5"
MESSAGES = [{"role": "user", "content": "What's the weather in Paris?"}]


def _recorded_reply() -> Dict[str, Any]:
    reply: Dict[str, Any] = load_recorded("anthropic", "tool_call")["interactions"][0]["response"]["json"]
    return reply


TOOL_USE = _recorded_reply()["content"][0]


def _sse(partial_json: str, *, text_first: bool = False) -> str:
    """The recorded tool_use reply as Anthropic streams it, input as *partial_json*.

    The input arrives in two ``input_json_delta`` fragments. With *text_first* a
    text block precedes the tool_use block, as Claude usually writes one, so the
    tool_use block is content block 1.
    """
    reply = _recorded_reply()
    start = {k: reply[k] for k in ("id", "type", "role", "model")}
    usage = {"input_tokens": reply["usage"]["input_tokens"], "output_tokens": 1}
    events: List[Tuple[str, Dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {**start, "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage},
            },
        )
    ]
    index = 0
    if text_first:
        events += [
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me check."}},
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        index = 1
    block = {"type": "tool_use", "id": TOOL_USE["id"], "name": TOOL_USE["name"], "input": {}}
    half = len(partial_json) // 2
    for piece in (partial_json[:half], partial_json[half:]):
        delta = {"type": "input_json_delta", "partial_json": piece}
        events.append(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}))
    events[-2:-2] = [("content_block_start", {"type": "content_block_start", "index": index, "content_block": block})]
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": reply["usage"]["output_tokens"]},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def _client(partial_json: str, *, text_first: bool = False) -> anthropic.Anthropic:
    body = _sse(partial_json, text_first=text_first)
    transport = _HTTP.MockTransport(
        lambda request: _HTTP.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )
    return anthropic.Anthropic(api_key="test-key", http_client=_HTTP.Client(transport=transport))


def _drain(client: anthropic.Anthropic, surface: str) -> List[str]:
    """Consume one streamed reply on *surface*; the type of every event the caller got."""
    if surface == "create":
        return [e.type for e in client.messages.create(model=MODEL, max_tokens=64, messages=MESSAGES, stream=True)]
    with client.messages.stream(model=MODEL, max_tokens=64, messages=MESSAGES) as stream:
        return [e.type for e in stream]


def _traced(
    mock_client: Any, surface: str, capture_content: bool, partial_json: str, *, text_first: bool = False
) -> List[str]:
    client = _client(partial_json, text_first=text_first)
    provider = AnthropicProvider()
    provider.connect(client)

    @trace(mock_client, capture_config=CaptureConfig(capture_content=capture_content))
    def agent() -> List[str]:
        return _drain(client, surface)

    try:
        return agent()
    finally:
        provider.disconnect()


def _malformed_arguments_warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if "malformed tool_call JSON arguments" in r.getMessage()]


def _tool_use_inputs(invoke: Dict[str, Any]) -> List[Any]:
    return [b["input"] for b in invoke["output_message"]["blocks"] if b["type"] == "tool_use"]


# ---------------------------------------------------------------------------
# messages.create(stream=True): the adapter alone decodes the input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "partial_json",
    [pytest.param(DEEP, id="nested-past-recursion-limit"), pytest.param(BIG_INT, id="integer-past-digit-limit")],
)
class TestCreateStreamWithUndecodableInput:
    def test_with_capture_content_the_string_is_kept_and_logged_once(
        self, mock_client, capture_trace, caplog, default_int_str_digit_limit, partial_json
    ):
        with caplog.at_level(logging.WARNING):
            received = _traced(mock_client, "create", True, partial_json)

        # Every event reached the caller, as without instrumentation.
        assert received == _drain(_client(partial_json), "create")
        events = capture_trace["events"]
        tool_call = find_event(events, "tool.call")["payload"]
        assert (tool_call["id"], tool_call["tool_name"]) == (TOOL_USE["id"], TOOL_USE["name"])
        assert tool_call["arguments"] == partial_json
        assert _tool_use_inputs(find_event(events, "model.invoke")["payload"]) == [partial_json]
        warnings = _malformed_arguments_warnings(caplog)
        assert len(warnings) == 1, warnings
        assert partial_json[:200] in warnings[0] and partial_json[:201] not in warnings[0]

    def test_without_capture_content_the_input_is_neither_decoded_nor_logged(
        self, mock_client, capture_trace, caplog, default_int_str_digit_limit, partial_json
    ):
        with caplog.at_level(logging.WARNING):
            received = _traced(mock_client, "create", False, partial_json)

        assert received == _drain(_client(partial_json), "create")
        events = capture_trace["events"]
        tool_call = find_event(events, "tool.call")["payload"]
        assert (tool_call["id"], tool_call["tool_name"]) == (TOOL_USE["id"], TOOL_USE["name"])
        assert "arguments" not in tool_call
        assert "output_message" not in find_event(events, "model.invoke")["payload"]
        assert _malformed_arguments_warnings(caplog) == []


@pytest.mark.parametrize("surface", ["create", "stream"])
def test_input_after_a_text_block_reaches_the_trace(mock_client, capture_trace, surface):
    # Claude usually writes a sentence before calling a tool, so the tool_use block
    # is content block 1, not 0.
    _traced(mock_client, surface, True, json.dumps(TOOL_USE["input"]), text_first=True)

    events = capture_trace["events"]
    assert find_event(events, "tool.call")["payload"]["arguments"] == {"city": "Paris"}
    blocks = find_event(events, "model.invoke")["payload"]["output_message"]["blocks"]
    assert blocks == [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": TOOL_USE["id"], "tool_name": TOOL_USE["name"], "input": {"city": "Paris"}},
    ]


# ---------------------------------------------------------------------------
# messages.stream(): the SDK's own accumulator parses the input first
# ---------------------------------------------------------------------------


class TestMessagesStreamToolInput:
    def test_with_capture_content_truncated_input_is_kept_and_logged_once(self, mock_client, capture_trace, caplog):
        # The SDK's partial-JSON parser accepts input cut short, so it reaches the
        # adapter, which keeps the string the model sent.
        with caplog.at_level(logging.WARNING):
            received = _traced(mock_client, "stream", True, TRUNCATED)

        assert received == _drain(_client(TRUNCATED), "stream")
        events = capture_trace["events"]
        assert find_event(events, "tool.call")["payload"]["arguments"] == TRUNCATED
        assert _tool_use_inputs(find_event(events, "model.invoke")["payload"]) == [TRUNCATED]
        assert len(_malformed_arguments_warnings(caplog)) == 1

    def test_without_capture_content_truncated_input_is_neither_decoded_nor_logged(
        self, mock_client, capture_trace, caplog
    ):
        with caplog.at_level(logging.WARNING):
            _traced(mock_client, "stream", False, TRUNCATED)

        events = capture_trace["events"]
        tool_call = find_event(events, "tool.call")["payload"]
        assert tool_call["tool_name"] == TOOL_USE["name"]
        assert "arguments" not in tool_call
        assert SENTINEL not in json.dumps(events)
        assert not any(SENTINEL in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "partial_json",
        [pytest.param(DEEP, id="nested-past-recursion-limit"), pytest.param(BIG_INT, id="integer-past-digit-limit")],
    )
    @pytest.mark.parametrize("capture_content", [False, True])
    def test_input_the_sdk_rejects_fails_the_call_as_it_does_uninstrumented(
        self, mock_client, capture_trace, default_int_str_digit_limit, partial_json, capture_content
    ):
        # The SDK's accumulator rejects this input itself: the caller's loop fails
        # with the SDK's own error whether or not the adapter is connected. The
        # adapter must pass that error through untouched and record the failure.
        with pytest.raises(ValueError) as uninstrumented:
            _drain(_client(partial_json), "stream")
        with pytest.raises(ValueError) as instrumented:
            _traced(mock_client, "stream", capture_content, partial_json)

        assert str(instrumented.value) == str(uninstrumented.value)
        errors = [e["payload"] for e in find_events(capture_trace["events"], "agent.error")]
        stream_errors = [e for e in errors if e.get("name") == "anthropic.messages.stream"]
        assert [e["error_type"] for e in stream_errors] == ["ValueError"]


def test_a_stream_the_adapter_cannot_aggregate_is_still_recorded(mock_client, capture_trace, caplog, monkeypatch):
    # If aggregating the events fails, the call is still recorded with what does
    # not depend on the reply, and the failure is logged, not swallowed.
    def broken(events: List[Any]) -> Optional[Any]:
        raise RuntimeError("aggregation failed")

    monkeypatch.setattr(AnthropicProvider, "aggregate_stream", staticmethod(broken))
    with caplog.at_level(logging.WARNING):
        received = _traced(mock_client, "stream", True, json.dumps(TOOL_USE["input"]))

    assert received == _drain(_client(json.dumps(TOOL_USE["input"])), "stream")
    invoke = find_event(capture_trace["events"], "model.invoke")["payload"]
    assert invoke["name"] == "anthropic.messages.stream"
    assert invoke["model"] == MODEL
    assert invoke["latency_ms"] >= 0
    assert invoke["output_message"] is None
    assert "usage" not in invoke
    assert find_events(capture_trace["events"], "tool.call") == []
    assert any(
        r.levelno == logging.WARNING and "anthropic.messages.stream" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
