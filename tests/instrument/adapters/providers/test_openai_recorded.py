"""Recorded-real-response replay for the OpenAI provider (LAY-3614).

Replays a REAL captured ``chat.completions`` wire body through a real
``openai.OpenAI`` client over ``httpx.MockTransport`` and asserts the adapter's
emitted events. The fixture is the provider's raw response (recorded upstream of
the parser); the assertions are the events (downstream). Unlike the hand-built
``conftest.make_openai_response`` doubles — which fabricate token counts and omit
real fields — these run against the actual OpenAI shape, including
``prompt_tokens_details`` / ``completion_tokens_details`` / ``service_tier``.

See ``tests/instrument/_recorded.py`` for the corpus design + its snapshot limit.
"""

from __future__ import annotations

import sys
import json
import logging

import httpx
import pytest

import openai
from layerlens.instrument import trace
from layerlens.instrument._secret_scrub import DEPTH_REDACTION, MAX_SCRUB_DEPTH
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.openai import OpenAIProvider

from ...conftest import find_event
from ..._recorded import load_recorded, mock_transport


def _client(fixture):
    transport, requests = mock_transport(fixture)
    client = openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport))
    return client, requests


class TestOpenAIRecorded:
    def test_default_real_shape(self, mock_client, capture_trace):
        fixture = load_recorded("openai", "default")
        client, _ = _client(fixture)
        provider = OpenAIProvider()
        provider.connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent():
            r = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Reply with exactly: pong"}],
            )
            return r.choices[0].message.content

        assert agent() == "pong"

        events = capture_trace["events"]
        mi = find_event(events, "model.invoke")
        assert mi["payload"]["name"] == "openai.chat.completions.create"
        # The response (dated) model wins over the requested alias.
        assert mi["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert mi["payload"]["response_model"] == "gpt-4o-mini-2024-07-18"
        assert mi["payload"]["finish_reason"] == "stop"
        assert mi["payload"]["system_fingerprint"] == "fp_2ff2473d75"
        # Real-shape fields the fabricated doubles never carried:
        assert mi["payload"]["service_tier"] == "default"
        assert mi["payload"]["output_message"] == {"role": "assistant", "content": "pong"}
        usage = mi["payload"]["usage"]
        assert usage["prompt_tokens"] == 12
        assert usage["completion_tokens"] == 1
        assert usage["total_tokens"] == 13
        # prompt_tokens_details / completion_tokens_details are real OpenAI shape:
        assert usage["cached_tokens"] == 0
        assert usage["reasoning_tokens"] == 0

        cost = find_event(events, "cost.record")
        assert cost["payload"]["provider"] == "openai"
        assert cost["payload"]["model"] == "gpt-4o-mini-2024-07-18"
        assert cost["payload"]["total_tokens"] == 13
        assert cost["payload"]["cost_usd"] > 0

        provider.disconnect()

    def test_tool_call_real_shape(self, mock_client, capture_trace):
        fixture = load_recorded("openai", "tool_call")
        client, _ = _client(fixture)
        provider = OpenAIProvider()
        provider.connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent():
            return client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                        },
                    }
                ],
            )

        agent()
        events = capture_trace["events"]
        tool_call = find_event(events, "tool.call")
        assert tool_call["payload"]["tool_name"] == "get_weather"
        # arguments arrive as a JSON string on the wire and are parsed downstream.
        assert tool_call["payload"]["arguments"] == {"city": "Paris"}
        assert tool_call["payload"]["provider"] == "openai"

        mi = find_event(events, "model.invoke")
        assert mi["payload"]["finish_reason"] == "tool_calls"
        assert mi["payload"]["usage"]["prompt_tokens"] == 54
        assert mi["payload"]["usage"]["completion_tokens"] == 14
        provider.disconnect()

    @pytest.mark.parametrize(
        "arguments",
        [
            # Valid JSON that Python still cannot decode, and neither failure is a
            # JSONDecodeError: an integer longer than int() converts (ValueError)
            # and nesting deeper than the decoder recurses (RecursionError).
            pytest.param('{"n": ' + "1" * 5000 + "}", id="integer-over-digit-limit"),
            pytest.param("[" * 50_000 + "]" * 50_000, id="nesting-over-recursion-limit"),
        ],
    )
    def test_undecodable_tool_arguments_do_not_fail_the_call(
        self, mock_client, capture_trace, caplog, default_int_str_digit_limit, arguments
    ):
        # The request succeeded and the reply arrived intact; instrumenting it must
        # not turn it into an exception raised from the caller's create().
        fixture = load_recorded("openai", "tool_call")
        fixture["interactions"][0]["response"]["json"]["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ] = arguments
        client, _ = _client(fixture)
        provider = OpenAIProvider()
        provider.connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent():
            return client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
            )

        with caplog.at_level(logging.WARNING):
            response = agent()

        assert response.choices[0].message.tool_calls[0].function.arguments == arguments
        tool_call = find_event(capture_trace["events"], "tool.call")
        assert tool_call["payload"]["tool_name"] == "get_weather"
        assert tool_call["payload"]["arguments"] == arguments
        assert any("malformed tool_call JSON arguments" in r.getMessage() for r in caplog.records)
        provider.disconnect()

    # An invariant gate member, as it was in test_secret_scrub.py: deep payloads never
    # raise into the caller.
    @pytest.mark.invariant
    def test_tool_arguments_nested_700_deep_do_not_fail_the_call(self, mock_client, capture_trace):
        """Arguments nested 700 levels are still decodable, so they reach the scrubber as
        a 700-level structure under ``capture_content=True``. Scrubbing it used to raise
        RecursionError out of the caller's create() (LAY-4267)."""
        # The depth sits between two limits on CPython <= 3.11, where the C JSON decoder
        # and Python frames share the 1,000 recursion limit. Above ~500 levels the old
        # scrubber (two frames per list level) raised. The decoder spends one level per
        # nesting level on top of the ~40 frames below it here, so 700 leaves it ~250
        # levels of headroom: the arguments decode, and the bounded scrubber is what runs.
        levels = 700
        assert levels < sys.getrecursionlimit() - 200
        arguments = "[" * levels + "]" * levels
        assert isinstance(json.loads(arguments), list)
        fixture = load_recorded("openai", "tool_call")
        fixture["interactions"][0]["response"]["json"]["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ] = arguments
        client, _ = _client(fixture)
        provider = OpenAIProvider()
        provider.connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent():
            return client.chat.completions.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": "What's the weather in Paris?"}]
            )

        try:
            response = agent()
        finally:
            provider.disconnect()

        # The caller gets the reply untouched, and the trace was serialized and uploaded.
        assert response.choices[0].message.tool_calls[0].function.arguments == arguments
        events = capture_trace["events"]
        assert find_event(events, "model.invoke")["payload"]["finish_reason"] == "tool_calls"
        tool_call = find_event(events, "tool.call")["payload"]
        assert tool_call["tool_name"] == "get_weather"
        # The payload is level 0 and ``arguments`` level 1: the levels above the bound are
        # kept, and the first container at the bound is replaced by the marker.
        expected = DEPTH_REDACTION
        for _ in range(MAX_SCRUB_DEPTH - 1):
            expected = [expected]
        assert tool_call["arguments"] == expected

    def test_provenance_is_stamped(self):
        fixture = load_recorded("openai", "default")
        prov = fixture["provenance"]
        assert prov["provider"] == "openai"
        assert prov["scenario"] == "default"
        # captured_at makes staleness visible (snapshot, not freshness).
        assert prov["captured_at"] and prov["captured_at"] != "pending-creds"
