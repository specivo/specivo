"""AI subscriptions, pure rules: plan presets, UTC-day coverage, resolver precedence and seat-cost proration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from specivo.core.ai_subscriptions import (
    PLAN_PRESETS,
    SEEDED_TOOLS,
    PlainSubscription,
    covers_day,
    get_preset,
    is_plan_key,
    monthly_fee,
    months_between,
    normalize_tool_slug,
    period_end,
    period_start,
    pick_covering,
    prorated_fee,
    seat_charges,
    subscription_charges,
    totals_by_currency,
    utc_day,
)
from specivo.core.currency_data import minor_units_for
from specivo.core.money import Money

pytestmark = pytest.mark.unit

BANGKOK = ZoneInfo("Asia/Bangkok")


def _sub(
    sub_id: int = 1,
    *,
    provider: str = "anthropic",
    tools: list[str] | None = None,
    amount: str = "20",
    currency: str = "USD",
    period: str = "month",
    starts: date = date(2026, 1, 1),
    ends: date | None = None,
) -> PlainSubscription:
    return PlainSubscription(
        id=sub_id,
        user_id=7,
        provider=provider,
        amount=Decimal(amount),
        currency_code=currency,
        billing_period=period,
        starts_on=starts,
        ends_on=ends,
        tool_slugs=list(tools or []),
    )


# ---------------------------------------------------------------------------
# Presets and tools
# ---------------------------------------------------------------------------


def test_presets_cover_the_plans_the_register_offers() -> None:
    keys = {p.key for p in PLAN_PRESETS}
    assert {
        "claude-pro",
        "claude-max-5x",
        "claude-max-20x",
        "chatgpt-plus",
        "chatgpt-pro",
        "cursor-pro",
        "copilot-pro",
        "google-ai-pro",
        "copilot-max",
        "google-ai-ultra-5x",
        "google-ai-ultra-20x",
        "supergrok",
    } <= keys
    assert len(keys) == len(PLAN_PRESETS)
    assert "custom" not in keys
    assert is_plan_key("custom") and is_plan_key("claude-pro") and not is_plan_key("claude-ultra")
    assert get_preset("custom") is None


def test_preset_prices_are_valid_entered_amounts_and_tools_are_seeded() -> None:
    seeded = {slug for slug, *_rest in SEEDED_TOOLS}
    for preset in PLAN_PRESETS:
        assert preset.amount > 0
        places = -preset.amount.normalize().as_tuple().exponent  # type: ignore[operator]
        assert max(places, 0) <= (minor_units_for(preset.currency) or 0)
        assert preset.billing_period in ("month", "year")
        assert set(preset.tool_slugs) <= seeded
        assert normalize_tool_slug(preset.key) == preset.key


def test_seeded_tools_and_slug_rules() -> None:
    assert [s for s, *_ in SEEDED_TOOLS] == [
        "claude-code",
        "codex",
        "gemini-cli",
        "cursor",
        "copilot",
        "opencode",
        "other",
    ]
    assert normalize_tool_slug(" Claude-Code ") == "claude-code"
    for bad in ("", "x", "-lead", "under_score", "a" * 51, "spaces in", None, 3):
        assert normalize_tool_slug(bad) is None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_date_only_bounds_map_to_utc_instants() -> None:
    assert period_start(date(2026, 3, 1)) == datetime(2026, 3, 1, tzinfo=UTC)
    assert period_end(date(2026, 3, 31)) == datetime(2026, 4, 1, tzinfo=UTC)
    assert period_end(None) is None


def test_instants_are_matched_by_their_utc_day() -> None:
    # 06:30 in Bangkok on 1 April is still 31 March in UTC.
    assert utc_day(datetime(2026, 4, 1, 6, 30, tzinfo=BANGKOK)) == date(2026, 3, 31)
    assert utc_day(datetime(2026, 4, 1, 7, 0, tzinfo=BANGKOK)) == date(2026, 4, 1)
    assert utc_day(date(2026, 4, 1)) == date(2026, 4, 1)
    with pytest.raises(ValueError):
        utc_day(datetime(2026, 4, 1, 12, 0))


def test_both_ends_are_inclusive_and_open_end_runs_on() -> None:
    assert covers_day(date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 1))
    assert covers_day(date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 31))
    assert not covers_day(date(2026, 3, 1), date(2026, 3, 31), date(2026, 4, 1))
    assert not covers_day(date(2026, 3, 1), None, date(2026, 2, 28))
    assert covers_day(date(2026, 3, 1), None, date(2030, 1, 1))
    assert covers_day(date(2026, 3, 5), date(2026, 3, 5), date(2026, 3, 5))


# ---------------------------------------------------------------------------
# Resolver precedence
# ---------------------------------------------------------------------------

AT = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)


def test_listed_tool_wins_over_provider_plan() -> None:
    claude_pro = _sub(1, provider="anthropic")
    cursor_pro = _sub(2, provider="cursor", tools=["cursor"])
    found = pick_covering(
        [claude_pro, cursor_pro], tool_slug="cursor", provider="anthropic", at=AT, tool_default_provider=None
    )
    assert found is not None and found.subscription is cursor_pro and found.matched_by == "tool"


def test_provider_plan_covers_tools_of_its_provider() -> None:
    claude_pro = _sub(1, provider="anthropic")
    found = pick_covering(
        [claude_pro], tool_slug="claude-code", provider="anthropic", at=AT, tool_default_provider="anthropic"
    )
    assert found is not None and found.subscription is claude_pro and found.matched_by == "provider"


def test_provider_plan_does_not_cover_another_vendors_tool() -> None:
    claude_pro = _sub(1, provider="anthropic")
    # Claude models used in Cursor are not covered by Claude Pro.
    assert (
        pick_covering([claude_pro], tool_slug="cursor", provider="anthropic", at=AT, tool_default_provider=None) is None
    )
    # Codex with an Anthropic model: Codex is an OpenAI tool.
    assert (
        pick_covering([claude_pro], tool_slug="codex", provider="anthropic", at=AT, tool_default_provider="openai")
        is None
    )


def test_provider_must_match_the_usage() -> None:
    chatgpt = _sub(1, provider="openai")
    assert pick_covering([chatgpt], tool_slug="codex", provider="ollama", at=AT, tool_default_provider="openai") is None
    # No provider on the usage: the tool's default provider stands in.
    found = pick_covering([chatgpt], tool_slug="codex", provider=None, at=AT, tool_default_provider="openai")
    assert found is not None and found.subscription is chatgpt


def test_plan_with_listed_tools_is_not_a_provider_plan() -> None:
    copilot = _sub(1, provider="github", tools=["copilot"])
    assert pick_covering([copilot], tool_slug="codex", provider="github", at=AT, tool_default_provider="github") is None


def test_no_tool_given_matches_by_provider_only() -> None:
    claude = _sub(1, provider="anthropic")
    listed = _sub(2, provider="anthropic", tools=["claude-code"])
    found = pick_covering([claude, listed], tool_slug=None, provider="anthropic", at=AT)
    assert found is not None and found.subscription is claude


def test_latest_start_then_newest_row_wins_within_a_step() -> None:
    older = _sub(1, starts=date(2026, 1, 1))
    newer = _sub(2, starts=date(2026, 3, 1))
    same_start = _sub(3, starts=date(2026, 3, 1))
    found = pick_covering(
        [older, newer, same_start],
        tool_slug="claude-code",
        provider="anthropic",
        at=AT,
        tool_default_provider="anthropic",
    )
    assert found is not None and found.subscription is same_start


def test_resolver_respects_the_period_at_utc_day_edges() -> None:
    march = _sub(1, starts=date(2026, 3, 1), ends=date(2026, 3, 31))
    kwargs = {"tool_slug": "claude-code", "provider": "anthropic", "tool_default_provider": "anthropic"}
    last_instant = datetime(2026, 4, 1, tzinfo=UTC) - timedelta(microseconds=1)
    assert pick_covering([march], at=last_instant, **kwargs) is not None
    assert pick_covering([march], at=datetime(2026, 4, 1, tzinfo=UTC), **kwargs) is None
    assert pick_covering([march], at=datetime(2026, 3, 1, tzinfo=UTC), **kwargs) is not None
    assert pick_covering([march], at=datetime(2026, 2, 28, 23, 59, tzinfo=UTC), **kwargs) is None
    # 1 April 05:00 in Bangkok is 31 March 22:00 UTC: still covered.
    assert pick_covering([march], at=datetime(2026, 4, 1, 5, 0, tzinfo=BANGKOK), **kwargs) is not None


# ---------------------------------------------------------------------------
# Proration
# ---------------------------------------------------------------------------


def test_whole_month_is_the_whole_fee() -> None:
    share, days = prorated_fee(Decimal("20"), "month", date(2026, 1, 1), None, 2026, 2)
    assert (share, days) == (Decimal("20"), 28)


def test_partial_first_month_is_prorated_by_days() -> None:
    # Starts on 21 April: 10 of 30 days.
    share, days = prorated_fee(Decimal("30"), "month", date(2026, 4, 21), None, 2026, 4)
    assert days == 10
    assert share == Decimal("10")


def test_plan_ending_mid_month_is_prorated() -> None:
    # Ends on 10 March (inclusive): 10 of 31 days.
    share, days = prorated_fee(Decimal("31"), "month", date(2026, 1, 1), date(2026, 3, 10), 2026, 3)
    assert (share, days) == (Decimal("10"), 10)
    # The month after it ended costs nothing.
    assert prorated_fee(Decimal("31"), "month", date(2026, 1, 1), date(2026, 3, 10), 2026, 4) == (Decimal(0), 0)


def test_plan_starting_and_ending_inside_one_month() -> None:
    share, days = prorated_fee(Decimal("300"), "month", date(2026, 6, 11), date(2026, 6, 20), 2026, 6)
    assert (share, days) == (Decimal("100"), 10)


def test_yearly_plan_is_divided_by_twelve_then_prorated() -> None:
    assert monthly_fee(Decimal("240"), "year") == Decimal("20")
    share, days = prorated_fee(Decimal("240"), "year", date(2025, 7, 1), None, 2026, 2)
    assert (share, days) == (Decimal("20"), 28)
    # A yearly plan from 15 November: 16 of 30 days of a twelfth.
    share, days = prorated_fee(Decimal("360"), "year", date(2026, 11, 15), None, 2026, 11)
    assert days == 16
    assert share == Decimal("16")


def test_prorated_share_keeps_full_precision() -> None:
    share, _days = prorated_fee(Decimal("20"), "month", date(2026, 1, 1), date(2026, 1, 1), 2026, 1)
    assert share == Decimal(20) / Decimal(31)
    assert Money(share, "USD").quantize().amount == Decimal("0.65")


def test_days_outside_the_month_and_unknown_period() -> None:
    assert prorated_fee(Decimal("20"), "month", date(2026, 5, 1), None, 2026, 4) == (Decimal(0), 0)
    with pytest.raises(ValueError):
        prorated_fee(Decimal("20"), "week", date(2026, 5, 1), None, 2026, 5)


def test_leap_february() -> None:
    share, days = prorated_fee(Decimal("29"), "month", date(2028, 2, 15), None, 2028, 2)
    assert (share, days) == (Decimal("15"), 15)


def test_charges_per_month_over_a_period_count_only_days_inside_it() -> None:
    sub = _sub(5, amount="31", starts=date(2026, 1, 1))
    charges = subscription_charges(sub, date(2026, 1, 21), date(2026, 3, 10))
    assert [(c.year, c.month, c.active_days, c.days_in_month) for c in charges] == [
        (2026, 1, 11, 31),
        (2026, 2, 28, 28),
        (2026, 3, 10, 31),
    ]
    assert charges[0].amount == Money("11", "USD")
    assert charges[1].amount == Money("31", "USD")
    assert charges[2].amount == Money("10", "USD")
    assert all(c.subscription_id == 5 and c.user_id == 7 for c in charges)


def test_seat_charges_and_totals_keep_currencies_apart() -> None:
    subs = [
        _sub(1, amount="20", currency="USD"),
        _sub(2, amount="600", currency="THB", starts=date(2026, 4, 16)),
        _sub(3, amount="240", currency="EUR", period="year"),
        _sub(4, amount="10", currency="USD", ends=date(2026, 3, 31)),
    ]
    charges = seat_charges(subs, 2026, 4)
    assert {c.subscription_id for c in charges} == {1, 2, 3}
    totals = totals_by_currency(charges)
    assert totals == {"USD": Money("20", "USD"), "THB": Money("300", "THB"), "EUR": Money("20", "EUR")}


def test_months_between() -> None:
    assert list(months_between(date(2026, 11, 20), date(2027, 2, 1))) == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]
    assert list(months_between(date(2026, 3, 1), date(2026, 2, 1))) == []
