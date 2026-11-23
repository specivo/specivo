"""Which field an invalid AI usage entry or AI subscription is refused on, and with what message.

Characterization tests for the validators of ``AiUsageService`` and
``AiSubscriptionService``: each case breaks one field of an otherwise valid
input and pins the 422 it gets.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.services.ai_subscription_service import AiSubscriptionService
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService

pytestmark = pytest.mark.service

DAY = date(2026, 3, 10)


def _usage(**changes: Any) -> AiUsageInput:
    base = AiUsageInput(tool="claude-code", model="claude-sonnet-4-5", usage_date=DAY, provider="anthropic")
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "source", "field", "message"),
    [
        ({}, "telepathy", "source", "Unknown source 'telepathy'"),
        ({"provider": "   "}, "api", "provider", "Enter a provider such as anthropic or openai"),
        ({"model": "m" * 201}, "api", "model", "Enter a model name of at most 200 characters"),
        ({"usage_date": datetime(2026, 3, 10, tzinfo=UTC)}, "api", "usage_date", "usage_date must be a date"),
        (
            {"started_at": datetime(2026, 3, 10, 12, tzinfo=UTC), "ended_at": datetime(2026, 3, 10, 11, tzinfo=UTC)},
            "api",
            "ended_at",
            "ended_at must not be before started_at",
        ),
        ({"request_count": True}, "api", "request_count", None),
        ({"request_count": -1}, "api", "request_count", None),
        ({"cost_type": "barter"}, "api", "cost_type", None),
        ({"attribution_method": "guess"}, "api", "attribution_method", "Unknown attribution method"),
        ({"idempotency_key": "has space"}, "api", "idempotency_key", None),
        ({"comments": "nul\x00byte"}, "api", "comments", "comments must not contain NUL characters"),
    ],
)
async def test_an_invalid_usage_field_is_refused(
    db_session: AsyncSession, changes: dict, source: str, field: str, message: str | None
) -> None:
    with pytest.raises(AppError) as exc_info:
        await AiUsageService().validate(db_session, _usage(**changes), source=source)
    assert exc_info.value.status_code == 422
    assert exc_info.value.field == field
    if message is not None:
        assert exc_info.value.message == message


async def test_the_first_invalid_usage_field_is_the_one_reported(db_session: AsyncSession) -> None:
    data = _usage(usage_date="2026-03-10", request_count=-1, attribution_method="guess")
    with pytest.raises(AppError) as exc_info:
        await AiUsageService().validate(db_session, data, source="api")
    assert exc_info.value.field == "usage_date"


async def test_an_empty_long_context_bucket_is_dropped(db_session: AsyncSession) -> None:
    values = await AiUsageService()._validated(
        db_session, _usage(tokens={"input": 10}, long_context={"input": 0}), source="api"
    )
    assert values["long_context"] is None
    assert values["tool_slug"] == "claude-code"
    assert values["attribution_method"] == "none"


def _subscription(**changes: Any) -> dict[str, Any]:
    return {
        "plan_key": "custom",
        "provider": "anthropic",
        "plan_name": "Team plan",
        "amount": "20",
        "currency": "USD",
        "billing_period": "month",
        "starts_on": DAY,
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"plan_key": "no-such-plan"}, "plan_key", "Choose a plan from the list, or custom"),
        ({"provider": "!!"}, "provider", "Enter the provider, for example anthropic or openai"),
        ({"plan_name": "p" * 101}, "plan_name", "Enter a plan name of at most 100 characters"),
        ({"amount": None}, "amount", "Enter the fee"),
        ({"currency": None}, "currency", "Choose a currency"),
        ({"billing_period": "week"}, "billing_period", "The billing period is month or year"),
        ({"starts_on": None}, "starts_on", "Enter the first day of the subscription"),
        ({"starts_on": datetime(2026, 3, 10, tzinfo=UTC)}, "starts_on", "Enter the first day of the subscription"),
        ({"ends_on": "2026-04-01"}, "ends_on", "Enter the last day of the subscription, or leave it empty"),
        ({"ends_on": DAY - timedelta(days=1)}, "ends_on", "The last day cannot be before the first day"),
        ({"notes": "n" * 2001}, "notes", "Notes are at most 2000 characters"),
        ({"amount": "20.001"}, "amount", None),
    ],
)
async def test_an_invalid_subscription_field_is_refused(
    db_session: AsyncSession, changes: dict, field: str, message: str | None
) -> None:
    with pytest.raises(AppError) as exc_info:
        await AiSubscriptionService()._validated(db_session, _subscription(**changes), current=None)
    assert exc_info.value.status_code == 422
    assert exc_info.value.field == field
    if message is not None:
        assert exc_info.value.message == message


async def test_a_new_subscription_takes_defaults_and_normalizes(db_session: AsyncSession) -> None:
    values = await AiSubscriptionService()._validated(
        db_session,
        {
            "plan_key": " CUSTOM ",
            "provider": "Anthropic",
            "plan_name": " Team ",
            "amount": "20",
            "currency": "usd",
            "billing_period": "  ",
            "starts_on": DAY,
            "notes": "  ",
        },
        current=None,
    )
    assert values == {
        "plan_key": "custom",
        "provider": "anthropic",
        "plan_name": "Team",
        "tool_slugs": [],
        "amount": Decimal("20"),
        "currency_code": "USD",
        "billing_period": "month",
        "starts_on": DAY,
        "notes": None,
    }
