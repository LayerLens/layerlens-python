"""Pricing-table tests covering LAY-3330 ACs.

The acceptance criteria are:

* Cost calculated dynamically from a pricing table (not inline rates).
* Default pricing covers all current OpenAI models (GPT-4o, mini, turbo,
  GPT-4, GPT-3.5, o1, o1-mini, o3, o3-mini).
* User can override pricing via the ``pricing_table`` argument.
* Fuzzy model-name matching: dated model IDs (``gpt-4o-2024-08-06``,
  ``claude-3-5-sonnet-20241022``) resolve to base-model pricing.
* Unknown models return ``None`` cost gracefully (no error).
"""

from __future__ import annotations

import os
import json
import logging
from pathlib import Path

import pytest

from layerlens.instrument.adapters.providers.pricing import (
    PRICING,
    AZURE_PRICING,
    BEDROCK_PRICING,
    PRICING_OVERRIDE_ENV,
    CostRecord,
    PricingTable,
    calculate_cost,
    price_cost_record,
    priced_token_count,
    usage_from_payload,
    reset_pricing_cache,
    unpriced_token_count,
)
from layerlens.instrument.adapters.providers.token_usage import NormalizedTokenUsage


def _usage(prompt: int = 100, completion: int = 50, cached: int | None = None) -> NormalizedTokenUsage:
    return NormalizedTokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cached_tokens=cached,
    )


#: The logger the pricing module reports an unusable override row on.
_PRICING_LOGGER = "layerlens.instrument.adapters.providers.pricing"

#: Values a pricing row's ``input``, ``output`` or ``cached`` price must not hold.
#: As an ``input`` or ``output`` price each used to reach the cost formula, which
#: raised TypeError on the first two and returned a negative, nonsensical or
#: non-finite cost for the rest; as a ``cached`` price it was ignored.
_INVALID_PRICES = [
    pytest.param("0.001", id="string"),
    pytest.param(None, id="null"),
    pytest.param(-0.001, id="negative"),
    pytest.param(True, id="boolean"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="infinity"),
]


class TestDefaultCoverage:
    @pytest.mark.parametrize(
        "model",
        [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-4",
            "gpt-3.5-turbo",
            "o1",
            "o1-mini",
            "o3",
            "o3-mini",
        ],
    )
    def test_default_pricing_covers_current_openai_models(self, model: str) -> None:
        # LAY-3330 AC: Default pricing covers all current OpenAI models.
        cost = calculate_cost(model, _usage())
        assert cost is not None, f"{model} missing from default PRICING table"
        assert cost > 0


class TestFuzzyMatching:
    def test_openai_dated_iso_suffix_resolves_to_base(self) -> None:
        # gpt-4o-2024-08-06 should match gpt-4o's pricing.
        dated = calculate_cost("gpt-4o-2024-08-06", _usage())
        base = calculate_cost("gpt-4o", _usage())
        assert dated is not None
        assert dated == base

    def test_openai_mini_dated_resolves_to_base(self) -> None:
        dated = calculate_cost("gpt-4o-mini-2024-07-18", _usage())
        base = calculate_cost("gpt-4o-mini", _usage())
        assert dated is not None
        assert dated == base

    def test_anthropic_short_date_suffix_resolves_to_base(self) -> None:
        # claude-3-5-sonnet-20241022 hits the exact entry; ensure another
        # short-date variant (e.g. an unknown dated build) also resolves.
        cost = calculate_cost("claude-3-5-sonnet-20990101", _usage())
        base = calculate_cost("claude-3-5-sonnet", _usage())
        assert cost is not None
        assert cost == base

    def test_longest_prefix_disambiguates_gpt_4o_from_gpt_4(self) -> None:
        # ``gpt-4o-2099-99-99-foo`` regex-strips to ``gpt-4o-2099-99-99-foo``
        # unchanged (the suffix isn't a valid date). The longest-prefix
        # fallback must pick ``gpt-4o`` over ``gpt-4``.
        cost = calculate_cost("gpt-4o-experimental-build", _usage())
        gpt_4o = calculate_cost("gpt-4o", _usage())
        gpt_4 = calculate_cost("gpt-4", _usage())
        assert cost is not None
        assert cost == gpt_4o
        assert cost != gpt_4


class TestBedrockNovaPricing:
    """LAY-3605: Nova (the live Bedrock Agents default) must be priced — including
    the region-prefixed inference-profile ids the real wire carries."""

    @pytest.mark.parametrize(
        "model",
        [
            "amazon.nova-micro-v1:0",
            "amazon.nova-lite-v1:0",
            "amazon.nova-pro-v1:0",
            "amazon.nova-premier-v1:0",
        ],
    )
    def test_nova_models_priced(self, model: str) -> None:
        cost = calculate_cost(model, _usage(), BEDROCK_PRICING)
        assert cost is not None, f"{model} missing from BEDROCK_PRICING"
        assert cost > 0

    def test_region_prefixed_nova_resolves_to_base(self) -> None:
        # Bedrock Agents requires an inference profile, so the live foundationModel
        # is region-prefixed (us./eu./apac./us-gov.) — it must resolve to the bare id.
        base = calculate_cost("amazon.nova-lite-v1:0", _usage(), BEDROCK_PRICING)
        assert base is not None
        for prefix in ("us.", "eu.", "apac.", "us-gov."):
            cost = calculate_cost(prefix + "amazon.nova-lite-v1:0", _usage(), BEDROCK_PRICING)
            assert cost == base, f"{prefix!r} prefix did not resolve to the base rate"

    def test_inference_profile_arn_resolves(self) -> None:
        arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0"
        cost = calculate_cost(arn, _usage(), BEDROCK_PRICING)
        base = calculate_cost("amazon.nova-pro-v1:0", _usage(), BEDROCK_PRICING)
        assert cost is not None and cost == base

    def test_region_prefix_normalization_also_fixes_bedrock_claude(self) -> None:
        # The same normalization resolves the inference-profile Claude ids the
        # bedrock PROVIDER adapter sees.
        cost = calculate_cost("us.anthropic.claude-3-haiku-20240307-v1:0", _usage(), BEDROCK_PRICING)
        base = calculate_cost("anthropic.claude-3-haiku-20240307-v1:0", _usage(), BEDROCK_PRICING)
        assert cost is not None and cost == base


class TestBedrockMistralAI21Pricing:
    """LAY-3452: the Bedrock provider parses the Mistral and AI21 families but
    they had no rates in BEDROCK_PRICING, so cost resolved to None. These assert
    real, positive, non-None costs — they FAIL if the rate entries are removed."""

    @pytest.mark.parametrize(
        "model",
        [
            "mistral.mistral-7b-instruct-v0:2",
            "mistral.mixtral-8x7b-instruct-v0:1",
            "mistral.mistral-large-2402-v1:0",
            "mistral.mistral-large-2407-v1:0",
            "mistral.mistral-small-2402-v1:0",
            "ai21.jamba-1-5-large-v1:0",
            "ai21.jamba-1-5-mini-v1:0",
        ],
    )
    def test_mistral_and_ai21_models_priced(self, model: str) -> None:
        cost = calculate_cost(model, _usage(), BEDROCK_PRICING)
        assert cost is not None, f"{model} missing from BEDROCK_PRICING"
        assert cost > 0

    def test_mistral_large_exact_cost(self) -> None:
        # mistral.mistral-large-2402-v1:0 = $0.004/1k input, $0.012/1k output.
        cost = calculate_cost(
            "mistral.mistral-large-2402-v1:0",
            _usage(prompt=1000, completion=1000),
            BEDROCK_PRICING,
        )
        assert cost is not None and cost > 0
        # 1000 * 0.004/1000 + 1000 * 0.012/1000 = 0.004 + 0.012 = 0.016
        assert cost == pytest.approx(0.016)

    def test_ai21_jamba_mini_exact_cost(self) -> None:
        # ai21.jamba-1-5-mini-v1:0 = $0.0002/1k input, $0.0004/1k output.
        cost = calculate_cost(
            "ai21.jamba-1-5-mini-v1:0",
            _usage(prompt=1000, completion=1000),
            BEDROCK_PRICING,
        )
        assert cost is not None and cost > 0
        # 1000 * 0.0002/1000 + 1000 * 0.0004/1000 = 0.0002 + 0.0004 = 0.0006
        assert cost == pytest.approx(0.0006)


class TestUnknownModelsGracefully:
    def test_completely_unknown_model_returns_none(self) -> None:
        assert calculate_cost("totally-fake-model-9000", _usage()) is None

    def test_empty_model_returns_none(self) -> None:
        assert calculate_cost("", _usage()) is None


class TestUserOverrides:
    def test_caller_supplied_pricing_table_takes_precedence(self) -> None:
        # The caller can pass an entirely custom pricing table — no code changes
        # in the library needed (LAY-3327 + LAY-3330 ACs).
        custom = {"my-private-model": {"input": 1.0, "output": 2.0}}
        cost = calculate_cost(
            "my-private-model",
            _usage(prompt=1000, completion=500),
            pricing_table=custom,
        )
        # 1000 * 1.0/1000 + 500 * 2.0/1000 = 1.0 + 1.0 = 2.0
        assert cost == pytest.approx(2.0)

    def test_custom_table_isolates_from_defaults(self) -> None:
        # A custom table that doesn't include a model that exists in PRICING
        # must NOT silently fall through to PRICING — it's an explicit override.
        custom = {"my-private-model": {"input": 1.0, "output": 2.0}}
        assert calculate_cost("gpt-4o", _usage(), pricing_table=custom) is None


class TestCachedTokens:
    def test_cached_tokens_discounted(self) -> None:
        # gpt-4o's cached-input price is half its input price (the row's
        # ``cached`` price). Anthropic gets 90% off; Google 75% off.
        without = calculate_cost("gpt-4o", _usage(prompt=1000, completion=0))
        with_cache = calculate_cost("gpt-4o", _usage(prompt=1000, completion=0, cached=500))
        assert with_cache is not None and without is not None
        # Half of the 1000 prompt tokens are cached at 50% off, so cost drops.
        assert with_cache < without

    def test_anthropic_cached_tokens_steeper_discount(self) -> None:
        # 90% off for Claude; ensure the function applies the right discount.
        cost = calculate_cost(
            "claude-3-5-sonnet",
            _usage(prompt=1000, completion=0, cached=1000),
        )
        # All-cached: 1000 * 0.003/1000 * 0.10 = 0.0003
        assert cost == pytest.approx(0.0003)

    def test_google_cached_tokens_discount(self) -> None:
        # 75% off for Gemini (0.25 multiplier) — the _cached_token_discount
        # gemini branch (pricing.py) had no test (LAY-3572 / B3).
        # gemini-1.5-pro input rate = 0.00125; all-cached 1000 tokens:
        # 1000 * 0.00125/1000 * 0.25 = 0.0003125
        cost = calculate_cost("gemini-1.5-pro", _usage(prompt=1000, completion=0, cached=1000))
        assert cost == pytest.approx(0.0003125)
        # ...and distinctly cheaper than the 50%-off default would give.
        default_rate = calculate_cost("gpt-4o-mini", _usage(prompt=1000, completion=0, cached=1000))
        gemini_ratio = cost / calculate_cost("gemini-1.5-pro", _usage(prompt=1000, completion=0))
        assert gemini_ratio == pytest.approx(0.25), "gemini cached discount is not 75% off"
        assert default_rate is not None


class TestPricingTableIsPubliclyAccessible:
    def test_pricing_dict_is_importable(self) -> None:
        # The story explicitly asks for "dynamic pricing table" — the table
        # itself must be a public attribute callers can introspect.
        assert isinstance(PRICING, dict)
        assert "gpt-4o" in PRICING
        assert "claude-3-5-sonnet" in PRICING


class TestEnvOverride:
    """LAY-3327 AC: pricing 'can be updated without code changes'.

    Setting ``LAYERLENS_PRICING_TABLE`` to a JSON file path applies overrides
    that take precedence over both the bundled defaults and any caller-supplied
    ``pricing_table``.
    """

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch):
        # The override loader caches; reset before and after each test so the
        # tests are independent.
        monkeypatch.delenv(PRICING_OVERRIDE_ENV, raising=False)
        reset_pricing_cache()
        yield
        reset_pricing_cache()

    def test_env_override_changes_pricing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"gpt-4o": {"input": 0.999, "output": 0.999}}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        cost = calculate_cost("gpt-4o", _usage(prompt=1000, completion=1000))
        # 1000 * 0.999/1000 + 1000 * 0.999/1000 = 1.998
        assert cost == pytest.approx(1.998)

    def test_env_override_wins_over_caller_supplied_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"gpt-4o": {"input": 0.5, "output": 0.5}}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        caller_table = {"gpt-4o": {"input": 100.0, "output": 100.0}}
        cost = calculate_cost("gpt-4o", _usage(prompt=1000, completion=0), pricing_table=caller_table)
        # env value (0.5) used, not caller table (100.0).
        # 1000 * 0.5/1000 = 0.5
        assert cost == pytest.approx(0.5)

    def test_env_override_supports_fuzzy_matching(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Override of ``gpt-4o`` should also apply to dated variants via the
        # same fuzzy resolution.
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"gpt-4o": {"input": 0.001, "output": 0.001}}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        cost = calculate_cost("gpt-4o-2024-08-06", _usage(prompt=1000, completion=0))
        assert cost == pytest.approx(0.001)

    def test_missing_env_var_uses_defaults(self) -> None:
        # When LAYERLENS_PRICING_TABLE isn't set, defaults work normally.
        assert PRICING_OVERRIDE_ENV not in os.environ
        cost = calculate_cost("gpt-4o", _usage(prompt=1000, completion=0))
        assert cost is not None
        assert cost > 0

    def test_unreadable_override_file_falls_back_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pointing at a missing file must not crash the request path.
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(tmp_path / "does-not-exist.json"))
        reset_pricing_cache()
        cost = calculate_cost("gpt-4o", _usage())
        # Falls back to defaults.
        assert cost is not None

    def test_malformed_json_override_falls_back_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not valid json at all {{{")
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(bad))
        reset_pricing_cache()
        cost = calculate_cost("gpt-4o", _usage())
        assert cost is not None

    def test_env_override_adds_new_model_not_in_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An ops team can price a new model that ships before a library release.
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"my-internal-llm-v2": {"input": 0.01, "output": 0.02}}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        cost = calculate_cost("my-internal-llm-v2", _usage(prompt=1000, completion=500))
        # 1000 * 0.01/1000 + 500 * 0.02/1000 = 0.01 + 0.01 = 0.02
        assert cost == pytest.approx(0.02)

    def test_env_override_cached_price_prices_cache_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The optional ``cached`` price is what a cache read costs, per 1K tokens.
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"my-internal-llm-v2": {"input": 0.01, "output": 0.02, "cached": 0.001}}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        cost = calculate_cost("my-internal-llm-v2", _usage(prompt=1000, completion=0, cached=800))
        # 200 uncached * 0.01/1000 + 800 cached * 0.001/1000 = 0.002 + 0.0008
        assert cost == pytest.approx(0.0028)

    @pytest.mark.parametrize("key", ["input", "output", "cached"])
    @pytest.mark.parametrize("value", _INVALID_PRICES)
    def test_env_override_row_with_an_invalid_price_is_ignored_with_a_warning(
        self,
        key: str,
        value: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Such a row used to raise TypeError while pricing every cost.record of
        # the model or produce a negative, NaN or infinite cost (see
        # _INVALID_PRICES). It is now skipped with a warning naming the row and
        # the key, as an unreadable override file is.
        row = {"input": 0.01, "output": 0.02, "cached": 0.001}
        row[key] = value
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"my-internal-llm-v2": row}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        with caplog.at_level(logging.WARNING, logger=_PRICING_LOGGER):
            cost = calculate_cost("my-internal-llm-v2", _usage(prompt=1000, completion=500, cached=200))

        assert cost is None, f"a row with {key}={value!r} priced the call"
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("'my-internal-llm-v2'" in w and repr(key) in w for w in warnings), warnings

    def test_env_override_cached_price_above_the_input_price_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A cache read never costs more than an uncached input token. A larger
        # ``cached`` price is a mistake, such as the price list's cache-write
        # column (1.25 times the input price) copied into ``cached``.
        override_path = tmp_path / "pricing.json"
        override_path.write_text(
            json.dumps({"my-internal-llm-v2": {"input": 0.002, "output": 0.012, "cached": 0.0025}})
        )
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        with caplog.at_level(logging.WARNING, logger=_PRICING_LOGGER):
            cost = calculate_cost("my-internal-llm-v2", _usage(prompt=1000, completion=0, cached=1000))

        assert cost is None
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("'my-internal-llm-v2'" in w and "'cached'" in w for w in warnings), warnings

    def test_an_entry_that_is_not_an_object_is_not_a_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only objects are price rows; any other entry must neither price nor
        # break the lookup, and the file's real rows still load.
        override_path = tmp_path / "pricing.json"
        override_path.write_text(
            json.dumps({"_source": "list prices", "my-internal-llm-v2": {"input": 0.01, "output": 0.02}})
        )
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        assert calculate_cost("_source", _usage()) is None
        assert calculate_cost("my-internal-llm-v2", _usage(prompt=1000, completion=0)) == pytest.approx(0.01)

    def test_an_invalid_override_row_leaves_the_bundled_row_in_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override_path = tmp_path / "pricing.json"
        override_path.write_text(json.dumps({"gpt-4.1": {"input": 0.002, "output": 0.008, "cached": "0.0005"}}))
        monkeypatch.setenv(PRICING_OVERRIDE_ENV, str(override_path))
        reset_pricing_cache()

        cost = calculate_cost("gpt-4.1", _usage(prompt=1000, completion=0, cached=1000))
        # 1,000 cache reads at the bundled gpt-4.1 cached-input price, $0.50 per 1M.
        assert cost == pytest.approx(1000 * 0.50 / 1_000_000)


class TestPricingTableClass:
    """LAY-3330 Claude Code Prompt requires a ``PricingTable`` class with:

    * default rates covering current OpenAI models
    * caller-provided overrides via constructor / from_dict / from_json_file
    * calculate_cost(model, input_tokens, output_tokens) -> CostRecord
    * fuzzy model matching for dated variants
    """

    def test_default_constructor_covers_openai_models(self):
        table = PricingTable()
        for m in ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1", "o1-mini", "o3", "o3-mini"]:
            assert table.has_model(m), f"PricingTable() missing default: {m}"

    def test_calculate_cost_returns_cost_record(self):
        table = PricingTable()
        record = table.calculate_cost("gpt-4o", input_tokens=1000, output_tokens=500)
        assert isinstance(record, CostRecord)
        assert record.model == "gpt-4o"
        assert record.input_tokens == 1000
        assert record.output_tokens == 500
        # gpt-4o: $0.0025/1k input + $0.01/1k output = $0.0025 + $0.005 = $0.0075
        assert record.cost_usd == pytest.approx(0.0075)

    def test_unknown_model_returns_record_with_none_cost(self):
        table = PricingTable()
        record = table.calculate_cost("totally-fake-model", input_tokens=100, output_tokens=50)
        assert record.cost_usd is None
        # And the input/output token counts are still surfaced so the caller
        # can decide how to log the unknown-model event.
        assert record.input_tokens == 100
        assert record.output_tokens == 50

    def test_fuzzy_match_on_class_method(self):
        table = PricingTable()
        dated = table.calculate_cost("gpt-4o-2024-08-06", input_tokens=1000, output_tokens=0)
        base = table.calculate_cost("gpt-4o", input_tokens=1000, output_tokens=0)
        assert dated.cost_usd == base.cost_usd

    def test_from_dict_overrides_defaults_entirely(self):
        custom = {"my-private-model": {"input": 1.0, "output": 2.0}}
        table = PricingTable.from_dict(custom)
        # Bundled defaults are NOT present in a from_dict table.
        assert table.has_model("my-private-model")
        assert not table.has_model("gpt-4o")

    def test_from_json_file(self, tmp_path):
        path = tmp_path / "rates.json"
        path.write_text(json.dumps({"team-llm": {"input": 0.005, "output": 0.01}}))
        table = PricingTable.from_json_file(str(path))
        record = table.calculate_cost("team-llm", input_tokens=2000, output_tokens=1000)
        # 2000 * 0.005/1000 + 1000 * 0.01/1000 = 0.01 + 0.01 = 0.02
        assert record.cost_usd == pytest.approx(0.02)

    def test_from_json_file_rejects_non_object_root(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError, match="must be an object"):
            PricingTable.from_json_file(str(path))

    def test_models_lists_keys(self):
        table = PricingTable.from_dict({"a": {"input": 0.1, "output": 0.2}, "b": {"input": 0.3, "output": 0.4}})
        models = table.models()
        assert sorted(models) == ["a", "b"]

    def test_cached_tokens_propagated_to_record(self):
        table = PricingTable()
        record = table.calculate_cost("gpt-4o", input_tokens=1000, output_tokens=0, cached_tokens=200)
        assert record.cached_tokens == 200
        # 800 non-cached at $0.0025/1k + 200 cached at 50% off = 200 * 0.00125/1k
        expected = (800 * 0.0025 / 1000) + (200 * (0.0025 * 0.5) / 1000)
        assert record.cost_usd == pytest.approx(expected)

    def test_a_rows_cached_price_prices_cache_reads(self):
        table = PricingTable.from_dict({"team-llm": {"input": 0.005, "output": 0.01, "cached": 0.0005}})
        record = table.calculate_cost("team-llm", input_tokens=2000, output_tokens=0, cached_tokens=1000)
        # 1000 uncached * 0.005/1000 + 1000 cached * 0.0005/1000 = 0.005 + 0.0005
        assert record.cost_usd == pytest.approx(0.0055)

    def test_a_zero_cached_price_makes_cache_reads_free(self):
        # 0.0 is a price, not a missing one: the provider fallback must not replace it.
        table = PricingTable.from_dict({"team-llm": {"input": 0.005, "output": 0.01, "cached": 0.0}})
        record = table.calculate_cost("team-llm", input_tokens=1000, output_tokens=0, cached_tokens=1000)
        assert record.cost_usd == 0.0

    def test_the_cached_price_applies_with_env_overrides_bypassed(self):
        table = PricingTable(
            {"team-llm": {"input": 0.005, "output": 0.01, "cached": 0.0005}}, respect_env_overrides=False
        )
        record = table.calculate_cost("team-llm", input_tokens=1000, output_tokens=0, cached_tokens=1000)
        # 1000 cached * 0.0005/1000
        assert record.cost_usd == pytest.approx(0.0005)

    def test_from_json_file_reads_the_cached_price(self, tmp_path):
        path = tmp_path / "rates.json"
        path.write_text(json.dumps({"team-llm": {"input": 0.005, "output": 0.01, "cached": 0.0005}}))
        table = PricingTable.from_json_file(str(path))
        record = table.calculate_cost("team-llm", input_tokens=1000, output_tokens=0, cached_tokens=1000)
        # 1000 cached * 0.0005/1000
        assert record.cost_usd == pytest.approx(0.0005)

    @pytest.mark.parametrize("key", ["input", "output", "cached"])
    @pytest.mark.parametrize("value", _INVALID_PRICES)
    def test_a_row_with_an_invalid_price_is_rejected(self, key, value):
        row = {"input": 0.005, "output": 0.01, "cached": 0.0005}
        row[key] = value
        with pytest.raises(ValueError, match=f"'team-llm'.*'{key}'"):
            PricingTable.from_dict({"team-llm": row})

    def test_from_json_file_rejects_an_invalid_price(self, tmp_path):
        path = tmp_path / "rates.json"
        path.write_text(json.dumps({"team-llm": {"input": 0.005, "output": 0.01, "cached": -0.0005}}))
        with pytest.raises(ValueError, match="'team-llm'.*'cached'"):
            PricingTable.from_json_file(str(path))

    def test_a_cached_price_above_the_input_price_is_rejected(self):
        # The price list's cache-write column (1.25 times input) in ``cached``.
        with pytest.raises(ValueError, match="'team-llm'.*'cached'"):
            PricingTable.from_dict({"team-llm": {"input": 0.002, "output": 0.012, "cached": 0.0025}})


# ---------------------------------------------------------------------------
# Service-tier pricing (A2 / LAY-3626) — OpenAI flex/batch (~0.5x) and priority
# (~2x) change the per-token price. The response echoes service_tier; pricing
# was model-keyed only, so flex was over-billed ~2x and priority UNDER-billed
# (the dangerous direction — budgets/ceilings read cost_usd). Web-grounded:
# OpenAI flex = batch rates (~50% off); priority ~2-2.5x standard.
# ---------------------------------------------------------------------------


@pytest.mark.invariant
class TestServiceTierPricing:
    def test_tier_orders_flex_lt_standard_lt_priority(self) -> None:
        u = _usage(prompt=1000, completion=1000)
        flex = calculate_cost("gpt-4o", u, service_tier="flex")
        std = calculate_cost("gpt-4o", u, service_tier="standard")
        prio = calculate_cost("gpt-4o", u, service_tier="priority")
        assert flex is not None and std is not None and prio is not None
        assert flex < std < prio, f"service_tier not priced: flex={flex} std={std} prio={prio}"

    def test_no_tier_equals_standard_backcompat(self) -> None:
        u = _usage(prompt=1000, completion=1000)
        assert calculate_cost("gpt-4o", u) == calculate_cost("gpt-4o", u, service_tier="standard")

    def test_flex_is_half_standard(self) -> None:
        u = _usage(prompt=1000, completion=1000)
        assert calculate_cost("gpt-4o", u, service_tier="flex") == pytest.approx(calculate_cost("gpt-4o", u) * 0.5)

    def test_priority_is_double_standard(self) -> None:
        u = _usage(prompt=1000, completion=1000)
        assert calculate_cost("gpt-4o", u, service_tier="priority") == pytest.approx(calculate_cost("gpt-4o", u) * 2.0)


# ---------------------------------------------------------------------------
# Cache-WRITE pricing (A3 / LAY-3626) — Anthropic cache_creation tokens cost
# 1.25x base input (5-min TTL); the formula priced only non-cached + cache-READ
# (0.1x) + completion, so every cache write was billed at $0 (systematic
# under-bill on the canonical long-running-agent prompt-cache pattern).
# ---------------------------------------------------------------------------


@pytest.mark.invariant
class TestCacheCreationPricing:
    def test_cache_write_tokens_are_priced(self) -> None:
        base = NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100)
        with_write = NormalizedTokenUsage(
            prompt_tokens=1000, completion_tokens=100, total_tokens=1100, cache_creation_tokens=1000
        )
        c_base = calculate_cost("claude-3-5-sonnet", base)
        c_write = calculate_cost("claude-3-5-sonnet", with_write)
        assert c_write > c_base, "cache_creation (write) tokens billed at $0"
        input_rate = 0.003  # claude-3-5-sonnet input $/1k
        expected_delta = 1000 * input_rate * 1.25 / 1000
        assert c_write - c_base == pytest.approx(expected_delta), "cache write not priced at input_rate * 1.25"

    def test_cache_write_costs_more_than_cache_read_for_same_tokens(self) -> None:
        read = NormalizedTokenUsage(prompt_tokens=2000, completion_tokens=100, total_tokens=2100, cached_tokens=1000)
        write = NormalizedTokenUsage(
            prompt_tokens=1000, completion_tokens=100, total_tokens=1100, cache_creation_tokens=1000
        )
        # both have 1000 full-rate input; read adds 1000 @0.1x, write adds 1000 @1.25x
        assert calculate_cost("claude-3-5-sonnet", write) > calculate_cost("claude-3-5-sonnet", read)


# ---------------------------------------------------------------------------
# Cache-write rate by provider (LAY-4259). A premium applies only where the
# provider documents one: Anthropic's 5-minute write (1.25x input) and OpenAI
# GPT-5.6 and later (1.25x input). OpenAI's earlier models have "No additional
# cache-write charge" (https://developers.openai.com/api/docs/guides/prompt-caching).
# ---------------------------------------------------------------------------

#: One flat rate for every model below, so each expectation differs only by the
#: write multiplier.
_FLAT_RATE = {"input": 0.001, "output": 0.002}


def _writes_only(tokens: int = 1000) -> NormalizedTokenUsage:
    return NormalizedTokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=tokens, cache_creation_tokens=tokens)


@pytest.mark.invariant
class TestCacheWriteRateByProvider:
    @pytest.mark.parametrize(
        "model, input_per_m, published_write_per_m",
        [
            ("gpt-6-astra", 10.00, 12.50),
            ("gpt-6.1-sol", 2.00, 2.50),
            ("gpt-6-luna", 0.10, 0.125),
            ("gpt-6-sol", 2.00, 2.50),
            ("gpt-5.6-sol", 4.00, 5.00),
            ("gpt-5.6-terra", 2.00, 2.50),
            ("gpt-5.6-luna", 0.20, 0.25),
        ],
    )
    def test_gpt_5_6_and_later_writes_cost_the_published_cache_write_price(
        self, model: str, input_per_m: float, published_write_per_m: float
    ) -> None:
        # The "Short context input" and "Short context cache writes" columns of
        # https://developers.openai.com/api/docs/pricing (standard tier): one
        # million written tokens cost exactly the published write price.
        table = {model: {"input": input_per_m / 1000, "output": 0.0}}
        assert calculate_cost(model, _writes_only(1_000_000), table) == pytest.approx(published_write_per_m)

    @pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol-2026-07-09", "gpt-6.1-sol-2026-09-29"])
    def test_the_gpt_5_6_alias_and_dated_ids_carry_the_premium(self, model: str) -> None:
        assert calculate_cost(model, _writes_only(), {model: _FLAT_RATE}) == pytest.approx(1000 * 0.001 * 1.25 / 1000)

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.4-mini",
            "gpt-5",
            "gpt-5-nano",
            "gpt-4.1-nano",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-3.5-turbo",
            "gpt-35-turbo",  # Azure's name for GPT-3.5: not "generation 35"
            "o1",
            "o3",
            "o4-mini",
            "gpt-rosalind-research",  # pricing page: "Cache-write pricing does not apply"
        ],
    )
    def test_openai_models_before_gpt_5_6_bill_writes_as_plain_input(self, model: str) -> None:
        assert calculate_cost(model, _writes_only(), {model: _FLAT_RATE}) == pytest.approx(1000 * 0.001 / 1000)

    @pytest.mark.parametrize(
        "model",
        [
            "claude-3-5-sonnet",
            "claude-sonnet-4-5-20250929",
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "anthropic/claude-sonnet-4",
        ],
    )
    def test_anthropic_writes_keep_the_5_minute_write_premium(self, model: str) -> None:
        assert calculate_cost(model, _writes_only(), {model: _FLAT_RATE}) == pytest.approx(1000 * 0.001 * 1.25 / 1000)

    def test_a_model_with_no_documented_premium_bills_writes_as_plain_input(self) -> None:
        # A custom model an ops team priced through a table: nothing documents a
        # write premium for it, so charging one would be an analogy, not a bill.
        assert calculate_cost(
            "my-internal-llm-v2", _writes_only(), {"my-internal-llm-v2": _FLAT_RATE}
        ) == pytest.approx(1000 * 0.001 / 1000)


@pytest.mark.invariant
class TestOpenAICacheWritesAreInsidePromptTokens:
    """OpenAI counts ``cache_write_tokens`` INSIDE ``prompt_tokens`` (as it does
    ``cached_tokens``); Anthropic reports ``cache_creation_input_tokens`` beside
    them. ``usage_from_payload`` is the one place a payload's keys become a
    :class:`NormalizedTokenUsage`, and it keeps the cache writes outside
    ``prompt_tokens`` for both, so the formula rates each token once."""

    #: Chat Completions usage: 1,500 input tokens, 1,024 of them written to the
    #: cache and 476 ordinary, plus 12 output tokens.
    OPENAI_WRITE = {
        "prompt_tokens": 1500,
        "completion_tokens": 12,
        "total_tokens": 1512,
        "cached_tokens": 0,
        "cache_write_tokens": 1024,
    }

    def test_openai_writes_move_out_of_prompt_tokens(self) -> None:
        usage = usage_from_payload(dict(self.OPENAI_WRITE))
        assert usage.cache_creation_tokens == 1024
        assert usage.prompt_tokens == 476
        assert usage.cached_tokens == 0
        assert usage.completion_tokens == 12
        assert usage.total_tokens == 1512

    def test_anthropic_creation_stays_beside_prompt_tokens(self) -> None:
        # anthropic.py reports prompt_tokens = input_tokens + cache reads; the
        # cache writes were never part of it.
        usage = usage_from_payload(
            {
                "prompt_tokens": 1050,
                "input_tokens": 50,
                "cached_tokens": 1000,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 2000,
                "completion_tokens": 100,
                "output_tokens": 100,
            }
        )
        assert usage.prompt_tokens == 1050
        assert usage.cache_creation_tokens == 2000

    def test_each_reported_token_is_rated_exactly_once(self) -> None:
        # 476 ordinary + 1,024 written + 12 output = the 1,512 OpenAI reported.
        assert priced_token_count(usage_from_payload(dict(self.OPENAI_WRITE))) == 1512
        assert unpriced_token_count(dict(self.OPENAI_WRITE)) == 0

    def test_an_earlier_model_bills_its_writes_at_the_plain_input_rate(self) -> None:
        # gpt-4o-mini from the bundled table ($0.15 input, $0.60 output per 1M):
        # all 1,500 input tokens at the input rate, the writes neither repeated
        # nor marked up.
        cost = price_cost_record({"provider": "openai", "model": "gpt-4o-mini", **self.OPENAI_WRITE})
        assert cost == pytest.approx(1500 * 0.15 / 1_000_000 + 12 * 0.60 / 1_000_000)

    def test_a_payload_that_reports_only_cache_writes_is_priced(self) -> None:
        # Written tokens are billed input: their key alone is reported usage.
        cost = price_cost_record({"provider": "openai", "model": "gpt-4o", "cache_write_tokens": 1000})
        assert cost == pytest.approx(1000 * 2.50 / 1_000_000)


class TestUnpriceableTokenShapes:
    """A shape the formula cannot price must return ``None``, never ``0.0``.

    ``_cost_from_rates`` prices prompt / cached / cache_creation / completion and
    NEVER reads ``total_tokens``. A payload that carries only a total therefore has
    no priceable dimension at all, and arithmetic over four zeros yields ``0.0`` — a
    *computed-looking* zero for a call the provider really billed. ``0.0 is not
    None``, so every downstream "did we get a price?" guard passes and the zero
    ships as if it were derived. ``None`` is the honest answer: we know the tokens,
    we cannot price them.

    Bite proof: revert the unpriceable-shape check in ``calculate_cost`` and every
    test in this class fails with ``0.0``.
    """

    def test_totals_only_is_unpriceable_not_free(self) -> None:
        usage = NormalizedTokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=1500)
        cost = calculate_cost("gpt-4o", usage)
        assert cost != 0.0, "a real 1500-token gpt-4o call priced as $0.00 — a fabricated cost"
        assert cost is None, f"totals-only must be unpriceable (None), got {cost!r}"

    def test_an_all_zero_usage_is_still_an_honest_zero(self) -> None:
        # BOUNDARY (the fix must not over-reach): a call that reports a zero total
        # AND zero on every dimension genuinely billed nothing. 0.0 is arithmetic
        # here, not fabrication — distinguishable from the totals-only case by the
        # absence of any positive count.
        #
        # Note this is ALSO what a bare ``NormalizedTokenUsage()`` is: every field
        # defaults to 0, so the formula cannot distinguish "the provider reported
        # zero usage" from "no usage was reported". That distinction is only
        # available one layer up, where key PRESENCE is visible — see
        # ``test_a_payload_with_no_token_keys_is_unpriceable``.
        usage = NormalizedTokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        assert calculate_cost("gpt-4o", usage) == 0.0
        assert calculate_cost("gpt-4o", NormalizedTokenUsage()) == 0.0

    def test_a_payload_with_no_token_keys_is_unpriceable(self) -> None:
        # The payload layer CAN tell absent from zero, and must: a cost.record that
        # reached pricing carrying no usage whatsoever is an upstream bug, and
        # answering 0.0 hides it behind a real-looking number. (_event_schema also
        # rejects a tokenless cost.record outright; this closes the pricing side.)
        from layerlens.instrument.adapters.providers.pricing import price_cost_record

        assert price_cost_record({"provider": "openai", "model": "gpt-4o"}) is None
        # ...while an EXPLICIT zero is a real report of no usage, and prices as 0.0.
        assert price_cost_record({"provider": "openai", "model": "gpt-4o", "total_tokens": 0}) == 0.0

    @pytest.mark.parametrize(
        "usage",
        [
            NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=0, total_tokens=0),
            NormalizedTokenUsage(prompt_tokens=0, completion_tokens=500, total_tokens=0),
            NormalizedTokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=1500, cached_tokens=1500),
            NormalizedTokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=1500, cache_creation_tokens=1500),
        ],
        ids=["prompt-only", "completion-only", "cached-only", "cache-write-only"],
    )
    def test_any_priceable_dimension_still_prices(self, usage: NormalizedTokenUsage) -> None:
        # The check keys off "no priceable dimension at all", NOT off a missing
        # split: a prompt-only embedding usage and a fully-cached turn are both
        # legitimately one-sided and must keep pricing.
        cost = calculate_cost("gpt-4o", usage)
        assert cost is not None and cost > 0.0

    def test_price_cost_record_propagates_the_unpriceable_verdict(self) -> None:
        from layerlens.instrument.adapters.providers.pricing import price_cost_record

        assert price_cost_record({"provider": "openai", "model": "gpt-4o", "total_tokens": 1500}) is None
        # the flat framework vocabulary reaches the same chokepoint
        assert price_cost_record({"provider": "openai", "model": "gpt-4o", "tokens_total": 1500}) is None
        # and a priceable shape is unaffected
        assert price_cost_record(
            {"provider": "openai", "model": "gpt-4o", "prompt_tokens": 1000, "completion_tokens": 500}
        ) == pytest.approx(0.0075)


@pytest.mark.invariant
class TestPricedTokenAccounting:
    """``priced_token_count`` must mirror the formula's four legs EXACTLY.

    It is the basis of the F4 under-report detector, so if it drifts from
    ``_cost_from_rates`` the detector either cries wolf or goes blind — both worse
    than not detecting at all.
    """

    def test_it_counts_each_leg_exactly_once(self) -> None:
        from layerlens.instrument.adapters.providers.pricing import priced_token_count

        # Distinct values so a double-count or an omission cannot cancel out.
        usage = NormalizedTokenUsage(
            prompt_tokens=100,
            completion_tokens=7,
            total_tokens=137,
            cached_tokens=30,
            cache_creation_tokens=1000,
        )
        # non_cached(70) + cached(30) + cache_creation(1000) + completion(7)
        assert priced_token_count(usage) == 1107

    def test_a_fully_cached_turn_counts_every_token_as_priced(self) -> None:
        from layerlens.instrument.adapters.providers.pricing import priced_token_count

        # prompt_tokens=0 with cached_tokens=1500 is a REAL reported shape (the
        # `[cached-only]` case above). The cached leg prices all 1500, so a detector
        # that computed `prompt + completion + cache_creation` would report a 100%
        # unpriced gap on a fully-priced turn.
        usage = NormalizedTokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=1500, cached_tokens=1500)
        assert priced_token_count(usage) == 1500

    def test_it_agrees_with_the_real_formula_about_what_was_rated(self) -> None:
        from layerlens.instrument.adapters.providers.pricing import _cost_from_rates, priced_token_count

        # Anti-drift: at a flat $1/1k on every leg and no cache discount, the cost in
        # dollars IS the priced token count / 1000. If a leg is ever added to the
        # formula without being added to the counter, this diverges.
        rates = {"input": 1.0, "output": 1.0}
        usage = NormalizedTokenUsage(prompt_tokens=100, completion_tokens=7, total_tokens=107)
        cost = _cost_from_rates(rates, "flat-rate-test-model", usage)
        assert cost is not None
        assert round(cost * 1000, 6) == float(priced_token_count(usage))

    def test_no_total_reported_means_no_gap_detected(self) -> None:
        from layerlens.instrument.adapters.providers.pricing import unpriced_token_count

        # HONEST LIMIT, asserted rather than left in prose: Anthropic reports no
        # total, so NormalizedTokenUsage derives it as prompt+completion and a token
        # billed outside output_tokens is invisible to this detector by construction.
        assert unpriced_token_count({"prompt_tokens": 10, "completion_tokens": 5}) == 0

    def test_it_detects_the_gemini_thinking_gap(self) -> None:
        from layerlens.instrument.adapters.providers.pricing import unpriced_token_count

        assert unpriced_token_count({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 57}) == 42


@pytest.mark.invariant
class TestReasoningTokensAreNotPricedAdditively:
    """Reasoning/thinking tokens must NEVER be added on top of ``completion_tokens``.

    LAY-3622 F4 re-judged the audit's own suggestion here. ``NormalizedTokenUsage``
    carries ``reasoning_tokens``/``thinking_tokens`` and the formula reads neither —
    which looks like an under-bill until you check where the numbers come from:

    * OpenAI takes ``reasoning_tokens`` from ``completion_tokens_details``, a
      BREAKDOWN of ``completion_tokens`` that the output leg already prices.
    * Anthropic's ``thinking_tokens`` is ``len(thinking_text) // 4``, an estimate,
      and its ``completion_tokens`` is ``output_tokens``, which already includes the
      thinking.

    So summing them would double-bill one provider and bill the other from a
    character count. These tests exist so that "fix" cannot land by accident.
    """

    def test_reasoning_tokens_do_not_change_the_price(self) -> None:
        base = NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        with_reasoning = NormalizedTokenUsage(
            prompt_tokens=1000, completion_tokens=500, total_tokens=1500, reasoning_tokens=400
        )
        assert calculate_cost("gpt-4o", with_reasoning) == calculate_cost("gpt-4o", base)

    def test_thinking_tokens_do_not_change_the_price(self) -> None:
        base = NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        with_thinking = NormalizedTokenUsage(
            prompt_tokens=1000, completion_tokens=500, total_tokens=1500, thinking_tokens=400
        )
        assert calculate_cost("claude-3-5-sonnet", with_thinking) == calculate_cost("claude-3-5-sonnet", base)

    def test_the_price_is_still_a_real_positive_number(self) -> None:
        # VACUITY CONTROL: the two tests above would also pass if calculate_cost
        # returned None for both sides.
        usage = NormalizedTokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500, reasoning_tokens=400)
        cost = calculate_cost("gpt-4o", usage)
        assert cost is not None and cost > 0


# ---------------------------------------------------------------------------
# OpenAI list prices. Every OpenAI row of the bundled table is pinned, leg by leg,
# to https://developers.openai.com/api/docs/pricing as checked on 2026-10-16
# (Standard tier, short context: up to 272K input tokens). A cache read used to
# cost half the input price on every OpenAI model; the list charges a quarter on
# gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, o3 and o4-mini, a tenth on GPT-5 and later,
# a twentieth on gpt-6.1-sol, and the full input price on the Pro models.
# ---------------------------------------------------------------------------

#: (model, input, cached input, output) in USD per 1M tokens, as the list shows
#: them. ``None`` is a "-" in the cached-input column.
_OPENAI_LIST_PRICES = [
    ("gpt-6-astra", 10.00, 1.00, 50.00),
    ("gpt-6.1-sol", 2.00, 0.10, 10.00),
    ("gpt-6-luna", 0.10, 0.01, 0.50),
    ("gpt-6-sol", 2.00, 0.20, 10.00),
    ("gpt-5.6-sol", 4.00, 0.40, 20.00),
    ("gpt-5.6-terra", 2.00, 0.20, 12.00),
    ("gpt-5.6-luna", 0.20, 0.02, 1.20),
    ("gpt-5.6-cyber", 12.50, 1.25, 75.00),
    ("gpt-5.5", 5.00, 0.50, 30.00),
    ("gpt-5.5-pro", 30.00, None, 180.00),
    ("gpt-5.5-cyber", 12.50, 1.25, 75.00),
    ("gpt-5.4", 2.50, 0.25, 15.00),
    ("gpt-5.4-mini", 0.75, 0.075, 4.50),
    ("gpt-5.4-nano", 0.20, 0.02, 1.25),
    ("gpt-5.4-pro", 30.00, None, 180.00),
    ("gpt-5.3-codex", 1.75, 0.175, 14.00),
    ("gpt-5.2", 1.75, 0.175, 14.00),
    ("gpt-5.2-pro", 21.00, None, 168.00),
    ("gpt-5.1", 1.25, 0.125, 10.00),
    ("gpt-5", 1.25, 0.125, 10.00),
    ("gpt-5-mini", 0.25, 0.025, 2.00),
    ("gpt-5-nano", 0.05, 0.005, 0.40),
    ("gpt-5-pro", 15.00, None, 120.00),
    ("gpt-5-search-api", 1.25, 0.125, 10.00),
    ("chat-latest", 5.00, 0.50, 30.00),
    ("gpt-rosalind-research", 5.00, 0.50, 25.00),
    ("gpt-rosalind-discovery", 5.00, 0.50, 25.00),
    ("gpt-4.1", 2.00, 0.50, 8.00),
    ("gpt-4.1-mini", 0.40, 0.10, 1.60),
    ("gpt-4.1-nano", 0.10, 0.025, 0.40),
    ("gpt-4o", 2.50, 1.25, 10.00),
    # A gpt-4o snapshot (https://developers.openai.com/api/docs/models/gpt-4o),
    # billed on the list's gpt-4o row.
    ("gpt-4o-2024-11-20", 2.50, 1.25, 10.00),
    ("gpt-4o-2024-05-13", 5.00, None, 15.00),
    ("gpt-4o-mini", 0.15, 0.075, 0.60),
    ("o1", 15.00, 7.50, 60.00),
    ("o1-pro", 150.00, None, 600.00),
    ("o3-pro", 20.00, None, 80.00),
    ("o3", 2.00, 0.50, 8.00),
    ("o4-mini", 1.10, 0.275, 4.40),
    ("o3-mini", 1.10, 0.55, 4.40),
    # The list names the snapshots these aliases point to: gpt-4-turbo-2024-04-09
    # and gpt-4-0613 (https://developers.openai.com/api/docs/deprecations).
    ("gpt-4-turbo", 10.00, None, 30.00),
    ("gpt-4", 30.00, None, 60.00),
    ("gpt-3.5-turbo", 0.50, None, 1.50),
    # Embeddings have no output tokens; the list shows "-" for their output.
    ("text-embedding-3-small", 0.02, None, 0.0),
    ("text-embedding-3-large", 0.13, None, 0.0),
    ("text-embedding-ada-002", 0.10, None, 0.0),
]

#: The list's "Short context cache writes" column, USD per 1M tokens. Only GPT-5.6
#: and later models carry one; every other model writes at its input price.
_OPENAI_LISTED_CACHE_WRITE_PRICES = [
    ("gpt-6-astra", 12.50),
    ("gpt-6.1-sol", 2.50),
    ("gpt-6-luna", 0.125),
    ("gpt-6-sol", 2.50),
    ("gpt-5.6-sol", 5.00),
    ("gpt-5.6-terra", 2.50),
    ("gpt-5.6-luna", 0.25),
    ("gpt-5.6-cyber", 15.625),
]


def _one_million(**tokens: int) -> NormalizedTokenUsage:
    """1M tokens, all on the leg(s) named, so the cost in USD is the per-1M price."""
    return NormalizedTokenUsage(total_tokens=1_000_000, **tokens)


@pytest.mark.invariant
class TestOpenAIListPrices:
    @pytest.mark.parametrize("model, input_per_m, cached_per_m, output_per_m", _OPENAI_LIST_PRICES)
    def test_an_uncached_input_token_costs_the_listed_input_price(
        self, model: str, input_per_m: float, cached_per_m: float | None, output_per_m: float
    ) -> None:
        assert calculate_cost(model, _one_million(prompt_tokens=1_000_000)) == pytest.approx(input_per_m)

    @pytest.mark.parametrize("model, input_per_m, cached_per_m, output_per_m", _OPENAI_LIST_PRICES)
    def test_a_cache_read_costs_the_listed_cached_input_price(
        self, model: str, input_per_m: float, cached_per_m: float | None, output_per_m: float
    ) -> None:
        # Where the list shows no cached-input price there is no discount, and a
        # read costs the input price: "GPT-5.5 Pro does not offer a cached input
        # discount" (https://developers.openai.com/api/docs/models/gpt-5.5-pro).
        expected = input_per_m if cached_per_m is None else cached_per_m
        usage = _one_million(prompt_tokens=1_000_000, cached_tokens=1_000_000)
        assert calculate_cost(model, usage) == pytest.approx(expected)

    @pytest.mark.parametrize("model, input_per_m, cached_per_m, output_per_m", _OPENAI_LIST_PRICES)
    def test_an_output_token_costs_the_listed_output_price(
        self, model: str, input_per_m: float, cached_per_m: float | None, output_per_m: float
    ) -> None:
        assert calculate_cost(model, _one_million(completion_tokens=1_000_000)) == pytest.approx(output_per_m)

    @pytest.mark.parametrize("model, write_per_m", _OPENAI_LISTED_CACHE_WRITE_PRICES)
    def test_a_cache_write_costs_the_listed_cache_write_price(self, model: str, write_per_m: float) -> None:
        assert calculate_cost(model, _one_million(cache_creation_tokens=1_000_000)) == pytest.approx(write_per_m)

    @pytest.mark.parametrize(
        "model, input_per_m",
        [
            (model, input_per_m)
            for model, input_per_m, _cached, _output in _OPENAI_LIST_PRICES
            if model not in dict(_OPENAI_LISTED_CACHE_WRITE_PRICES)
        ],
    )
    def test_without_a_listed_cache_write_price_a_write_costs_the_input_price(
        self, model: str, input_per_m: float
    ) -> None:
        assert calculate_cost(model, _one_million(cache_creation_tokens=1_000_000)) == pytest.approx(input_per_m)

    def test_every_bundled_openai_row_is_on_the_list(self) -> None:
        # An OpenAI row added without its list prices above has none of its legs
        # checked.
        openai_rows = {
            model for model in PRICING if model.startswith(("gpt-", "o1", "o3", "o4", "text-embedding-", "chat-"))
        }
        # o1-mini left the API on 2025-10-27 and the list no longer carries it
        # (https://developers.openai.com/api/docs/deprecations).
        assert openai_rows - {"o1-mini"} == {row[0] for row in _OPENAI_LIST_PRICES}


@pytest.mark.invariant
class TestCacheReadRateByModel:
    """2,000 uncached and 8,000 cached input tokens, then 500 output tokens, worked
    by hand from the list's per-1M prices for one model of each cached-input rate."""

    USAGE = NormalizedTokenUsage(prompt_tokens=10_000, cached_tokens=8_000, completion_tokens=500, total_tokens=10_500)

    @pytest.mark.parametrize(
        "model, expected",
        [
            # Half: 2,000 x $2.50 + 8,000 x $1.25 + 500 x $10. Unchanged.
            ("gpt-4o", 0.020),
            # A quarter: 2,000 x $2 + 8,000 x $0.50 + 500 x $8. Was $0.016, with
            # the reads at $1.00.
            ("gpt-4.1", 0.012),
            # A quarter: 2,000 x $1.10 + 8,000 x $0.275 + 500 x $4.40. Was $0.0088,
            # with the reads at $0.55.
            ("o4-mini", 0.0066),
            # A quarter, at o3's $2 / $8 list price: 2,000 x $2 + 8,000 x $0.50 +
            # 500 x $8. Was $0.08, at $10 / $40 with the reads at $5.
            ("o3", 0.012),
            # A tenth: 2,000 x $1.25 + 8,000 x $0.125 + 500 x $10.
            ("gpt-5", 0.0085),
            # A tenth: 2,000 x $4 + 8,000 x $0.40 + 500 x $20.
            ("gpt-5.6-sol", 0.0212),
            # A twentieth: 2,000 x $2 + 8,000 x $0.10 + 500 x $10.
            ("gpt-6.1-sol", 0.0098),
            # No cached-input discount: 10,000 x $30 + 500 x $180.
            ("gpt-5.5-pro", 0.39),
        ],
    )
    def test_cache_reads_cost_the_models_cached_input_price(self, model: str, expected: float) -> None:
        assert calculate_cost(model, self.USAGE) == pytest.approx(expected)


@pytest.mark.invariant
class TestGpt56CacheReadsAndWritesEndToEnd:
    """A GPT-5.6+ call that reads from and writes to the prompt cache.

    OpenAI counts both inside the input tokens: "input tokens use the
    uncached-input, cached-input, or cache-write rate"
    (https://developers.openai.com/api/docs/guides/prompt-caching). These 10,000
    input tokens are 1,000 uncached, 6,000 read and 3,000 written, then 400 output
    tokens. gpt-5.6-terra lists $2 input, $0.20 cached input, $2.50 cache writes and
    $12 output per 1M: 1,000 x $2 + 6,000 x $0.20 + 3,000 x $2.50 + 400 x $12.
    """

    PAYLOAD = {
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "prompt_tokens": 10_000,
        "cached_tokens": 6_000,
        "cache_write_tokens": 3_000,
        "completion_tokens": 400,
        "total_tokens": 10_400,
    }
    EXPECTED = 0.0155

    def test_price_cost_record_prices_each_token_once_at_its_listed_price(self) -> None:
        assert price_cost_record(dict(self.PAYLOAD)) == pytest.approx(self.EXPECTED)

    def test_calculate_cost_prices_the_normalized_usage_the_same(self) -> None:
        # usage_from_payload's shape: the 3,000 writes are outside prompt_tokens.
        usage = NormalizedTokenUsage(
            prompt_tokens=7_000,
            cached_tokens=6_000,
            cache_creation_tokens=3_000,
            completion_tokens=400,
            total_tokens=10_400,
        )
        assert calculate_cost("gpt-5.6-terra", usage) == pytest.approx(self.EXPECTED)


@pytest.mark.invariant
class TestFastAndUltrafastTiers:
    """Service tiers at the list's own Batch, Flex, Fast and Ultrafast prices.

    Priority processing was renamed Fast mode on 2026-07-30. GPT-6 responses
    report ``service_tier: "fast"``; GPT-5.6 and earlier models report
    ``"priority"`` whichever was requested
    (https://developers.openai.com/api/docs/guides/fast-mode). Ultrafast exists for
    gpt-6-astra and gpt-6.1-sol
    (https://developers.openai.com/api/docs/guides/ultrafast-mode). USD per 1M
    tokens; a model without a listed cache-write price writes at its input price.
    """

    @pytest.mark.parametrize(
        "model, tier, input_per_m, cached_per_m, write_per_m, output_per_m",
        [
            ("gpt-6-sol", "fast", 4.00, 0.40, 5.00, 20.00),
            ("gpt-6-luna", "fast", 0.20, 0.02, 0.25, 1.00),
            ("gpt-5.6-sol", "priority", 8.00, 0.80, 10.00, 40.00),
            ("gpt-6-astra", "ultrafast", 60.00, 6.00, 75.00, 300.00),
            ("gpt-6.1-sol", "ultrafast", 12.00, 0.60, 15.00, 60.00),
            ("gpt-6-sol", "batch", 1.00, 0.10, 1.25, 5.00),
            ("gpt-5.4-mini", "flex", 0.375, 0.0375, 0.375, 2.25),
        ],
    )
    def test_each_leg_costs_the_listed_tier_price(
        self,
        model: str,
        tier: str,
        input_per_m: float,
        cached_per_m: float,
        write_per_m: float,
        output_per_m: float,
    ) -> None:
        legs = [
            (_one_million(prompt_tokens=1_000_000), input_per_m),
            (_one_million(prompt_tokens=1_000_000, cached_tokens=1_000_000), cached_per_m),
            (_one_million(cache_creation_tokens=1_000_000), write_per_m),
            (_one_million(completion_tokens=1_000_000), output_per_m),
        ]
        for usage, expected in legs:
            assert calculate_cost(model, usage, service_tier=tier) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "model, tier, listed_input_per_m", [("gpt-6-sol", "fast", 4.00), ("gpt-6.1-sol", "ultrafast", 12.00)]
    )
    def test_the_tier_applies_to_a_row_from_a_custom_table(
        self, model: str, tier: str, listed_input_per_m: float
    ) -> None:
        # Both models list $2 standard input; the tier alone accounts for the rest.
        table = {model: {"input": 0.002, "output": 0.010}}
        usage = _one_million(prompt_tokens=1_000_000)
        assert calculate_cost(model, usage, table, service_tier=tier) == pytest.approx(listed_input_per_m)


# ---------------------------------------------------------------------------
# Regression for the 2026-10-16 refresh: every row of the three bundled tables
# costs what it did before, except the rows the refresh re-priced on purpose.
# ---------------------------------------------------------------------------

#: 1M input tokens, 250K of them cache reads, plus 50K cache writes and 100K output.
_REGRESSION_USAGE = NormalizedTokenUsage(
    prompt_tokens=1_000_000,
    cached_tokens=250_000,
    cache_creation_tokens=50_000,
    completion_tokens=100_000,
    total_tokens=1_150_000,
)

#: What _REGRESSION_USAGE cost on each row before the refresh, recorded by running
#: the previous release's pricing module.
_COST_BEFORE_REFRESH = {
    ("PRICING", "gpt-4o"): 3.3125,
    ("PRICING", "gpt-4o-mini"): 0.19875,
    ("PRICING", "gpt-4o-2024-11-20"): 3.3125,
    ("PRICING", "gpt-4.1"): 2.65,
    ("PRICING", "gpt-4.1-mini"): 0.53,
    ("PRICING", "gpt-4.1-nano"): 0.1325,
    ("PRICING", "gpt-4-turbo"): 12.25,
    ("PRICING", "gpt-4"): 33.75,
    ("PRICING", "gpt-3.5-turbo"): 0.6125,
    ("PRICING", "o1"): 19.875,
    ("PRICING", "o1-mini"): 3.975,
    ("PRICING", "o3"): 13.25,
    ("PRICING", "o3-mini"): 1.4575,
    ("PRICING", "o4-mini"): 1.4575,
    ("PRICING", "claude-sonnet-4-5-20250929"): 4.0125,
    ("PRICING", "claude-sonnet-4-5"): 4.0125,
    ("PRICING", "claude-opus-4-20250115"): 20.0625,
    ("PRICING", "claude-opus-4-6"): 20.0625,
    ("PRICING", "claude-opus-4-7"): 20.0625,
    ("PRICING", "claude-haiku-4-5-20251001"): 1.07,
    ("PRICING", "claude-haiku-4-5"): 1.07,
    ("PRICING", "claude-haiku-3-5-20241022"): 1.07,
    ("PRICING", "claude-haiku-3-5"): 1.07,
    ("PRICING", "claude-3-5-sonnet-20241022"): 4.0125,
    ("PRICING", "claude-3-5-sonnet"): 4.0125,
    ("PRICING", "claude-3-opus-20240229"): 20.0625,
    ("PRICING", "claude-3-opus"): 20.0625,
    ("PRICING", "claude-3-haiku-20240307"): 0.334375,
    ("PRICING", "claude-3-haiku"): 0.334375,
    ("PRICING", "gemini-2.5-pro"): 2.078125,
    ("PRICING", "gemini-2.5-flash"): 0.0946875,
    ("PRICING", "gemini-2.0-flash"): 0.12625,
    ("PRICING", "gemini-1.5-pro"): 1.578125,
    ("PRICING", "gemini-1.5-flash"): 0.0946875,
    ("PRICING", "llama-3.3-70b"): 1.01475,
    ("PRICING", "llama-3.1-70b"): 1.01475,
    ("PRICING", "llama-3.1-8b"): 0.2255,
    ("PRICING", "mistral-large"): 2.45,
    ("PRICING", "mistral-small"): 0.245,
    ("PRICING", "text-embedding-3-small"): 0.0185,
    ("PRICING", "text-embedding-3-large"): 0.12025,
    ("PRICING", "text-embedding-ada-002"): 0.0925,
    ("AZURE_PRICING", "gpt-4o"): 3.64375,
    ("AZURE_PRICING", "gpt-4o-mini"): 0.218625,
    ("AZURE_PRICING", "gpt-4-turbo"): 13.475,
    ("AZURE_PRICING", "gpt-4"): 37.125,
    ("AZURE_PRICING", "gpt-35-turbo"): 0.67375,
    ("BEDROCK_PRICING", "anthropic.claude-3-5-sonnet-20241022-v2:0"): 4.0125,
    ("BEDROCK_PRICING", "anthropic.claude-3-opus-20240229-v1:0"): 20.0625,
    ("BEDROCK_PRICING", "anthropic.claude-3-haiku-20240307-v1:0"): 0.334375,
    ("BEDROCK_PRICING", "meta.llama3-1-70b-instruct-v1:0"): 1.01475,
    ("BEDROCK_PRICING", "meta.llama3-1-8b-instruct-v1:0"): 0.2255,
    ("BEDROCK_PRICING", "cohere.command-r-plus-v1:0"): 4.275,
    ("BEDROCK_PRICING", "cohere.command-r-v1:0"): 0.6125,
    ("BEDROCK_PRICING", "amazon.nova-micro-v1:0"): 0.046375,
    ("BEDROCK_PRICING", "amazon.nova-lite-v1:0"): 0.0795,
    ("BEDROCK_PRICING", "amazon.nova-pro-v1:0"): 1.06,
    ("BEDROCK_PRICING", "amazon.nova-premier-v1:0"): 3.5625,
    ("BEDROCK_PRICING", "amazon.titan-text-lite-v1"): 0.15875,
    ("BEDROCK_PRICING", "amazon.titan-text-express-v1"): 0.245,
    ("BEDROCK_PRICING", "amazon.titan-text-premier-v1:0"): 0.6125,
    ("BEDROCK_PRICING", "mistral.mistral-7b-instruct-v0:2"): 0.15875,
    ("BEDROCK_PRICING", "mistral.mixtral-8x7b-instruct-v0:1"): 0.48625,
    ("BEDROCK_PRICING", "mistral.mistral-large-2402-v1:0"): 4.9,
    ("BEDROCK_PRICING", "mistral.mistral-large-2407-v1:0"): 2.45,
    ("BEDROCK_PRICING", "mistral.mistral-small-2402-v1:0"): 1.225,
    ("BEDROCK_PRICING", "ai21.jamba-1-5-large-v1:0"): 2.65,
    ("BEDROCK_PRICING", "ai21.jamba-1-5-mini-v1:0"): 0.225,
}

#: The rows the refresh re-priced, and what _REGRESSION_USAGE costs on them now,
#: worked from the list prices per 1M tokens: 750K uncached and 250K cached input,
#: 50K written at the input price, 100K output.
_COST_AFTER_REFRESH = {
    # o3's list price is $2 / $8 (was $10 / $40) and a read costs $0.50:
    # 1.5 + 0.125 + 0.1 + 0.8.
    ("PRICING", "o3"): 2.525,
    # A read costs a quarter of the input price, not half.
    ("PRICING", "gpt-4.1"): 2.525,  # 1.5 + 0.125 + 0.1 + 0.8
    ("PRICING", "gpt-4.1-mini"): 0.505,  # 0.3 + 0.025 + 0.02 + 0.16
    ("PRICING", "gpt-4.1-nano"): 0.12625,  # 0.075 + 0.00625 + 0.005 + 0.04
    ("PRICING", "o4-mini"): 1.38875,  # 0.825 + 0.06875 + 0.055 + 0.44
    # No cached-input price on the list: a read costs the input price, not half.
    ("PRICING", "gpt-4-turbo"): 13.5,  # 7.5 + 2.5 + 0.5 + 3
    ("PRICING", "gpt-4"): 37.5,  # 22.5 + 7.5 + 1.5 + 6
    ("PRICING", "gpt-3.5-turbo"): 0.675,  # 0.375 + 0.125 + 0.025 + 0.15
    ("PRICING", "text-embedding-3-small"): 0.021,  # 0.015 + 0.005 + 0.001
    ("PRICING", "text-embedding-3-large"): 0.1365,  # 0.0975 + 0.0325 + 0.0065
    ("PRICING", "text-embedding-ada-002"): 0.105,  # 0.075 + 0.025 + 0.005
}

_BUNDLED_TABLES = {"PRICING": PRICING, "AZURE_PRICING": AZURE_PRICING, "BEDROCK_PRICING": BEDROCK_PRICING}


@pytest.mark.invariant
@pytest.mark.parametrize("table_name, model", sorted(_COST_BEFORE_REFRESH))
def test_a_row_costs_what_it_did_before_the_refresh_unless_it_was_repriced(table_name: str, model: str) -> None:
    key = (table_name, model)
    expected = _COST_AFTER_REFRESH.get(key, _COST_BEFORE_REFRESH[key])
    assert calculate_cost(model, _REGRESSION_USAGE, _BUNDLED_TABLES[table_name]) == pytest.approx(expected)
