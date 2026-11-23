"""Billing account values, route matching, tool coverage and the project payer default (no database)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from specivo.core.ai_billing import (
    PROJECT_PAYER_DEFAULT_KEY,
    covers_tool,
    normalize_account_slug,
    normalize_route,
    normalize_route_pattern,
    payer_from_default,
    project_payer_default,
    route_matches,
)
from specivo.core.exceptions import AppError
from specivo.models.ai_billing_account import subscription_account, subscription_account_slug
from specivo.services.ai_billing_account_service import parse_account_tool_slugs, parse_route_patterns, parse_share

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("corp-gateway", "corp-gateway"),
        ("  Corp.Gateway_1 ", "corp.gateway_1"),
        ("a", "a"),
        ("x" * 64, "x" * 64),
        ("x" * 65, None),
        ("-lead", None),
        ("has space", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_normalize_account_slug(value: object, expected: str | None) -> None:
    assert normalize_account_slug(value) == expected


def test_normalize_route_keeps_case_and_trims() -> None:
    assert normalize_route("  LiteLLM-Corp ") == "LiteLLM-Corp"
    assert normalize_route("   ") is None
    assert normalize_route(None) is None
    assert normalize_route("r" * 150) == "r" * 100


@pytest.mark.parametrize(
    ("value", "expected"),
    [("LiteLLM-*", "litellm-*"), ("  corp ", "corp"), ("two words", None), ("", None), ("p" * 101, None)],
)
def test_normalize_route_pattern(value: str, expected: str | None) -> None:
    assert normalize_route_pattern(value) == expected


def test_route_matches_is_fnmatch_and_case_insensitive() -> None:
    patterns = ["litellm-*", "corp-proxy"]
    assert route_matches(patterns, "LiteLLM-EU")
    assert route_matches(patterns, "corp-proxy")
    assert not route_matches(patterns, "corp-proxy-2")
    assert not route_matches(patterns, None)
    assert not route_matches([], "litellm-eu")


def test_covers_tool_empty_list_covers_every_tool() -> None:
    assert covers_tool([], "codex")
    assert covers_tool(["codex"], "codex")
    assert not covers_tool(["codex"], "claude-code")
    assert not covers_tool(["codex"], None)


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, "none"),
        ({}, "none"),
        ({PROJECT_PAYER_DEFAULT_KEY: "employer"}, "employer"),
        ({PROJECT_PAYER_DEFAULT_KEY: "employee"}, "employee"),
        ({PROJECT_PAYER_DEFAULT_KEY: "none"}, "none"),
        ({PROJECT_PAYER_DEFAULT_KEY: "someone"}, "none"),
        ({PROJECT_PAYER_DEFAULT_KEY: True}, "none"),
    ],
)
def test_project_payer_default(settings: dict | None, expected: str) -> None:
    assert project_payer_default(settings) == expected


def test_payer_from_default() -> None:
    assert payer_from_default("employer") == "employer"
    assert payer_from_default("employee") == "employee"
    assert payer_from_default("none") == "unknown"


def test_parse_route_patterns_dedupes_in_order() -> None:
    assert parse_route_patterns("Corp-*, litellm  corp-*") == ["corp-*", "litellm"]
    assert parse_route_patterns(None) == []
    with pytest.raises(AppError) as exc:
        parse_route_patterns([f"p{i}" for i in range(21)])
    assert exc.value.field == "route_patterns"


def test_parse_account_tool_slugs() -> None:
    assert parse_account_tool_slugs("codex, Claude-Code codex") == ["claude-code", "codex"]
    with pytest.raises(AppError) as exc:
        parse_account_tool_slugs(["not a tool"])
    assert exc.value.code == "ai_billing_account_invalid"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("0.5", Decimal("0.5")), (Decimal("1"), Decimal("1")), (0, Decimal("0"))],
)
def test_parse_share(value: object, expected: Decimal | None) -> None:
    assert parse_share(value) == expected


@pytest.mark.parametrize("value", ["1.5", "-0.1", "0.12345", "abc", 0.5, True, "NaN"])
def test_parse_share_refuses(value: object) -> None:
    with pytest.raises(AppError) as exc:
        parse_share(value)
    assert exc.value.field == "reimburse_share"


def test_subscription_account() -> None:
    account = subscription_account(
        subscription_id=7, user_id=3, plan_key="claude-max", plan_name="Claude Max", created_by_id=3
    )
    assert account.slug == subscription_account_slug("claude-max", 7) == "claude-max-7"
    assert (account.kind, account.payer, account.owner_user_id) == ("subscription", "employee", 3)
    assert account.default_cost_type == "subscription"
    assert account.tool_slugs == [] and account.route_patterns == []
    assert account.reimburse_share is None and account.is_active
