"""Centralized price-on-emit chokepoint (A1 / LAY-3626).

Pricing was copy-pasted across ~10 emit paths; the one true chokepoint every
event passes — ``TraceCollector.emit`` — priced nothing, so a new ``_fire``
override that forgot the hook shipped a tokens-only ``cost.record`` and
tier/cache-write pricing (A2/A3) only applied where each site remembered to pass
them. The collector now recomputes ``cost_usd`` for every ``cost.record`` from
its own payload (model + usage + service_tier + provider) — one place, every
path priced identically. These tests bite: drop the chokepoint and the
unpriced/tier/cache cases go RED.
"""

from __future__ import annotations

import pytest

from layerlens.instrument._collector import TraceCollector
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.providers.pricing import calculate_cost
from layerlens.instrument.adapters.providers.token_usage import NormalizedTokenUsage

pytestmark = pytest.mark.invariant


def _cost_event(collector: TraceCollector) -> dict:
    return [e for e in collector.events if e["event_type"] == "cost.record"][0]


def _collector() -> TraceCollector:
    # capture_content=True so redaction never interferes with the cost fields.
    return TraceCollector(object(), CaptureConfig(capture_content=True))


def test_chokepoint_fills_missing_cost_usd() -> None:
    """A1/A11: a cost.record emitted WITHOUT cost_usd but with a priced model is
    priced centrally — no emit path can ship a tokens-only record."""
    c = _collector()
    c.emit(
        "cost.record",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "prompt_tokens": 1000,
            "completion_tokens": 1000,
            "total_tokens": 2000,
        },
        span_id="s1",
    )
    ev = _cost_event(c)
    expected = calculate_cost(
        "gpt-4o", NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000)
    )
    assert expected and expected > 0
    assert ev["payload"]["cost_usd"] == expected, "chokepoint did not price a tokens-only cost.record"


def test_chokepoint_applies_service_tier_centrally() -> None:
    """A2 at the chokepoint: a priority-tier cost.record emitted WITHOUT cost_usd
    is filled at ~2x standard — tier pricing flows through the central fill."""
    c = _collector()
    c.emit(
        "cost.record",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "prompt_tokens": 1000,
            "completion_tokens": 1000,
            "total_tokens": 2000,
            "service_tier": "priority",
        },
        span_id="s1",
    )
    ev = _cost_event(c)
    std = calculate_cost("gpt-4o", NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000))
    assert ev["payload"]["cost_usd"] == pytest.approx(std * 2.0), "service_tier not applied at the chokepoint"


def test_chokepoint_does_not_clobber_preset_cost_usd() -> None:
    """Fill-when-absent: an adapter-computed cost_usd (langfuse vendor cost,
    bedrock_agents, the _fire helpers) is PRESERVED, never overwritten."""
    c = _collector()
    c.emit(
        "cost.record",
        {
            "provider": "langfuse",
            "model": "gpt-4o",
            "prompt_tokens": 1000,
            "completion_tokens": 1000,
            "total_tokens": 2000,
            "cost_usd": 0.123,
        },
        span_id="s1",
    )
    ev = _cost_event(c)
    assert ev["payload"]["cost_usd"] == 0.123, "chokepoint clobbered an adapter-supplied cost_usd"


def test_chokepoint_prices_cache_creation_centrally() -> None:
    """A3 at the chokepoint: cache_creation (write) tokens add cost even when the
    payload uses the raw ``cache_creation_input_tokens`` wire key."""
    c = _collector()
    c.emit(
        "cost.record",
        {
            "provider": "anthropic",
            "model": "claude-3-5-sonnet",
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cache_creation_input_tokens": 1000,
        },
        span_id="s1",
    )
    ev = _cost_event(c)
    base = calculate_cost(
        "claude-3-5-sonnet", NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100)
    )
    assert ev["payload"]["cost_usd"] > base, "cache_creation not priced at the chokepoint"


class TestOpenAICacheWrites:
    """OpenAI's ``cache_write_tokens`` are part of ``prompt_tokens`` (LAY-4259).

    "Cache-write pricing is not an additive fee: input tokens use the
    uncached-input, cached-input, or cache-write rate", and only GPT-5.6 and later
    charge 1.25x input for a write; earlier models have "No additional cache-write
    charge" (https://developers.openai.com/api/docs/guides/prompt-caching). The
    chokepoint must rate every reported input token exactly once, at the rate its
    model is billed. Expected costs are worked by hand from published per-1M prices
    (https://developers.openai.com/api/docs/pricing, checked 2026-10-16).
    """

    def test_a_gpt_5_6_or_later_write_is_priced_at_the_published_write_price(self) -> None:
        # gpt-6-luna from the bundled table: $0.10 input, $0.125 cache writes,
        # $0.50 output per 1M.
        c = _collector()
        c.emit(
            "cost.record",
            {
                "provider": "openai",
                "model": "gpt-6-luna",
                "prompt_tokens": 1500,
                "completion_tokens": 12,
                "total_tokens": 1512,
                "cached_tokens": 0,
                "cache_write_tokens": 1024,
            },
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        # 476 ordinary input at $0.10 + 1,024 written at the published $0.125
        # cache-write price + 12 output at $0.50 (all per 1M).
        expected = 476 * 0.10 / 1_000_000 + 1024 * 0.125 / 1_000_000 + 12 * 0.50 / 1_000_000
        assert payload["cost_usd"] == pytest.approx(expected)  # 0.0001816
        assert "cost_status" not in payload

    def test_an_earlier_model_write_is_priced_at_the_plain_input_rate(self) -> None:
        # gpt-4o-mini from the bundled table: $0.15 input, $0.075 cached input,
        # $0.60 output per 1M, and no cache-write charge. 2,048 input tokens:
        # 1,024 read from the cache, 1,024 written to it, none ordinary.
        c = _collector()
        c.emit(
            "cost.record",
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "prompt_tokens": 2048,
                "completion_tokens": 16,
                "total_tokens": 2064,
                "cached_tokens": 1024,
                "cache_write_tokens": 1024,
            },
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        expected = 1024 * 0.075 / 1_000_000 + 1024 * 0.15 / 1_000_000 + 16 * 0.60 / 1_000_000
        assert payload["cost_usd"] == pytest.approx(expected)  # 0.00024
        assert "cost_status" not in payload

    def test_cache_reads_and_writes_are_each_priced_at_the_models_listed_price(self) -> None:
        # gpt-6.1-sol from the bundled table: $2 input, $0.10 cached input (a
        # twentieth of the input price), $2.50 cache writes, $10 output per 1M.
        # 20,000 input tokens: 16,000 read from the cache, 2,048 written to it and
        # 1,952 ordinary.
        c = _collector()
        c.emit(
            "cost.record",
            {
                "provider": "openai",
                "model": "gpt-6.1-sol",
                "prompt_tokens": 20_000,
                "completion_tokens": 1_000,
                "total_tokens": 21_000,
                "cached_tokens": 16_000,
                "cache_write_tokens": 2_048,
            },
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        expected = (
            1_952 * 2.00 / 1_000_000 + 16_000 * 0.10 / 1_000_000 + 2_048 * 2.50 / 1_000_000 + 1_000 * 10.00 / 1_000_000
        )
        assert payload["cost_usd"] == pytest.approx(expected)  # 0.020624
        assert "cost_status" not in payload

    def test_anthropic_cache_creation_is_priced_exactly_as_before(self) -> None:
        # Regression guard. anthropic.py's payload for claude-3-5-sonnet ($3
        # input, $15 output per 1M): 50 uncached input, 1,000 read at 0.1x,
        # 2,000 written at the 5-minute 1.25x, 100 output.
        c = _collector()
        c.emit(
            "cost.record",
            {
                "provider": "anthropic",
                "model": "claude-3-5-sonnet",
                "prompt_tokens": 1050,
                "input_tokens": 50,
                "cached_tokens": 1000,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 2000,
                "completion_tokens": 100,
                "output_tokens": 100,
            },
            span_id="s1",
        )
        expected = 50 * 3.0 / 1_000_000 + 1000 * 0.30 / 1_000_000 + 2000 * 3.75 / 1_000_000 + 100 * 15.0 / 1_000_000
        assert _cost_event(c)["payload"]["cost_usd"] == pytest.approx(expected)  # 0.00945


def test_chokepoint_leaves_unpriced_model_unpriced() -> None:
    """A model that resolves to no rate stays cost_usd=None (don't fabricate)."""
    c = _collector()
    c.emit(
        "cost.record",
        {
            "provider": "ollama",
            "model": "some-local-model-xyz",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
        span_id="s1",
    )
    ev = _cost_event(c)
    assert ev["payload"].get("cost_usd") is None


def test_chokepoint_uses_azure_table_for_azure_provider() -> None:
    """Provider routing: azure gpt-4o is priced from AZURE_PRICING, not PRICING
    (different rate) — the chokepoint must not mis-route to the OpenAI table."""
    from layerlens.instrument.adapters.providers.pricing import AZURE_PRICING

    c = _collector()
    c.emit(
        "cost.record",
        {"provider": "azure", "model": "gpt-4o", "prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000},
        span_id="s1",
    )
    ev = _cost_event(c)
    azure_expected = calculate_cost(
        "gpt-4o", NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=0, total_tokens=1000), AZURE_PRICING
    )
    assert ev["payload"]["cost_usd"] == azure_expected, "azure cost.record not priced from AZURE_PRICING"


class TestNeverFabricatesAZero:
    """The chokepoint must not turn an unpriceable payload into ``cost_usd: 0.0``.

    FILL-WHEN-ABSENT was written for the *dropped-price* case (an emit path forgot
    the hook). It also fires for a payload the formula cannot price at all — a
    totals-only usage — and the shared formula answers ``0.0`` there, because it
    sums four zeroes. The chokepoint then ships that zero as a computed price: a
    real, billed 1500-token gpt-4o call presented to the customer as free. This is
    the exact failure the chokepoint was built to prevent ("no path can ship a
    tokens-only cost.record"), inverted.

    The honest outcome is a cost that is ABSENT plus an explicit reason, so a
    fail-closed reader can tell "unknowable from this payload" apart from "a priced
    model's cost was dropped" (the A11 lock's job).

    Bite proof: revert the unpriceable-shape check and these go RED with 0.0.
    """

    def test_a_totals_only_record_ships_no_fabricated_zero(self) -> None:
        c = _collector()
        c.emit(
            "cost.record",
            {"provider": "openai", "model": "gpt-4o", "total_tokens": 1500},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload.get("cost_usd") != 0.0, (
            "the chokepoint fabricated $0.00 for a real billed 1500-token gpt-4o call"
        )
        assert payload.get("cost_usd") is None
        # the honest token count must survive — we suppress the price, not the usage
        assert payload["total_tokens"] == 1500

    def test_an_unpriceable_shape_is_marked_so_it_is_not_read_as_a_dropped_price(self) -> None:
        c = _collector()
        c.emit(
            "cost.record",
            {"provider": "openai", "model": "gpt-4o", "total_tokens": 1500},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload.get("cost_status") == "unpriceable_token_shape", (
            "a priced model with no priceable dimension must say WHY it has no cost, "
            "or it is indistinguishable from the A11 dropped-price bug"
        )

    def test_the_flat_framework_vocabulary_reaches_the_same_verdict(self) -> None:
        c = _collector()
        c.emit(
            "cost.record",
            {"provider": "openai", "model": "gpt-4o", "tokens_total": 1500},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload.get("cost_usd") != 0.0
        assert payload.get("cost_status") == "unpriceable_token_shape"

    def test_an_unpriced_model_is_not_marked_unpriceable(self) -> None:
        # BOUNDARY: a local/custom model has no rate at all. That is the
        # pre-existing, legal tokens-only case (A11 allows a None cost there) and
        # must NOT acquire the unpriceable-shape marker, which is specifically
        # about a PRICED model we could not price from this payload.
        c = _collector()
        c.emit(
            "cost.record",
            {"provider": "ollama", "model": "my-local-llama", "total_tokens": 1500},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload.get("cost_usd") is None
        assert "cost_status" not in payload

    def test_a_priceable_record_is_untouched(self) -> None:
        # BOUNDARY: the fix must not disturb the path the chokepoint exists for.
        c = _collector()
        c.emit(
            "cost.record",
            {"provider": "openai", "model": "gpt-4o", "prompt_tokens": 1000, "completion_tokens": 500},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload["cost_usd"] == calculate_cost(
            "gpt-4o", NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=500)
        )
        assert payload["cost_usd"] > 0
        assert "cost_status" not in payload


class TestPartialTokenShape:
    """The DETECTABLE under-report (LAY-3622 / F4): a real cost that understates.

    Cluster A killed the fabricated ``0.0``. The remaining case is the opposite
    shape: we DO price the record, but the provider reported more billed tokens than
    any rate was applied to. The canonical producer is Gemini, which reports
    ``thoughtsTokenCount`` OUTSIDE ``candidatesTokenCount`` while
    ``total_token_count`` includes it — thinking tokens bill at the output rate and
    none of them were priced.

    The chokepoint MARKS this and changes no money. Inventing a rate for tokens we
    cannot attribute would be a guess billed to a customer, which is the same class
    of dishonesty as A4b's derived zero, just in the other direction. Reading
    ``reasoning_tokens`` into the formula instead would DOUBLE-bill OpenAI (whose
    ``reasoning_tokens`` is a breakdown of ``completion_tokens``) and bill Anthropic
    from a ``chars // 4`` estimate — see ``pricing._cost_from_rates``.
    """

    #: The real Gemini thinking shape: total_token_count includes the 42 thought
    #: tokens, candidates_token_count does not.
    GEMINI_THINKING = {
        "provider": "google",
        "model": "gemini-2.5-pro",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 57,
        "reasoning_tokens": 42,
    }

    def test_a_reported_total_beyond_the_priced_sides_is_MARKED(self) -> None:
        c = _collector()
        c.emit("cost.record", dict(self.GEMINI_THINKING), span_id="s1")
        payload = _cost_event(c)["payload"]
        assert payload["cost_status"] == "partial_token_shape"
        assert payload["unpriced_tokens"] == 42, "the magnitude must ride along, not be re-derivable only"
        assert payload["cost_usd"] > 0, "the cost we CAN compute is still reported"

    def test_the_marker_changes_NO_money(self) -> None:
        # The whole point of mark-only. If a future change starts pricing the
        # residual, this is the assertion that makes it a deliberate act.
        c = _collector()
        c.emit("cost.record", dict(self.GEMINI_THINKING), span_id="s1")
        marked = _cost_event(c)["payload"]["cost_usd"]
        assert marked == calculate_cost(
            "gemini-2.5-pro", NormalizedTokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=57)
        )

    def test_a_complete_token_shape_is_NOT_marked(self) -> None:
        # VACUITY CONTROL: if everything were marked, the marker would mean nothing.
        c = _collector()
        c.emit(
            "cost.record",
            {"provider": "openai", "model": "gpt-4o", "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert "cost_status" not in payload
        assert "unpriced_tokens" not in payload

    def test_a_fully_cached_turn_is_NOT_marked(self) -> None:
        # THE FALSE POSITIVE the obvious arithmetic produces. A turn reported as
        # fully cached carries prompt_tokens=0 with cached_tokens=1500, and
        # `prompt + completion + cache_creation` reads that as 1500 unpriced tokens —
        # when in fact every one of them was priced through the cached leg. This
        # shape is real: test_pricing.py's `[cached-only]` case.
        c = _collector()
        c.emit(
            "cost.record",
            {
                "provider": "openai",
                "model": "gpt-4o",
                "prompt_tokens": 0,
                "cached_tokens": 1500,
                "total_tokens": 1500,
            },
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload["cost_usd"] > 0
        assert "cost_status" not in payload, "a fully-cached turn was wrongly reported as a partial under-price"

    def test_a_VENDOR_reported_cost_is_never_marked(self) -> None:
        # A gateway's own charge (langfuse billing figure, OpenRouter usage
        # accounting) is a billed FACT, not our estimate of one, so a token gap
        # against it says nothing about its accuracy.
        c = _collector()
        c.emit(
            "cost.record",
            {**self.GEMINI_THINKING, "cost_usd": 0.99, "cost_source": "langfuse"},
            span_id="s1",
        )
        payload = _cost_event(c)["payload"]
        assert payload["cost_usd"] == 0.99
        assert "cost_status" not in payload

    def test_a_totals_only_shape_stays_UNPRICEABLE_not_partial(self) -> None:
        # The two markers must not blur: this one has no cost at all, so calling it
        # "partial" would turn an honestly-withheld cost into an apparently-real one.
        c = _collector()
        c.emit("cost.record", {"provider": "openai", "model": "gpt-4o", "total_tokens": 1500}, span_id="s1")
        payload = _cost_event(c)["payload"]
        assert payload["cost_status"] == "unpriceable_token_shape"
        assert payload.get("cost_usd") is None
        assert "unpriced_tokens" not in payload

    def test_an_adapter_supplied_cost_status_is_not_clobbered(self) -> None:
        c = _collector()
        c.emit("cost.record", {**self.GEMINI_THINKING, "cost_usd": 0.5, "cost_status": "vendor_says_so"}, span_id="s1")
        assert _cost_event(c)["payload"]["cost_status"] == "vendor_says_so"
