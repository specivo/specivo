"""Pure AI usage rules: the price instant, the cost rules per cost type and the sanity bounds.

Nothing here touches the database. :class:`specivo.services.ai_usage_service.AiUsageService`
resolves the price row and the covering subscription, then calls
:func:`compute_costs`.

**Price instant.** Every entry is priced at one instant in UTC
(:func:`priced_at_for`): its ``started_at``, otherwise 00:00 UTC of its
``usage_date`` (buckets are split at UTC midnight).

**Marginal cost by cost type** (what the usage cost on top of what is paid anyway):

========================  ==============================================  ==========================================
cost type                 marginal                                        API equivalent
========================  ==============================================  ==========================================
``api``                   the reported cost if given, else the API        computed from the price
                          equivalent
``subscription``          zero                                            computed
``local``                 zero, or the local price row's cost when an     from the local row's ``baseline_model`` if
                          administrator priced the local model            set, else none
``free``                  zero                                            computed
``unknown``               none                                            computed
========================  ==============================================  ==========================================

The provider's reported cost wins for ``api`` spend (``cost_basis`` ``reported``,
or ``manual`` for a cost a person typed); the price table then drives only the
API equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from specivo.core.ai_pricing import TOKEN_CLASSES, ModelPrice, PricedUsage, TokenCounts, price_usage
from specivo.core.money import Money

#: Largest token count one class of one entry may hold (a whole day of heavy cache reads fits).
MAX_TOKENS_PER_CLASS = 1_000_000_000_000
MAX_REQUEST_COUNT = 100_000_000
#: Usage dates accepted: from this day up to tomorrow (UTC), so clocks a little ahead still work.
EARLIEST_USAGE_DATE = date(2020, 1, 1)
FUTURE_TOLERANCE = timedelta(days=1)

COMMENTS_MAX_LENGTH = 10_000
IDEMPOTENCY_KEY_MAX_LENGTH = 128
CONTEXT_MAX_LENGTHS: dict[str, int] = {
    "external_session_id": 200,
    "git_branch": 255,
    "repo_hint": 255,
    "client_name": 100,
    "client_version": 50,
}


def utc_midnight(day: date) -> datetime:
    """00:00 UTC of *day*."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def priced_at_for(usage_date: date, started_at: datetime | None) -> datetime:
    """The instant an entry is priced at: ``started_at`` in UTC, else the start of its UTC-day bucket."""
    if started_at is not None:
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("started_at needs a time zone offset")
        return started_at.astimezone(UTC)
    return utc_midnight(usage_date)


def token_counts(values: dict[str, Any] | None) -> TokenCounts | None:
    """A long-context bucket as stored (``None`` or a ``{class: count}`` map) as :class:`TokenCounts`."""
    if not values:
        return None
    counts = TokenCounts.from_mapping({k: int(v) for k, v in values.items()})
    return None if counts.is_zero() else counts


@dataclass(frozen=True, slots=True)
class CostInputs:
    """What :func:`compute_costs` needs about one entry.

    ``price`` is the row resolved for the model at ``priced_at`` (``None``:
    unpriced); ``is_local_price`` says that row prices a local model, and
    ``baseline_price`` is its baseline model's price at the same instant (the
    "what would it have cost on the API" value). ``zero_currency`` is the
    currency a zero marginal is stated in when nothing else names one.
    """

    cost_type: str
    tokens: TokenCounts
    long_context: TokenCounts | None
    price: ModelPrice | None
    is_local_price: bool = False
    baseline_price: ModelPrice | None = None
    reported_cost: Money | None = None
    reported_by_person: bool = False
    subscription_currency: str | None = None
    zero_currency: str = "USD"


@dataclass(frozen=True, slots=True)
class Costs:
    """The cost snapshot stored on an entry."""

    marginal: Money | None
    api_equivalent: Money | None
    cost_basis: str


def _priced(inputs: CostInputs, price: ModelPrice | None) -> PricedUsage | None:
    if price is None:
        return None
    return price_usage(inputs.tokens, inputs.long_context, price)


def compute_costs(inputs: CostInputs) -> Costs:
    """The marginal cost, the API equivalent and the cost basis of one entry (see the module docstring)."""
    if inputs.is_local_price:
        local = _priced(inputs, inputs.price)
        equivalent = _priced(inputs, inputs.baseline_price)
    else:
        local = None
        equivalent = _priced(inputs, inputs.price)

    api_equivalent = equivalent.money if equivalent is not None else None
    if equivalent is not None:
        basis = equivalent.basis
    elif local is not None:
        basis = local.basis
    else:
        basis = "unpriced"

    currency = (
        (api_equivalent.currency if api_equivalent is not None else None)
        or (local.money.currency if local is not None else None)
        or inputs.subscription_currency
        or (inputs.reported_cost.currency if inputs.reported_cost is not None else None)
        or inputs.zero_currency
    )

    marginal: Money | None
    cost_type = inputs.cost_type
    if cost_type == "api":
        if inputs.reported_cost is not None:
            marginal = inputs.reported_cost
            basis = "manual" if inputs.reported_by_person else "reported"
        else:
            marginal = api_equivalent
    elif cost_type == "local":
        if local is not None:
            marginal = local.money
        else:
            marginal = Money.zero(currency)
    elif cost_type in ("subscription", "free"):
        marginal = Money.zero(inputs.subscription_currency or currency)
    else:
        marginal = None
    return Costs(marginal=marginal, api_equivalent=api_equivalent, cost_basis=basis)


def check_tokens(values: dict[str, Any] | None, field: str = "tokens") -> dict[str, int]:
    """Validate a ``{class: count}`` map: known classes, ints, 0..MAX; missing classes are 0.

    Raises ``ValueError`` with a message naming the class.
    """
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError(f"{field} must be an object of token counts")
    unknown = set(values) - set(TOKEN_CLASSES)
    if unknown:
        raise ValueError(f"Unknown token classes in {field}: {', '.join(sorted(unknown))}")
    result: dict[str, int] = {}
    for token_class in TOKEN_CLASSES:
        value = values.get(token_class, 0)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field}.{token_class} must be a whole number")
        if value < 0 or value > MAX_TOKENS_PER_CLASS:
            raise ValueError(f"{field}.{token_class} must be between 0 and {MAX_TOKENS_PER_CLASS}")
        result[token_class] = value
    return result
