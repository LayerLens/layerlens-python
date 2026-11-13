"""Claude Haiku 4.5 and Claude Opus 4.6 / 4.7 cost what Anthropic lists.

The bundled table priced Claude Haiku 4.5 at Claude 3.5 Haiku's $0.80 / $4 per million tokens, 20%
low, and Claude Opus 4.6 and 4.7 at Claude Opus 4 / 4.1's $15 / $75, three times too high (docs
E2E 2026-11-10, L3 AD-18). The prices are Anthropic's model pricing table,
https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-10, in USD per million
tokens:

    Claude Opus 4.7   base input $5, 5m cache writes $6.25, cache hits $0.50, output $25
    Claude Opus 4.6   base input $5, 5m cache writes $6.25, cache hits $0.50, output $25
    Claude Haiku 4.5  base input $1, 5m cache writes $1.25, cache hits $0.10, output $5

A cache read is a tenth of the input price and a cache write 1.25 times it, as the table prices
every Claude row, so each row only needs its input and output prices.
"""

from __future__ import annotations

import pytest

from layerlens.instrument.adapters.providers.pricing import PRICING, calculate_cost
from layerlens.instrument.adapters.providers.token_usage import NormalizedTokenUsage

MILLION = 1_000_000


def _usage(*, prompt: int = 0, cached: int = 0, writes: int = 0, completion: int = 0) -> NormalizedTokenUsage:
    return NormalizedTokenUsage(
        prompt_tokens=prompt,
        cached_tokens=cached or None,
        cache_creation_tokens=writes or None,
        completion_tokens=completion,
        total_tokens=prompt + writes + completion,
    )


@pytest.mark.parametrize(
    "model, input_per_m, write_per_m, read_per_m, output_per_m",
    [
        ("claude-opus-4-7", 5.00, 6.25, 0.50, 25.00),
        ("claude-opus-4-6", 5.00, 6.25, 0.50, 25.00),
        ("claude-haiku-4-5", 1.00, 1.25, 0.10, 5.00),
        ("claude-haiku-4-5-20251001", 1.00, 1.25, 0.10, 5.00),
    ],
)
def test_each_kind_of_token_costs_the_list_price(
    model: str, input_per_m: float, write_per_m: float, read_per_m: float, output_per_m: float
) -> None:
    assert model in PRICING
    assert calculate_cost(model, _usage(prompt=MILLION)) == pytest.approx(input_per_m)
    assert calculate_cost(model, _usage(writes=MILLION)) == pytest.approx(write_per_m)
    assert calculate_cost(model, _usage(prompt=MILLION, cached=MILLION)) == pytest.approx(read_per_m)
    assert calculate_cost(model, _usage(completion=MILLION)) == pytest.approx(output_per_m)


def test_a_dated_haiku_4_5_reply_costs_what_the_alias_does() -> None:
    # The live call the E2E priced: 12 input and 6 output tokens of claude-haiku-4-5-20251001.
    usage = _usage(prompt=12, completion=6)
    assert calculate_cost("claude-haiku-4-5-20251001", usage) == pytest.approx(4.2e-05)
    assert calculate_cost("claude-haiku-4-5-20251001", usage) == calculate_cost("claude-haiku-4-5", usage)
