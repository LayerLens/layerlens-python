"""OpenAI prompt-cache writes (``cache_write_tokens``) are recorded and priced once.

OpenAI reports the input tokens a request wrote to the prompt cache as
``cache_write_tokens``: in ``usage.prompt_tokens_details`` on Chat Completions and
in ``usage.input_tokens_details`` on the Responses API (declared by openai-python
from 2.45.0). Two documented facts decide how the adapter must record and price
them (https://developers.openai.com/api/docs/guides/prompt-caching):

* The writes are part of the input tokens, as ``cached_tokens`` are: "Cache-write
  pricing is not an additive fee: input tokens use the uncached-input,
  cached-input, or cache-write rate". The guide's own cost function computes
  ``ordinary_input_tokens = input_tokens - cached_tokens - cache_write_tokens``.
* Only GPT-5.6 and later charge extra for them: "For GPT-5.6 and later, cache
  writes cost 1.25x the standard, uncached input-token rate". Earlier models have
  "No additional cache-write charge".

The bodies carry the ``usage`` members the guide documents (it names the fields
and gives the cost formula, not example counts) and are replayed through a real
``openai.OpenAI`` client over ``httpx.MockTransport``, so the SDK
does its own deserialization (an undeclared extra field before openai 2.45, a
declared one after). The bundled ``PRICING`` table has no GPT-5.6+ model, so
gpt-6-luna is priced the way a customer prices a new model, through
``LAYERLENS_PRICING_TABLE``, with its rates from
https://developers.openai.com/api/docs/pricing ($0.10 input, $0.125 cache
writes, $0.50 output per 1M tokens).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator

import httpx
import pytest

import openai
from openai.types.chat import ChatCompletion
from layerlens.instrument import trace
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.openai import OpenAIProvider
from layerlens.instrument.adapters.providers.pricing import PRICING_OVERRIDE_ENV, reset_pricing_cache

from ...conftest import find_event
from ..._recorded import load_recorded, mock_transport

#: gpt-6-luna per-1K USD rates from the pricing page ($0.10 / $0.50 per 1M).
_GPT_6_LUNA_RATES = {"gpt-6-luna": {"input": 0.0001, "output": 0.0005}}

#: Published per-token prices for gpt-6-luna, written out so each expected cost
#: below is derived from the pricing page rather than from the code under test.
_LUNA_INPUT = 0.10 / 1_000_000
_LUNA_CACHE_WRITE = 0.125 / 1_000_000
_LUNA_OUTPUT = 0.50 / 1_000_000


@pytest.fixture
def gpt_6_luna_priced(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(_GPT_6_LUNA_RATES))
    monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(path))
    reset_pricing_cache()
    yield
    monkeypatch.delenv(PRICING_OVERRIDE_ENV, raising=False)
    reset_pricing_cache()


def _chat_body(
    *,
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    completion_tokens: int,
) -> Dict[str, Any]:
    """A Chat Completions body with the documented ``usage`` members."""
    return {
        "id": "chatcmpl-cache-write-0001",
        "object": "chat.completion",
        "created": 1790000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "pong", "refusal": None, "annotations": []},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "audio_tokens": 0,
                "cache_write_tokens": cache_write_tokens,
                "cached_tokens": cached_tokens,
            },
            "completion_tokens_details": {
                "accepted_prediction_tokens": 0,
                "audio_tokens": 0,
                "reasoning_tokens": 0,
                "rejected_prediction_tokens": 0,
            },
        },
        "service_tier": "default",
    }


def _responses_body(*, model: str, input_tokens: int, cached_tokens: int, cache_write_tokens: int) -> Dict[str, Any]:
    """A Responses API body whose usage is a first request writing its whole
    12,000-token prefix to the cache (``cache_write_tokens == input_tokens``)."""
    output_tokens = 40
    return {
        "id": "resp_cache_write_0001",
        "object": "response",
        "created_at": 1790000000,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": [
            {
                "type": "message",
                "id": "msg_cache_write_0001",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "pong", "annotations": [], "logprobs": []}],
            }
        ],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "store": True,
        "temperature": 1,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1,
        "truncation": "disabled",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": input_tokens + output_tokens,
        },
        "metadata": {},
    }


def _client(body: Dict[str, Any]) -> openai.OpenAI:
    transport, _ = mock_transport({"interactions": [{"response": {"status_code": 200, "json": body}}]})
    return openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport))


#: An explicit cache breakpoint after a 1,024-token stable prefix: the 476 tokens
#: after it are ordinary input, the 1,024 before it are written to the cache.
_CHAT_WRITE: Dict[str, Any] = dict(
    model="gpt-6-luna", prompt_tokens=1500, cached_tokens=0, cache_write_tokens=1024, completion_tokens=12
)


class TestExtraction:
    def test_chat_completions_cache_write_tokens_are_recorded(self) -> None:
        response = ChatCompletion.model_validate(_chat_body(**_CHAT_WRITE))

        usage = OpenAIProvider.extract_meta(response)["usage"]

        assert usage["cache_write_tokens"] == 1024
        # OpenAI's own counts are reported untouched.
        assert usage["prompt_tokens"] == 1500
        assert usage["cached_tokens"] == 0
        assert usage["total_tokens"] == 1512

    def test_a_response_that_reports_no_writes_carries_no_cache_write_key(self, mock_client, capture_trace) -> None:
        # A real body captured before OpenAI added the field (openai 2.32,
        # 2026-06-22). An absent count must stay absent, never become a made-up 0.
        fixture = load_recorded("openai", "default")
        transport, _ = mock_transport(fixture)
        client = openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=transport))
        OpenAIProvider().connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent() -> None:
            client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "ping"}])

        agent()
        events = capture_trace["events"]
        assert "cache_write_tokens" not in find_event(events, "model.invoke")["payload"]["usage"]
        assert "cache_write_tokens" not in find_event(events, "cost.record")["payload"]


class TestChatCompletions:
    def test_cache_writes_reach_the_invoke_the_cost_record_and_the_price(
        self, mock_client, capture_trace, gpt_6_luna_priced
    ) -> None:
        client = _client(_chat_body(**_CHAT_WRITE))
        OpenAIProvider().connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent() -> None:
            client.chat.completions.create(model="gpt-6-luna", messages=[{"role": "user", "content": "ping"}])

        agent()
        events = capture_trace["events"]

        invoke = find_event(events, "model.invoke")["payload"]
        assert invoke["usage"]["cache_write_tokens"] == 1024
        assert invoke["usage"]["prompt_tokens"] == 1500
        assert invoke["prompt_tokens"] == 1500
        assert invoke["otel_gen_ai"]["gen_ai.usage.input_tokens"] == 1500
        assert invoke["otel_gen_ai"]["gen_ai.usage.cache_creation_input_tokens"] == 1024

        cost = find_event(events, "cost.record")["payload"]
        assert cost["cache_write_tokens"] == 1024
        assert cost["prompt_tokens"] == 1500
        # 476 ordinary input + 1,024 written at the published write price + 12 output.
        expected = 476 * _LUNA_INPUT + 1024 * _LUNA_CACHE_WRITE + 12 * _LUNA_OUTPUT
        assert cost["cost_usd"] == pytest.approx(expected)  # 0.0001816
        # Every billed token was rated exactly once, so nothing is flagged as unpriced.
        assert "cost_status" not in cost

    def test_cache_writes_survive_the_default_redaction(self, mock_client, capture_trace) -> None:
        # The default config strips content (capture_content=False). Token counts
        # are structure, not content, so the cache-write count must survive it.
        client = _client(_chat_body(**_CHAT_WRITE))
        OpenAIProvider().connect(client)

        @trace(mock_client)
        def agent() -> None:
            client.chat.completions.create(model="gpt-6-luna", messages=[{"role": "user", "content": "secret prompt"}])

        agent()
        events = capture_trace["events"]
        invoke = find_event(events, "model.invoke")["payload"]
        assert invoke["usage"]["cache_write_tokens"] == 1024
        assert "messages" not in invoke, "the default config must still redact content"
        assert find_event(events, "cost.record")["payload"]["cache_write_tokens"] == 1024


class TestResponsesApi:
    def test_a_first_request_that_writes_its_prefix_is_priced_at_the_write_rate(
        self, mock_client, capture_trace, gpt_6_luna_priced
    ) -> None:
        client = _client(
            _responses_body(model="gpt-6-luna", input_tokens=12000, cached_tokens=0, cache_write_tokens=12000)
        )
        OpenAIProvider().connect(client)

        @trace(mock_client, capture_config=CaptureConfig.full())
        def agent() -> None:
            client.responses.create(model="gpt-6-luna", input="ping")

        agent()
        events = capture_trace["events"]

        invoke = find_event(events, "model.invoke")["payload"]
        assert invoke["name"] == "openai.responses.create"
        assert invoke["usage"]["cache_write_tokens"] == 12000
        assert invoke["usage"]["prompt_tokens"] == 12000

        cost = find_event(events, "cost.record")["payload"]
        assert cost["cache_write_tokens"] == 12000
        expected = 12000 * _LUNA_CACHE_WRITE + 40 * _LUNA_OUTPUT
        assert cost["cost_usd"] == pytest.approx(expected)  # 0.00152
        assert "cost_status" not in cost
