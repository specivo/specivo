"""AI tools and personal AI subscriptions: plan presets, coverage and seat-cost proration.

Pure functions and data, no database. The service
(:mod:`specivo.services.ai_subscription_service`) loads rows and calls these.

**Dates.** A subscription's ``starts_on`` and ``ends_on`` are calendar dates
read as **UTC days**, both inclusive. A subscription that starts on
2026-03-01 and ends on 2026-03-31 covers every instant in
``[2026-03-01T00:00:00Z, 2026-04-01T00:00:00Z)``; without an end it covers
everything from its start on. An instant is matched by its UTC date, the
same rule usage entries use for ``priced_at`` and their UTC-day buckets, so
a bucket is either wholly inside a subscription or wholly outside it.

**Coverage** (:func:`pick_covering`), used to default a usage entry's cost
type:

1. *Tool match*: a subscription that lists the tool in ``tool_slugs``
   covers that tool, whatever model provider the usage reports (Cursor Pro
   covers Claude models used in Cursor).
2. *Provider match*: a subscription with no tools listed covers the tools
   **of its provider**, the tools whose ``default_provider`` is the
   subscription's provider, when the usage reports that provider (Claude Pro
   covers Claude Code, ChatGPT Plus covers Codex). It never covers a tool of
   another vendor that happens to call the same models.

Within a step the subscription that started last wins, then the newest row.

**Seat cost** (:func:`prorated_fee`, :func:`seat_charges`): a monthly fee
counts in full for a month the subscription covers entirely, and by covered
days otherwise (``fee x covered days / days in the month``). A yearly fee
is spread over twelve months first. Amounts are exact ``Decimal`` values,
never rounded here.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from specivo.core.money import Money

#: Slug of an AI tool: lower case letters, digits and dashes, 2 to 50 characters.
TOOL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,49}$")
#: Key of a plan preset (or ``custom``).
PLAN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

CUSTOM_PLAN_KEY = "custom"
BILLING_PERIODS: tuple[str, ...] = ("month", "year")

#: Tools every install starts with: (slug, name, default provider, position).
#: A default provider is the model provider usage from the tool reports when it
#: names none; tools that route to several providers have none.
SEEDED_TOOLS: tuple[tuple[str, str, str | None, int], ...] = (
    ("claude-code", "Claude Code", "anthropic", 10),
    ("codex", "Codex", "openai", 20),
    ("gemini-cli", "Gemini CLI", "gemini", 30),
    ("cursor", "Cursor", None, 40),
    ("copilot", "GitHub Copilot", None, 50),
    ("opencode", "OpenCode", None, 60),
    ("other", "Other", None, 1000),
)

#: Position of a tool created on first use by a reporter.
AUTO_TOOL_POSITION = 900


def normalize_tool_slug(value: object) -> str | None:
    """A tool slug in lower case, or ``None`` when *value* is not a valid slug."""
    if not isinstance(value, str):
        return None
    slug = value.strip().lower()
    return slug if TOOL_SLUG_RE.match(slug) else None


# ---------------------------------------------------------------------------
# Plan presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanPreset:
    """A known personal plan with a suggested list price.

    The price is a hint for the form, in the provider's own currency, and is
    edited freely: providers change prices, and people pay in other
    currencies or with tax.
    """

    key: str
    name: str
    provider: str
    amount: Decimal
    currency: str
    billing_period: str = "month"
    tool_slugs: tuple[str, ...] = ()

    @property
    def money(self) -> Money:
        return Money(self.amount, self.currency)


#: Personal plans offered as presets, in display order. The prices are the
#: providers' published US list prices (as of 2026) and may be out of date.
PLAN_PRESETS: tuple[PlanPreset, ...] = (
    PlanPreset("claude-pro", "Claude Pro", "anthropic", Decimal("20"), "USD"),
    PlanPreset("claude-max-5x", "Claude Max 5x", "anthropic", Decimal("100"), "USD"),
    PlanPreset("claude-max-20x", "Claude Max 20x", "anthropic", Decimal("200"), "USD"),
    PlanPreset("chatgpt-plus", "ChatGPT Plus", "openai", Decimal("20"), "USD"),
    PlanPreset("chatgpt-pro", "ChatGPT Pro", "openai", Decimal("200"), "USD"),
    PlanPreset("cursor-pro", "Cursor Pro", "cursor", Decimal("20"), "USD", tool_slugs=("cursor",)),
    PlanPreset("copilot-pro", "GitHub Copilot Pro", "github", Decimal("10"), "USD", tool_slugs=("copilot",)),
    PlanPreset("copilot-pro-plus", "GitHub Copilot Pro+", "github", Decimal("39"), "USD", tool_slugs=("copilot",)),
    PlanPreset("copilot-max", "GitHub Copilot Max", "github", Decimal("100"), "USD", tool_slugs=("copilot",)),
    PlanPreset("google-ai-pro", "Google AI Pro", "gemini", Decimal("19.99"), "USD"),
    PlanPreset("google-ai-ultra-5x", "Google AI Ultra 5x", "gemini", Decimal("99.99"), "USD"),
    PlanPreset("google-ai-ultra-20x", "Google AI Ultra 20x", "gemini", Decimal("199.99"), "USD"),
    PlanPreset("supergrok", "SuperGrok", "xai", Decimal("30"), "USD"),
)

_PRESETS_BY_KEY: dict[str, PlanPreset] = {p.key: p for p in PLAN_PRESETS}


def get_preset(key: str | None) -> PlanPreset | None:
    """The preset *key*, or ``None`` (also for ``custom``)."""
    return _PRESETS_BY_KEY.get(key or "")


def is_plan_key(key: str) -> bool:
    """Whether *key* names a preset or is ``custom``."""
    return key == CUSTOM_PLAN_KEY or key in _PRESETS_BY_KEY


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def utc_day(at: datetime | date) -> date:
    """The UTC calendar day of *at*; a ``date`` is taken as a UTC day already.

    A naive ``datetime`` is refused: an instant needs an offset.
    """
    if isinstance(at, datetime):
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Give the instant with a time zone offset")
        return at.astimezone(UTC).date()
    return at


def period_start(starts_on: date) -> datetime:
    """The first instant a subscription starting on *starts_on* covers (00:00 UTC that day)."""
    return datetime(starts_on.year, starts_on.month, starts_on.day, tzinfo=UTC)


def period_end(ends_on: date | None) -> datetime | None:
    """The first instant **after** a subscription ending on *ends_on* (00:00 UTC the next day)."""
    if ends_on is None:
        return None
    return period_start(ends_on) + timedelta(days=1)


def covers_day(starts_on: date, ends_on: date | None, day: date) -> bool:
    """Whether a subscription from *starts_on* to *ends_on* (inclusive, open when ``None``) covers *day*."""
    return starts_on <= day and (ends_on is None or day <= ends_on)


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_first(year: int, month: int) -> date:
    return date(year, month, 1)


def month_last(year: int, month: int) -> date:
    return date(year, month, days_in_month(year, month))


def months_between(start: date, end: date) -> Iterator[tuple[int, int]]:
    """Every ``(year, month)`` from *start*'s month to *end*'s month, inclusive."""
    if end < start:
        return
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            year, month = year + 1, 1


def overlap_days(starts_on: date, ends_on: date | None, first: date, last: date) -> int:
    """Days of ``[first, last]`` (inclusive) a subscription from *starts_on* to *ends_on* covers."""
    lo = max(starts_on, first)
    hi = last if ends_on is None else min(ends_on, last)
    return (hi - lo).days + 1 if hi >= lo else 0


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class SubscriptionLike(Protocol):
    """What coverage and proration read from a subscription row."""

    id: int
    user_id: int
    provider: str
    tool_slugs: list[str]
    amount: Decimal
    currency_code: str
    billing_period: str
    starts_on: date
    ends_on: date | None


@dataclass(frozen=True, slots=True)
class Coverage:
    """A subscription that covers usage, and how it matched (``tool`` or ``provider``)."""

    subscription: SubscriptionLike
    matched_by: str


def _latest_first(rows: Iterable[SubscriptionLike]) -> list[SubscriptionLike]:
    return sorted(rows, key=lambda s: (s.starts_on, s.id), reverse=True)


def pick_covering(
    subscriptions: Iterable[SubscriptionLike],
    *,
    tool_slug: str | None,
    provider: str | None,
    at: datetime | date,
    tool_default_provider: str | None = None,
) -> Coverage | None:
    """The subscription that covers usage of *tool_slug* with *provider* at *at*, or ``None``.

    *subscriptions* are one user's. *tool_default_provider* is the tool's
    ``default_provider``: a subscription without tools covers only the tools
    of its own provider. A usage without a provider is taken to use the
    tool's default provider. See the module docstring for the rules.
    """
    day = utc_day(at)
    active = [s for s in subscriptions if covers_day(s.starts_on, s.ends_on, day)]
    if not active:
        return None
    if tool_slug:
        by_tool = [s for s in active if tool_slug in (s.tool_slugs or [])]
        if by_tool:
            return Coverage(_latest_first(by_tool)[0], "tool")
    effective_provider = provider or tool_default_provider
    if not effective_provider:
        return None
    by_provider = [
        s
        for s in active
        if not s.tool_slugs
        and s.provider == effective_provider
        and (tool_slug is None or tool_default_provider == s.provider)
    ]
    if by_provider:
        return Coverage(_latest_first(by_provider)[0], "provider")
    return None


# ---------------------------------------------------------------------------
# Seat cost
# ---------------------------------------------------------------------------


def monthly_fee(amount: Decimal, billing_period: str) -> Decimal:
    """The fee of one whole month: the amount, or a twelfth of a yearly amount."""
    if billing_period == "year":
        return amount / Decimal(12)
    if billing_period == "month":
        return amount
    raise ValueError(f"Unknown billing period {billing_period!r}")


def prorated_fee(
    amount: Decimal,
    billing_period: str,
    starts_on: date,
    ends_on: date | None,
    year: int,
    month: int,
    *,
    first: date | None = None,
    last: date | None = None,
) -> tuple[Decimal, int]:
    """The share of a fee for *year*-*month*, and the days it counts.

    ``fee x covered days / days in the month``, the fee being a twelfth of a
    yearly amount. *first* and *last* narrow the days counted to part of the
    month (a report period that starts or ends inside it); the divisor stays
    the whole month. One division, so ``20 x 31 / 31`` is exactly ``20``.
    """
    if billing_period not in BILLING_PERIODS:
        raise ValueError(f"Unknown billing period {billing_period!r}")
    month_start = month_first(year, month)
    month_end = month_last(year, month)
    lo = max(month_start, first) if first is not None else month_start
    hi = min(month_end, last) if last is not None else month_end
    if hi < lo:
        return Decimal(0), 0
    days = overlap_days(starts_on, ends_on, lo, hi)
    if days == 0:
        return Decimal(0), 0
    total_days = days_in_month(year, month)
    if days == total_days:
        return monthly_fee(amount, billing_period), days
    divisor = Decimal(total_days * (12 if billing_period == "year" else 1))
    return amount * Decimal(days) / divisor, days


@dataclass(frozen=True, slots=True)
class SeatCharge:
    """One subscription's fee share in one month."""

    subscription_id: int
    user_id: int
    year: int
    month: int
    active_days: int
    days_in_month: int
    amount: Money


def subscription_charges(
    subscription: SubscriptionLike,
    first: date,
    last: date,
) -> list[SeatCharge]:
    """*subscription*'s fee per month for the days from *first* to *last* (inclusive) it covers.

    One charge per month with at least one covered day; months at the edges
    of the period count only their days inside it.
    """
    charges: list[SeatCharge] = []
    for year, month in months_between(first, last):
        share, days = prorated_fee(
            subscription.amount,
            subscription.billing_period,
            subscription.starts_on,
            subscription.ends_on,
            year,
            month,
            first=first,
            last=last,
        )
        if days:
            charges.append(
                SeatCharge(
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                    year=year,
                    month=month,
                    active_days=days,
                    days_in_month=days_in_month(year, month),
                    amount=Money(share, subscription.currency_code),
                )
            )
    return charges


def seat_charges(subscriptions: Iterable[SubscriptionLike], year: int, month: int) -> list[SeatCharge]:
    """Every subscription's fee share for the whole month *year*-*month*."""
    first, last = month_first(year, month), month_last(year, month)
    charges: list[SeatCharge] = []
    for sub in subscriptions:
        charges.extend(subscription_charges(sub, first, last))
    return charges


def totals_by_currency(charges: Sequence[SeatCharge]) -> dict[str, Money]:
    """Charges summed per currency (never converted)."""
    totals: dict[str, Money] = {}
    for charge in charges:
        code = charge.amount.currency
        totals[code] = totals[code] + charge.amount if code in totals else charge.amount
    return totals


@dataclass(slots=True)
class PlainSubscription:
    """A subscription without a database row (tests, previews)."""

    id: int
    user_id: int
    provider: str
    amount: Decimal
    currency_code: str
    billing_period: str
    starts_on: date
    ends_on: date | None = None
    tool_slugs: list[str] = field(default_factory=list)
