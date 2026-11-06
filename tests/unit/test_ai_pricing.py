"""The pure AI pricing rules: token counts, the cost formula, long context and model names.

Every model here is invented (``lab-model-*``) and priced in round numbers per
one million tokens, so each expected total can be checked by hand. Costs are
exact decimals and compared exactly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from specivo.core.ai_pricing import (
    ModelPrice,
    TokenCounts,
    default_cache_write_1h,
    is_long_context_request,
    normalize_model_name,
    normalize_provider,
    price_usage,
)
from specivo.core.money import Money

pytestmark = pytest.mark.unit


def dec(value: str | int) -> Decimal:
    return Decimal(value)


def usd(amount: str) -> Money:
    return Money(amount, "USD")


def cost_of_request(request: TokenCounts, price: ModelPrice) -> Money:
    """Price a single request in the tier it falls in, as the usage reporter buckets it."""
    if is_long_context_request(request, price.long_context_threshold):
        return price_usage(TokenCounts(), request, price).money
    return price_usage(request, None, price).money


#: A model with every base price, the one-hour cache write derived from input.
LAB_MODEL_1 = ModelPrice(
    currency="USD",
    input=dec("2"),
    output=dec("8"),
    cache_write_5m=dec("2.5"),
    cache_write_1h=default_cache_write_1h(dec("2")),
    cache_read=dec("0.2"),
)

#: A two-tier model with a deliberately tiny threshold.
LAB_MODEL_2 = ModelPrice(
    currency="USD",
    input=dec("1"),
    output=dec("5"),
    cache_read=dec("0.1"),
    long_context_threshold=1_000,
    long_input=dec("2"),
    long_output=dec("10"),
    long_cache_read=dec("0.2"),
)

#: A two-tier model with a realistic threshold.
LAB_MODEL_3 = ModelPrice(
    currency="USD",
    input=dec("4"),
    output=dec("20"),
    cache_read=dec("0.4"),
    long_context_threshold=100_000,
    long_input=dec("8"),
    long_output=dec("40"),
    long_cache_read=dec("0.8"),
)


# ---------------------------------------------------------------------------
# Cache writes
# ---------------------------------------------------------------------------


def test_five_minute_and_one_hour_cache_writes_are_priced_separately() -> None:
    usage = TokenCounts(cache_write_5m=400_000, cache_write_1h=250_000, cache_read=1_000_000)
    result = price_usage(usage, None, LAB_MODEL_1)
    # 0.4M × 2.5 + 0.25M × 4 (twice input) + 1M × 0.2
    assert result.money == usd("2.2")
    assert not result.is_partial
    assert result.basis == "calculated"


def test_cache_writes_without_a_duration_split_use_the_five_minute_rate() -> None:
    # A source that reports one flat cache write count puts it all in cache_write_5m.
    assert price_usage(TokenCounts(cache_write_5m=2_000_000), None, LAB_MODEL_1).money == usd("5")


def test_one_hour_cache_write_defaults_to_twice_input() -> None:
    assert default_cache_write_1h(dec("2")) == dec("4")
    assert default_cache_write_1h(dec("0.75")) == dec("1.5")
    assert default_cache_write_1h(None) is None
    assert LAB_MODEL_1.rate("cache_write_1h") == dec("4")


# ---------------------------------------------------------------------------
# Long context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_tokens", "expected"),
    [
        (399, "0.000959"),  # 999 tokens of context: base tier
        (400, "0.000960"),  # exactly the threshold: still the base tier
        (401, "0.001922"),  # one token over: the whole request at long rates
    ],
)
def test_crossing_the_threshold_moves_the_whole_request_to_long_rates(input_tokens: int, expected: str) -> None:
    request = TokenCounts(input=input_tokens, output=100, cache_read=600)
    assert cost_of_request(request, LAB_MODEL_2).amount == dec(expected)


def test_cache_reads_count_toward_the_long_context_threshold() -> None:
    # 5K fresh input alone is far below 100K; with 200K cache reads the request is long.
    long_request = TokenCounts(input=5_000, output=2_000, cache_read=200_000)
    assert is_long_context_request(long_request, 100_000)
    # 5K × 8 + 2K × 40 + 200K × 0.8
    assert cost_of_request(long_request, LAB_MODEL_3).amount == dec("0.28")

    short_request = TokenCounts(input=5_000, output=2_000, cache_read=90_000)
    assert not is_long_context_request(short_request, 100_000)
    # 5K × 4 + 2K × 20 + 90K × 0.4
    assert cost_of_request(short_request, LAB_MODEL_3).amount == dec("0.096")


def test_cache_writes_count_toward_the_threshold_but_output_does_not() -> None:
    assert is_long_context_request(TokenCounts(input=10, cache_write_1h=100_000), 100_000)
    assert is_long_context_request(TokenCounts(cache_write_5m=100_001), 100_000)
    assert not is_long_context_request(TokenCounts(input=10, output=500_000, reasoning=500_000), 100_000)


def test_no_threshold_means_never_long() -> None:
    assert not is_long_context_request(TokenCounts(input=50_000_000), None)


def test_long_bucket_uses_long_prices_and_falls_back_per_class() -> None:
    price = ModelPrice(
        currency="USD",
        input=dec("2"),
        output=dec("10"),
        cache_read=dec("0.2"),
        long_context_threshold=100_000,
        long_input=dec("4"),
        long_output=dec("15"),
    )
    long_tokens = TokenCounts(input=1_000_000, output=1_000_000, cache_read=1_000_000, reasoning=1_000_000)
    # long input 4 + long output 15 + cache read (no long price) 0.2 + reasoning as long output 15
    assert price_usage(TokenCounts(), long_tokens, price).money == usd("34.2")
    # plus 1M base-tier input at 2
    assert price_usage(TokenCounts(input=1_000_000), long_tokens, price).money == usd("36.2")


# ---------------------------------------------------------------------------
# The cost formula
# ---------------------------------------------------------------------------


def test_every_class_is_priced_per_million_exactly() -> None:
    usage = TokenCounts(
        input=1_500, output=7_000, cache_write_5m=20_000, cache_write_1h=5_000, cache_read=600_000, reasoning=1_000
    )
    # 1.5K × 2 + 7K × 8 + 20K × 2.5 + 5K × 4 + 600K × 0.2 + 1K × 8 (reasoning as output) = 257_000 / 1M
    assert price_usage(usage, None, LAB_MODEL_1).money == usd("0.257")


@pytest.mark.parametrize(
    ("rates", "expected"),
    [
        (("1", "4", "1.25", "2", "0.1"), "8.35"),
        (("0.25", "1", "0.3", "0.5", "0.025"), "2.075"),
    ],
)
def test_one_million_tokens_in_each_class_cost_the_sum_of_the_rates(
    rates: tuple[str, str, str, str, str], expected: str
) -> None:
    model = ModelPrice(
        currency="USD",
        input=dec(rates[0]),
        output=dec(rates[1]),
        cache_write_5m=dec(rates[2]),
        cache_write_1h=dec(rates[3]),
        cache_read=dec(rates[4]),
    )
    usage = TokenCounts(
        input=1_000_000, output=1_000_000, cache_write_5m=1_000_000, cache_write_1h=1_000_000, cache_read=1_000_000
    )
    assert price_usage(usage, None, model).money.amount == dec(expected)


def test_a_zero_price_is_priced_not_missing() -> None:
    free = ModelPrice(currency="USD", input=dec("0"), output=dec("0"), cache_read=dec("0"))
    result = price_usage(TokenCounts(input=100, output=50, cache_read=1_000), None, free)
    assert result.money == usd("0")
    assert result.unpriced_classes == ()
    assert not result.is_partial and result.basis == "calculated"


def test_reasoning_falls_back_to_the_output_price() -> None:
    assert price_usage(TokenCounts(reasoning=1_000_000), None, LAB_MODEL_1).money == usd("8")


def test_reasoning_in_the_long_tier_without_a_long_output_price_uses_output() -> None:
    price = ModelPrice(currency="USD", input=dec("1"), output=dec("6"), long_context_threshold=10, long_input=dec("2"))
    assert price_usage(TokenCounts(), TokenCounts(reasoning=1_000_000), price).money == usd("6")


def test_reasoning_uses_its_own_price_in_both_tiers() -> None:
    priced = ModelPrice(
        currency="USD",
        input=dec("1"),
        output=dec("6"),
        reasoning=dec("3"),
        long_context_threshold=10,
        long_output=dec("12"),
    )
    assert price_usage(TokenCounts(reasoning=1_000_000), None, priced).money == usd("3")
    assert price_usage(TokenCounts(), TokenCounts(reasoning=1_000_000), priced).money == usd("3")


def test_classes_with_tokens_but_no_price_make_the_result_partial() -> None:
    no_writes = ModelPrice(currency="USD", input=dec("1"), output=dec("5"), cache_read=dec("0.1"))
    result = price_usage(TokenCounts(input=2_000_000, cache_write_5m=300, cache_write_1h=9), None, no_writes)
    assert result.money == usd("2")
    assert result.unpriced_classes == ("cache_write_5m", "cache_write_1h")
    assert result.is_partial and result.basis == "partial"


def test_unpriced_classes_are_listed_once_base_tier_first() -> None:
    no_writes = ModelPrice(currency="USD", input=dec("1"), output=dec("5"), long_context_threshold=10)
    result = price_usage(
        TokenCounts(cache_write_1h=4),
        TokenCounts(input=1_000_000, cache_write_5m=7, cache_write_1h=3),
        no_writes,
    )
    assert result.money == usd("1")
    assert result.unpriced_classes == ("cache_write_1h", "cache_write_5m")


def test_classes_without_tokens_never_count_as_unpriced() -> None:
    no_cache = ModelPrice(currency="USD", input=dec("3"), output=dec("9"))
    assert price_usage(TokenCounts(input=10), TokenCounts(), no_cache).unpriced_classes == ()
    assert price_usage(TokenCounts(input=10), None, no_cache).unpriced_classes == ()


def test_prices_in_other_currencies_stay_in_that_currency() -> None:
    cny = ModelPrice(currency="CNY", input=dec("4"), output=dec("16"))
    assert price_usage(TokenCounts(input=250_000, output=125_000), None, cny).money == Money("3", "CNY")
    thb = ModelPrice(currency="THB", input=dec("100"), output=dec("500"))
    assert price_usage(TokenCounts(output=2), None, thb).money == Money("0.001", "THB")


def test_no_tokens_cost_zero() -> None:
    assert price_usage(TokenCounts(), None, LAB_MODEL_1).money == usd("0")
    assert price_usage(TokenCounts(), TokenCounts(), LAB_MODEL_2).money == usd("0")


def test_unknown_token_class_is_refused() -> None:
    with pytest.raises(ValueError):
        LAB_MODEL_1.rate("audio")


# ---------------------------------------------------------------------------
# Token counts
# ---------------------------------------------------------------------------


def test_token_counts_refuse_negative_and_non_int_values() -> None:
    with pytest.raises(ValueError):
        TokenCounts(cache_read=-5)
    with pytest.raises(TypeError):
        TokenCounts(output=2.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TokenCounts(input=False)


def test_token_counts_from_a_mapping() -> None:
    with pytest.raises(ValueError):
        TokenCounts.from_mapping({"output": 1, "video": 3})
    assert TokenCounts.from_mapping(None).is_zero()
    assert TokenCounts.from_mapping({}).is_zero()
    counts = TokenCounts.from_mapping({"input": 5, "cache_read": 7, "output": 3})
    assert counts.total == 15
    assert counts.context_tokens == 12
    assert not counts.is_zero()


# ---------------------------------------------------------------------------
# Model names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lab-model-1", (None, "lab-model-1")),
        ("  Lab-Model-1 ", (None, "lab-model-1")),
        ("anthropic/lab-model-2", ("anthropic", "lab-model-2")),
        ("Anthropic/Lab-Model-2", ("anthropic", "lab-model-2")),
        ("openrouter/openai/lab-model-3", ("openai", "lab-model-3")),
        ("openai/openrouter/lab-model-3", ("openai", "lab-model-3")),
        ("models/lab-model-4", (None, "lab-model-4")),
        ("google/lab-model-4", ("gemini", "lab-model-4")),
        ("x-ai/lab-model-5", ("xai", "lab-model-5")),
        ("litellm/deepseek/lab-model-6", ("deepseek", "lab-model-6")),
        ("accounts/acme/models/x", (None, "accounts/acme/models/x")),
        ("anthropic/", (None, "anthropic/")),
    ],
)
def test_normalize_model_name(raw: str, expected: tuple[str | None, str]) -> None:
    assert normalize_model_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Anthropic ", "anthropic"),
        ("google", "gemini"),
        ("x-ai", "xai"),
        ("openrouter", "openrouter"),
        ("acme-ai", "acme-ai"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_provider(raw: str | None, expected: str | None) -> None:
    assert normalize_provider(raw) == expected
