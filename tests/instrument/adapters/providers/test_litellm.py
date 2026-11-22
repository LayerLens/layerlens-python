from __future__ import annotations

import sys
import json
import types
import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Iterator, Optional
from unittest.mock import Mock

import httpx
import pytest

import openai
from layerlens.instrument import trace
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.litellm import (
    LiteLLMProvider,
    _route_provider,
    instrument_litellm,
    uninstrument_litellm,
)
from layerlens.instrument.adapters.providers.pricing import PRICING, calculate_cost
from layerlens.instrument.adapters.providers.token_usage import NormalizedTokenUsage

from .conftest import (
    make_openai_response,
    make_openai_response_no_usage,
    make_openai_response_empty_choices,
)
from ...conftest import find_event, find_events
from ..._recorded import load_recorded
from .test_openai_tool_arguments import _chat_stream_body


def _expected_cost(model: str, usage: dict) -> float:
    """Recompute the expected cost_usd from the SAME usage the event carries, so
    the assertion bites on a dropped cost_usd OR a changed pricing formula."""
    return calculate_cost(
        model,
        NormalizedTokenUsage(
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
            cached_tokens=usage.get("cached_tokens"),
        ),
        PRICING,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_mock_litellm(response=None):
    """Inject a fake litellm module into sys.modules with real OpenAI response types."""
    mock_mod = types.ModuleType("litellm")
    mock_mod.completion = Mock(return_value=response or make_openai_response())
    mock_mod.acompletion = Mock()
    sys.modules["litellm"] = mock_mod
    return mock_mod


def _remove_mock_litellm():
    uninstrument_litellm()
    for key in list(sys.modules.keys()):
        if key.startswith("litellm"):
            del sys.modules[key]


def _openai_chunk(
    *,
    content: Optional[str] = None,
    role: Optional[str] = None,
    finish_reason: Optional[str] = None,
    model: Optional[str] = None,
    response_id: Optional[str] = None,
    usage: Any = None,
) -> SimpleNamespace:
    """An OpenAI-shaped streaming chunk (litellm yields these)."""
    delta = SimpleNamespace(content=content, role=role, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, index=0)
    return SimpleNamespace(
        choices=[choice],
        model=model,
        id=response_id,
        system_fingerprint=None,
        service_tier=None,
        usage=usage,
    )


def _install_streaming_litellm(chunks: List[Any], model: str = "gpt-4") -> Any:
    """Inject a fake litellm whose completion(stream=True) yields *chunks*."""
    mock_mod = types.ModuleType("litellm")

    def _completion(**kwargs: Any) -> Any:
        if kwargs.get("stream") is True:
            return iter(chunks)
        return make_openai_response(model=model)

    mock_mod.completion = Mock(side_effect=_completion)
    mock_mod.acompletion = Mock()
    sys.modules["litellm"] = mock_mod
    return mock_mod


# ---------------------------------------------------------------------------
# Emit events
# ---------------------------------------------------------------------------


class TestEmitsEvents:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_model_invoke_and_cost_record(self, mock_client, capture_trace):
        instrument_litellm()

        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            import litellm

            r = litellm.completion(model="gpt-4", messages=[{"role": "user", "content": "Hi"}])
            return r.choices[0].message.content

        my_agent()
        events = capture_trace["events"]

        model_invoke = find_event(events, "model.invoke")
        assert model_invoke["payload"]["name"] == "litellm.completion"
        assert model_invoke["payload"]["model"] == "gpt-4"
        assert model_invoke["payload"]["output_message"]["content"] == "Hello!"
        assert model_invoke["payload"]["usage"]["total_tokens"] == 15
        assert "latency_ms" in model_invoke["payload"]
        # S19/F12: the framework stamp is the integration ('litellm'), NOT the
        # routed underlying provider ('openai' here) that priced the call.
        assert model_invoke["payload"]["framework"] == "litellm"

        # Exactly ONE cost.record per priced provider call (no double-count
        # across the model.invoke/_emit_cost fork) — LAY-3572 / B2.
        costs = find_events(events, "cost.record")
        assert len(costs) == 1, f"expected exactly one cost.record, got {len(costs)}"
        cost = costs[0]
        # LAY-3455: the cost event is attributed to the underlying routed
        # provider (gpt-4 -> openai), not the "litellm" router itself.
        assert cost["payload"]["provider"] == "openai"
        assert cost["payload"]["total_tokens"] == 15
        # The cost_usd VALUE must be present and correct, not just the tokens
        # (LAY-3572 / B1 / W1 — the old test never asserted the value).
        expected = _expected_cost("gpt-4", model_invoke["payload"]["usage"])
        assert expected is not None and expected > 0, "gpt-4 must price to a positive cost"
        assert cost["payload"]["cost_usd"] == expected, "cost_usd missing or miscomputed on the provider cost.record"

    def test_framework_is_litellm_even_for_routed_provider(self, mock_client, capture_trace):
        # The audit's headline litellm case: a gemini model routes to the
        # 'google' underlying provider, but the framework column must read
        # 'litellm' (the integration), not 'google' (S19/F12).
        self.mock_litellm.completion = Mock(return_value=make_openai_response(model="gemini-2.5-flash"))
        instrument_litellm()

        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            import litellm

            litellm.completion(model="gemini-2.5-flash", messages=[{"role": "user", "content": "Hi"}])

        my_agent()
        events = capture_trace["events"]
        # Sanity: the router really does classify gemini -> google.
        assert _route_provider("gemini-2.5-flash") == "google"
        mi = find_event(events, "model.invoke")["payload"]
        assert mi["framework"] == "litellm"
        cost = find_event(events, "cost.record")["payload"]
        assert cost["framework"] == "litellm"
        # The honest underlying provider is preserved on cost.record.
        assert cost["provider"] == "google"

    def test_error_emits_agent_error(self, mock_client, capture_trace):
        self.mock_litellm.completion = Mock(side_effect=RuntimeError("rate limited"))
        instrument_litellm()

        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            import litellm

            try:
                litellm.completion(model="gpt-4", messages=[])
            except RuntimeError:
                pass
            return "recovered"

        my_agent()
        events = capture_trace["events"]
        error = find_event(events, "agent.error")
        assert error["payload"]["error"] == "rate limited"
        assert "latency_ms" in error["payload"]


# ---------------------------------------------------------------------------
# Streaming (LAY G8 — litellm inherited the base no-op aggregate_stream, so a
# litellm.completion(stream=True) emitted ZERO model.invoke / cost telemetry)
# ---------------------------------------------------------------------------


class TestStreaming:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_streaming_emits_model_invoke_and_cost(self, mock_client, capture_trace):
        from .test_streaming import _openai_chunk

        usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8)

        def fake_stream(**kwargs):
            yield _openai_chunk(role="assistant", content="hi", model="gpt-4o", response_id="c1")
            yield _openai_chunk(content=" there", usage=usage, finish_reason="stop")

        self.mock_litellm.completion = Mock(side_effect=lambda **kw: fake_stream(**kw))
        instrument_litellm()

        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            import litellm

            stream = litellm.completion(model="gpt-4o", messages=[{"role": "user", "content": "Hi"}], stream=True)
            for _ in stream:
                pass
            return "done"

        my_agent()
        events = capture_trace["events"]
        model_invoke = find_event(events, "model.invoke")
        assert model_invoke["payload"]["usage"]["total_tokens"] == 8, "streamed usage not aggregated"
        assert model_invoke["payload"].get("output_message", {}).get("content") == "hi there", (
            "streamed content chunks not concatenated"
        )
        costs = find_events(events, "cost.record")
        assert len(costs) == 1, f"streaming call must emit exactly one cost.record, got {len(costs)}"
        cost = costs[0]
        assert cost["payload"]["total_tokens"] == 8
        # cost_usd must be priced from the AGGREGATED streamed usage (W2).
        expected = _expected_cost("gpt-4o", model_invoke["payload"]["usage"])
        assert expected is not None and expected > 0
        assert cost["payload"]["cost_usd"] == expected, "streaming cost_usd missing or miscomputed"

    def test_stream_emits_single_model_invoke_and_cost_record(self, mock_client, capture_trace):
        """Before LAY-3621 LiteLLMProvider had no aggregate_stream override, so
        the base returned None and the streamed call produced ZERO model.invoke
        / cost.record events. This test bites: revert aggregate_stream and the
        ``find_event`` calls below raise (no events emitted)."""
        usage = SimpleNamespace(prompt_tokens=7, completion_tokens=4, total_tokens=11)
        chunks = [
            _openai_chunk(role="assistant", content="Hel", model="gpt-4", response_id="chatcmpl-s1"),
            _openai_chunk(content="lo"),
            _openai_chunk(content="!", usage=usage, finish_reason="stop"),
        ]
        self.mock_litellm = _install_streaming_litellm(chunks)
        instrument_litellm()

        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            import litellm

            for _ in litellm.completion(model="gpt-4", messages=[{"role": "user", "content": "Hi"}], stream=True):
                pass
            return "done"

        my_agent()
        events = capture_trace["events"]

        invokes = find_events(events, "model.invoke")
        assert len(invokes) == 1, f"expected exactly one model.invoke, got {len(invokes)}"
        payload = invokes[0]["payload"]
        assert payload["name"] == "litellm.completion"
        assert payload["output_message"]["content"] == "Hello!"
        assert "ttft_ms" in payload  # streaming path was taken

        costs = find_events(events, "cost.record")
        assert len(costs) == 1, f"expected exactly one cost.record, got {len(costs)}"
        assert costs[0]["payload"]["total_tokens"] == 11  # non-None tokens aggregated from stream


# ---------------------------------------------------------------------------
# Passthrough / no-op behavior
# ---------------------------------------------------------------------------


class TestPassthrough:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_no_op_outside_trace(self):
        instrument_litellm()
        import litellm

        result = litellm.completion(model="gpt-4", messages=[])
        assert result.choices[0].message.content == "Hello!"


# ---------------------------------------------------------------------------
# Connect / disconnect lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_uninstrument_restores_original(self):
        original = self.mock_litellm.completion
        instrument_litellm()
        assert self.mock_litellm.completion is not original
        uninstrument_litellm()
        assert self.mock_litellm.completion is original

    def test_disconnect_when_not_connected(self):
        provider = LiteLLMProvider()
        provider.disconnect()  # should not raise

    def test_double_connect_is_idempotent(self):
        """connect() guards against double-wrapping (litellm.py:43,48): a second
        connect() with no intervening disconnect must not re-wrap or clobber the
        saved original, so exactly one wrap survives and disconnect fully
        restores. Verification + regression pin — the guard already holds.
        """
        original_completion = self.mock_litellm.completion
        original_acompletion = self.mock_litellm.acompletion

        provider = LiteLLMProvider()
        provider.connect()
        wrapped_completion = self.mock_litellm.completion
        wrapped_acompletion = self.mock_litellm.acompletion
        # First connect wrapped both methods...
        assert wrapped_completion is not original_completion
        assert wrapped_acompletion is not original_acompletion
        # ...and saved the TRUE originals for restoration.
        assert provider._originals["completion"] is original_completion
        assert provider._originals["acompletion"] is original_acompletion

        # Second connect without disconnect must be a no-op for wrapping.
        provider.connect()
        assert self.mock_litellm.completion is wrapped_completion  # not re-wrapped
        assert self.mock_litellm.acompletion is wrapped_acompletion
        # ...and must NOT clobber the saved originals with the wrappers.
        assert provider._originals["completion"] is original_completion
        assert provider._originals["acompletion"] is original_acompletion

        # disconnect fully restores the true originals (no residual wrapper).
        provider.disconnect()
        assert self.mock_litellm.completion is original_completion
        assert self.mock_litellm.acompletion is original_acompletion


# ---------------------------------------------------------------------------
# adapter_info
# ---------------------------------------------------------------------------


class TestAdapterInfo:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_info_before_connect(self):
        provider = LiteLLMProvider()
        info = provider.adapter_info()
        assert info.name == "litellm"
        assert info.adapter_type == "provider"
        assert info.connected is False

    def test_info_after_connect(self):
        provider = LiteLLMProvider()
        provider.connect()
        info = provider.adapter_info()
        assert info.connected is True

    def test_info_after_disconnect(self):
        provider = LiteLLMProvider()
        provider.connect()
        provider.disconnect()
        assert provider.adapter_info().connected is False


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


class TestConvenienceAPI:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_instrument_and_uninstrument(self):
        original = self.mock_litellm.completion
        instrument_litellm()
        assert self.mock_litellm.completion is not original
        uninstrument_litellm()
        assert self.mock_litellm.completion is original


# ---------------------------------------------------------------------------
# capture_params filtering
# ---------------------------------------------------------------------------


class TestCaptureParams:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm()

    def teardown_method(self):
        _remove_mock_litellm()

    def test_captured_params_included(self, mock_client, capture_trace):
        instrument_litellm()

        @trace(mock_client)
        def my_agent():
            import litellm

            litellm.completion(
                model="gpt-4",
                temperature=0.7,
                top_p=0.9,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return "done"

        my_agent()
        params = find_event(capture_trace["events"], "model.invoke")["payload"]["parameters"]
        assert params["model"] == "gpt-4"
        assert params["temperature"] == 0.7
        assert params["top_p"] == 0.9

    def test_non_captured_params_excluded(self, mock_client, capture_trace):
        instrument_litellm()

        @trace(mock_client)
        def my_agent():
            import litellm

            litellm.completion(
                model="gpt-4",
                messages=[],
                api_key="sk-123",
            )
            return "done"

        my_agent()
        params = find_event(capture_trace["events"], "model.invoke")["payload"]["parameters"]
        assert "api_key" not in params
        assert "messages" not in params


# ---------------------------------------------------------------------------
# Extractor edge cases (LiteLLM reuses OpenAI extractors, real types)
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_extract_output_normal(self):
        r = make_openai_response(content="LiteLLM response")
        output = LiteLLMProvider.extract_output(r)
        assert output == {"role": "assistant", "content": "LiteLLM response"}

    def test_extract_output_empty_choices(self):
        r = make_openai_response_empty_choices()
        assert LiteLLMProvider.extract_output(r) is None

    def test_extract_meta_normal(self):
        r = make_openai_response(model="gpt-4o", prompt_tokens=5, completion_tokens=3, total_tokens=8)
        meta = LiteLLMProvider.extract_meta(r)
        assert meta["response_model"] == "gpt-4o"
        assert meta["usage"]["total_tokens"] == 8

    def test_extract_meta_no_usage(self):
        r = make_openai_response_no_usage()
        meta = LiteLLMProvider.extract_meta(r)
        assert "usage" not in meta


# ---------------------------------------------------------------------------
# LAY-3455: routing classification -> underlying provider
# ---------------------------------------------------------------------------


class TestRouteProvider:
    def test_bare_openai_models(self):
        assert _route_provider("gpt-4o") == "openai"
        assert _route_provider("gpt-4") == "openai"
        assert _route_provider("o1-preview") == "openai"
        assert _route_provider("o3-mini") == "openai"
        assert _route_provider("chatgpt-4o-latest") == "openai"

    def test_bare_other_families(self):
        assert _route_provider("claude-3-5-sonnet") == "anthropic"
        assert _route_provider("gemini-1.5-pro") == "google"
        assert _route_provider("command-r-plus") == "cohere"
        assert _route_provider("mistral-large") == "mistral"
        assert _route_provider("mixtral-8x7b") == "mistral"
        assert _route_provider("llama3-70b") == "meta"
        assert _route_provider("totally-unknown-model") == "litellm"

    def test_prefixed_route_strings(self):
        assert _route_provider("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0") == "bedrock"
        assert _route_provider("azure/gpt-4o") == "azure"
        assert _route_provider("vertex_ai/gemini-1.5-pro") == "google_vertex"
        assert _route_provider("gemini/gemini-1.5-flash") == "google"
        assert _route_provider("ollama/llama3") == "ollama"
        assert _route_provider("anthropic/claude-3-opus") == "anthropic"
        assert _route_provider("openrouter/some-model") == "openrouter"
        # Unknown prefix falls through verbatim.
        assert _route_provider("customvendor/some-model") == "customvendor"

    def test_classify_provider_reads_model_kwarg(self):
        assert LiteLLMProvider.classify_provider("litellm.completion", {"model": "claude-3-5-sonnet"}) == "anthropic"
        assert LiteLLMProvider.classify_provider("litellm.completion", {}) is None


class TestRoutingEndToEnd:
    def setup_method(self):
        self.mock_litellm = _install_mock_litellm(make_openai_response(model="claude-3-5-sonnet"))

    def teardown_method(self):
        _remove_mock_litellm()

    def test_cost_record_provider_is_underlying_provider(self, mock_client, capture_trace):
        """A litellm.completion(model="claude-3-5-sonnet") must emit cost.record
        with provider="anthropic" (not "litellm"). Revert classify_provider and
        this asserts on "litellm" instead -> fails."""
        instrument_litellm()

        @trace(mock_client)
        def my_agent():
            import litellm

            litellm.completion(model="claude-3-5-sonnet", messages=[{"role": "user", "content": "Hi"}])
            return "done"

        my_agent()
        cost = find_event(capture_trace["events"], "cost.record")
        assert cost["payload"]["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# Tool calls the model requested, through the REAL litellm package. litellm
# returns OpenAI's response shape for every provider it routes to, so the OpenAI
# tool-call hook applies; the adapter inherited the base hook, which returns no
# tool calls, so a litellm call never produced a tool.call.
# ---------------------------------------------------------------------------

_TOOL_CALL_ID = "call_N0cYZsPSiRm7o3khCvuy6J3M"
_SENTINEL = "LL-SENTINEL-4267"


def _tool_call_body(arguments: Optional[str] = None) -> Dict[str, Any]:
    """The recorded OpenAI tool-call response body, as litellm returns it for a
    routed call; *arguments* replaces the model's arguments (in memory only)."""
    body: Dict[str, Any] = load_recorded("openai", "tool_call")["interactions"][0]["response"]["json"]
    if arguments is not None:
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    return body


def _malformed_arguments_warnings(caplog: Any) -> List[str]:
    return [r.getMessage() for r in caplog.records if "malformed tool_call JSON arguments" in r.getMessage()]


@pytest.fixture
def real_litellm() -> Iterator[Any]:
    """The real litellm module, instrumented for the test and restored after it.

    Imported here rather than at module level: the other tests in this module put a
    fake ``litellm`` in ``sys.modules`` and evict every ``litellm*`` module when they
    finish, so a module-level reference could be a module the adapter never patched.
    """
    import litellm

    instrument_litellm()
    try:
        yield litellm
    finally:
        uninstrument_litellm()


class TestToolCalls:
    def test_tool_call_emitted(self, mock_client, capture_trace, real_litellm):
        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            response = real_litellm.completion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "What's the weather in Paris?"}],
                mock_response=_tool_call_body(),
            )
            return type(response).__name__

        # The real litellm class, not a stand-in.
        assert my_agent() == "ModelResponse"
        events = capture_trace["events"]
        tool_call = find_event(events, "tool.call")["payload"]
        assert tool_call["tool_name"] == "get_weather"
        assert tool_call["id"] == _TOOL_CALL_ID
        # '{"city":"Paris"}' on the wire, an object in the trace, as for OpenAI.
        assert tool_call["arguments"] == {"city": "Paris"}
        assert tool_call["model"] == "gpt-4o-mini-2024-07-18"
        assert tool_call["provider"] == "openai"
        assert tool_call["framework"] == "litellm"
        output = find_event(events, "model.invoke")["payload"]["output_message"]
        assert output["tool_calls"][0]["arguments"] == {"city": "Paris"}

    def test_async_tool_call_emitted(self, mock_client, capture_trace, real_litellm):
        @trace(mock_client, capture_config=CaptureConfig.full())
        async def my_agent():
            response = await real_litellm.acompletion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "What's the weather in Paris?"}],
                mock_response=_tool_call_body(),
            )
            return type(response).__name__

        assert asyncio.run(my_agent()) == "ModelResponse"
        events = capture_trace["events"]
        assert find_event(events, "model.invoke")["payload"]["name"] == "litellm.acompletion"
        tool_call = find_event(events, "tool.call")["payload"]
        assert tool_call["tool_name"] == "get_weather"
        assert tool_call["id"] == _TOOL_CALL_ID
        assert tool_call["arguments"] == {"city": "Paris"}

    def test_streamed_tool_call_emitted(self, mock_client, capture_trace, real_litellm):
        # litellm streams an OpenAI model through the openai client it is handed, so the
        # recorded tool call is served as OpenAI's SSE chunks and litellm turns them into
        # its own stream. (Its ``mock_response`` cannot stream a tool call on every
        # litellm the lock resolves: 1.74 only streams a string.)
        recorded = _tool_call_body()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        requests: List[httpx.Request] = []

        def serve(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = _chat_stream_body(recorded)
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        client = openai.OpenAI(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(serve)))

        @trace(mock_client, capture_config=CaptureConfig.full())
        def my_agent():
            stream = real_litellm.completion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "What's the weather in Paris?"}],
                stream=True,
                client=client,
            )
            return [type(chunk).__name__ for chunk in stream]

        # litellm's own stream: ModelResponseStream chunks carrying the tool-call deltas,
        # fetched through the given client.
        assert set(my_agent()) == {"ModelResponseStream"}
        assert [(r.method, r.url.path) for r in requests] == [("POST", "/v1/chat/completions")]
        tool_call = find_event(capture_trace["events"], "tool.call")["payload"]
        assert tool_call["tool_name"] == "get_weather"
        assert tool_call["id"] == _TOOL_CALL_ID
        assert tool_call["arguments"] == {"city": "Paris"}

    @pytest.mark.parametrize("capture_content", [False, True])
    def test_malformed_arguments_are_parsed_and_logged_only_with_capture_content(
        self, mock_client, capture_trace, caplog, real_litellm, capture_content
    ):
        malformed = '{"city": "' + _SENTINEL

        @trace(mock_client, capture_config=CaptureConfig(capture_content=capture_content))
        def my_agent():
            real_litellm.completion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "What's the weather in Paris?"}],
                mock_response=_tool_call_body(malformed),
            )
            return "done"

        with caplog.at_level(logging.WARNING):
            my_agent()

        events = capture_trace["events"]
        tool_call = find_event(events, "tool.call")["payload"]
        assert tool_call["tool_name"] == "get_weather"
        invoke = find_event(events, "model.invoke")["payload"]
        warnings = _malformed_arguments_warnings(caplog)
        if capture_content:
            # Parsed once for the two places it appears: one WARNING, and the string
            # the model sent in both.
            assert len(warnings) == 1, warnings
            assert tool_call["arguments"] == malformed
            assert invoke["output_message"]["tool_calls"][0]["arguments"] == malformed
        else:
            # Not parsed at all: no WARNING (it would carry a snippet of the arguments),
            # and nothing of them in the trace.
            assert warnings == []
            assert "arguments" not in tool_call
            assert "output_message" not in invoke
            assert _SENTINEL not in json.dumps(events)
