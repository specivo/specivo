"""The pure AI usage rules: the price instant, the marginal cost per cost type and the token bounds."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from specivo.core.ai_pricing import ModelPrice, TokenCounts
from specivo.core.ai_usage import MAX_TOKENS_PER_CLASS, CostInputs, check_tokens, compute_costs, priced_at_for
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
