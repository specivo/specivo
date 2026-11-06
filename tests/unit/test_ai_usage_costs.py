"""The pure AI usage rules: the price instant, the marginal cost per cost type and the token bounds."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from specivo.core.ai_pricing import ModelPrice, TokenCounts
from specivo.core.ai_usage import (
    MAX_TOKENS_PER_CLASS,
    CostInputs,
    check_bucket_totals,
    check_tokens,
    compute_costs,
    priced_at_for,
)
from specivo.core.money import Money
from specivo.services.permission_service import PERMISSIONS, Permission

pytestmark = pytest.mark.unit

PRICE = ModelPrice(currency="USD", input=Decimal("2"), output=Decimal("10"))
LOCAL = ModelPrice(currency="USD", input=Decimal("0.1"), output=Decimal("0.1"))
TOKENS = TokenCounts(input=1_000_000, output=100_000)


def costs(cost_type: str, **kw: object):
    values: dict = {"cost_type": cost_type, "tokens": TOKENS, "long_context": None, "price": PRICE}
    values.update(kw)
    return compute_costs(CostInputs(**values))


def test_priced_at_is_started_at_in_utc() -> None:
    started = datetime(2026, 3, 10, 9, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    assert priced_at_for(date(2026, 3, 10), started) == datetime(2026, 3, 10, 2, 0, tzinfo=UTC)


def test_priced_at_without_start_is_the_utc_midnight_of_the_bucket() -> None:
    assert priced_at_for(date(2026, 3, 10), None) == datetime(2026, 3, 10, tzinfo=UTC)


def test_priced_at_refuses_a_naive_start() -> None:
    with pytest.raises(ValueError):
        priced_at_for(date(2026, 3, 10), datetime(2026, 3, 10, 9, 0))


def test_api_uses_the_api_equivalent_without_a_reported_cost() -> None:
    result = costs("api")
    assert result.marginal == result.api_equivalent == Money("3", "USD")
    assert result.cost_basis == "calculated"


def test_api_uses_the_reported_cost_when_given() -> None:
    result = costs("api", reported_cost=Money("1.5", "EUR"))
    assert result.marginal == Money("1.5", "EUR")
    assert result.api_equivalent == Money("3", "USD")
    assert result.cost_basis == "reported"
    assert costs("api", reported_cost=Money("1", "EUR"), reported_by_person=True).cost_basis == "manual"


@pytest.mark.parametrize("cost_type", ["subscription", "free"])
def test_flat_rate_and_free_usage_costs_nothing_more(cost_type: str) -> None:
    result = costs(cost_type)
    assert result.marginal == Money("0", "USD")
    assert result.api_equivalent == Money("3", "USD")


def test_subscription_zero_is_in_the_plan_currency() -> None:
    assert costs("subscription", subscription_currency="THB").marginal == Money("0", "THB")


def test_unknown_has_no_marginal_cost() -> None:
    result = costs("unknown")
    assert result.marginal is None
    assert result.api_equivalent == Money("3", "USD")


def test_local_row_prices_the_marginal_cost_and_the_baseline_the_api_equivalent() -> None:
    result = costs("local", price=LOCAL, is_local_price=True, baseline_price=PRICE)
    assert result.marginal == Money("0.11", "USD")
    assert result.api_equivalent == Money("3", "USD")
    without_baseline = costs("local", price=LOCAL, is_local_price=True)
    assert without_baseline.api_equivalent is None
    assert without_baseline.marginal == Money("0.11", "USD")


def test_local_usage_of_a_cloud_model_costs_nothing_more() -> None:
    result = costs("local")
    assert result.marginal == Money("0", "USD")
    assert result.api_equivalent == Money("3", "USD")


def test_unpriced_is_never_zero() -> None:
    result = costs("api", price=None)
    assert result.marginal is None and result.api_equivalent is None
    assert result.cost_basis == "unpriced"
    free = costs("free", price=None, zero_currency="EUR")
    assert free.marginal == Money("0", "EUR") and free.cost_basis == "unpriced"


def test_partial_when_a_class_is_unpriced() -> None:
    result = costs("api", tokens=TokenCounts(input=1_000_000, cache_read=10))
    assert result.cost_basis == "partial"
    assert result.marginal == Money("2", "USD")


def test_long_context_bucket_uses_long_prices() -> None:
    price = ModelPrice(
        currency="USD",
        input=Decimal("3"),
        output=Decimal("15"),
        long_context_threshold=200_000,
        long_input=Decimal("6"),
    )
    result = costs("api", tokens=TokenCounts(), long_context=TokenCounts(input=1_000_000), price=price)
    assert result.api_equivalent == Money("6", "USD")


def test_both_buckets_are_priced_once_each_at_their_own_tier() -> None:
    """The buckets are disjoint: base tokens at base rates, long-context tokens at long rates, then summed."""
    price = ModelPrice(
        currency="USD",
        input=Decimal("2"),
        output=Decimal("10"),
        cache_read=Decimal("0.2"),
        long_context_threshold=200_000,
        long_input=Decimal("4"),
        long_output=Decimal("15"),
        long_cache_read=Decimal("0.4"),
    )
    base = TokenCounts(input=1_000_000, output=100_000, cache_read=2_000_000)  # 2 + 1 + 0.4 = 3.4
    long = TokenCounts(input=500_000, output=200_000, cache_read=1_000_000)  # 2 + 3 + 0.4 = 5.4
    result = costs("api", tokens=base, long_context=long, price=price)
    assert result.api_equivalent == Money("8.8", "USD")
    assert result.marginal == Money("8.8", "USD")
    # Neither bucket is charged again at the other tier.
    alone = costs("api", tokens=base, long_context=None, price=price).api_equivalent
    assert alone == Money("3.4", "USD")


def test_bucket_totals_bound_applies_to_the_sum() -> None:
    half = MAX_TOKENS_PER_CLASS // 2 + 1
    tokens = check_tokens({"input": half})
    long = check_tokens({"input": half}, "long_context")
    with pytest.raises(ValueError, match="input"):
        check_bucket_totals(tokens, long)
    check_bucket_totals(tokens, check_tokens({"input": half - 2}, "long_context"))
    check_bucket_totals(check_tokens({"input": MAX_TOKENS_PER_CLASS}), None)


def test_check_tokens_bounds() -> None:
    assert check_tokens({"input": 5}) == {
        "input": 5,
        "output": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 0,
        "reasoning": 0,
    }
    for bad in ({"input": -1}, {"input": MAX_TOKENS_PER_CLASS + 1}, {"input": 1.5}, {"input": True}, {"prompt": 1}):
        with pytest.raises(ValueError):
            check_tokens(bad)


def test_ai_usage_permissions_are_in_the_catalogue() -> None:
    for permission in (
        Permission.VIEW_AI_USAGE,
        Permission.LOG_AI_USAGE,
        Permission.MANAGE_AI_USAGE,
        Permission.LOG_AI_USAGE_FOR_OTHERS,
    ):
        assert permission in PERMISSIONS


def test_entry_token_views_keep_the_buckets_apart_and_sum_them() -> None:
    from specivo.models.ai_usage_entry import AiUsageEntry

    entry = AiUsageEntry(
        input=100, output=10, cache_write_5m=0, cache_write_1h=0, cache_read=1000, reasoning=0,
        long_context={"input": 50, "cache_read": 400},
    )  # fmt: skip
    assert entry.tokens()["input"] == 100
    assert entry.long_context_tokens() == {
        "input": 50,
        "output": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 400,
        "reasoning": 0,
    }
    assert entry.all_tokens()["input"] == 150 and entry.all_tokens()["cache_read"] == 1400
    assert sum(entry.all_tokens().values()) == 1560
    entry.long_context = None
    assert entry.all_tokens() == entry.tokens()
