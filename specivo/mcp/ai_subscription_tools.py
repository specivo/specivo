"""MCP tool implementation for personal AI subscriptions: ``specivo_ai_subscriptions``.

Like ``specivo.mcp.ai_usage_tools``: the function takes an explicit session and
user, delegates every rule to :class:`AiSubscriptionService` (who may change
whose subscriptions, the self-service setting, the audit of an administrator's
change to somebody else's) and returns text for the agent.

The tool acts for the person behind the key. Subscriptions belong to an
account, not to a project, so a project-limited key is refused, as on the REST
routes; a service account holds none, so it is refused too. Only ``presets``,
a lookup, is open to every caller.

``add`` takes several periods in one call (the invoices of one plan over
time) and is all or nothing: every period is tried, the failures are reported
together, and when any fails nothing is added.

Every result that shows subscriptions ends with **warnings** about the plan
families it touches: two periods of one family that overlap, or a gap between
them. A family is one person's subscriptions with the same provider and the
same listed tools (Claude Pro, Max 5x and Max 20x are one family; ChatGPT
Plus is another). Warnings never refuse anything: a gap is often real (a
cancelled month), an overlap sometimes too (an upgrade billed mid-period).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_subscriptions import PLAN_PRESETS, PlanPreset
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.core.money import Money, format_decimal_plain
from specivo.mcp.tools import _log_tool
from specivo.models.ai_subscription import AiSubscription, AiTool
from specivo.models.user import User
from specivo.services.ai_subscription_service import AiSubscriptionService, SubscriptionInput
from specivo.services.api_key_scope import is_global_admin, is_limited_key
from specivo.services.security_audit_service import AuditEvent

_service = AiSubscriptionService()

AI_SUBSCRIPTION_OPS = ("list", "presets", "add", "update", "end", "delete")
#: Most periods one ``add`` call takes.
MAX_PERIODS = 50
_MAX_LIMIT = 200
#: Fields ``update`` may change (the service's own names).
UPDATE_FIELDS = (
    "plan_key",
    "provider",
    "plan_name",
    "tool_slugs",
    "amount",
    "currency",
    "billing_period",
    "starts_on",
    "ends_on",
    "notes",
)


class SubscriptionPeriod(BaseModel):
    """One period of a subscription, as an invoice shows it."""

    model_config = ConfigDict(extra="forbid")

    plan_key: str | None = Field(
        None,
        description="A preset key from op='presets' (e.g. claude-max-5x), or 'custom'. Fields left out take "
        "the preset's values.",
    )
    provider: str | None = Field(None, description="Custom plan: the provider, e.g. anthropic or openai.")
    plan_name: str | None = Field(None, description="Custom plan: its name.")
    tool_slugs: list[str] | None = Field(
        None, description="Tools the plan covers; leave out to cover the tools of its provider."
    )
    amount: str | int | float | None = Field(
        None, description="What you pay per billing period, tax included, as a string such as '112.00'."
    )
    currency: str | None = Field(None, description="ISO currency code, e.g. USD or THB.")
    billing_period: str | None = Field(None, description="month (default) or year.")
    starts_on: str = Field(description="First day, YYYY-MM-DD (UTC day).")
    ends_on: str | None = Field(None, description="Last day, YYYY-MM-DD (included); leave out while it runs.")
    notes: str | None = Field(None, description="Free text, e.g. the invoice number.")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValidationError(message=f"{field} must be a date such as 2026-11-05", field=field) from None


def _amount(value: str | int | float | None) -> str | int | None:
    """The fee for the service: a JSON number is read as the decimal it was written as."""
    if isinstance(value, float):
        return repr(value)
    return value


def _period_input(period: SubscriptionPeriod) -> SubscriptionInput:
    return SubscriptionInput(
        plan_key=(period.plan_key or "custom"),
        provider=period.provider,
        plan_name=period.plan_name,
        tool_slugs=period.tool_slugs,
        amount=_amount(period.amount),
        currency=period.currency,
        billing_period=period.billing_period,
        starts_on=_parse_date(period.starts_on, "starts_on"),
        ends_on=_parse_date(period.ends_on, "ends_on"),
        notes=period.notes,
    )


def _period_label(index: int, period: SubscriptionPeriod) -> str:
    plan = period.plan_key or period.plan_name or "custom"
    return f"periods[{index}] ({plan} from {period.starts_on})"


# ---------------------------------------------------------------------------
# Who
# ---------------------------------------------------------------------------


def _require_person_key(user: User) -> None:
    """Refuse a project-limited key and a service or system account."""
    if is_limited_key():
        raise PermissionDeniedError(
            _(
                "AI subscriptions belong to your account, not to a project: "
                "connect with an unlimited personal API key to manage them."
            )
        )
    if not _service.can_hold(user):
        raise PermissionDeniedError(
            _(
                "AI subscriptions belong to people, not to service accounts: connect with your "
                "personal API key, or ask an administrator to record them."
            )
        )


async def _owner(session: AsyncSession, caller: User, login: str | None) -> User:
    """Whose subscriptions: the caller, or the person *login* names (administrators only)."""
    if login is None or not login.strip() or login.strip().lower() == caller.login.lower():
        return caller
    if not is_global_admin(caller):
        raise PermissionDeniedError(_("Only administrators manage other people's AI subscriptions."))
    owner = (
        await session.execute(select(User).where(func.lower(User.login) == login.strip().lower()))
    ).scalar_one_or_none()
    if owner is None:
        raise NotFoundError(f"No user with the login '{login.strip()}'")
    if not _service.can_hold(owner):
        raise AppError(
            code="ai_subscription_owner_invalid",
            message=f"{owner.login} is a service or system account; they hold no AI subscriptions",
            status_code=422,
            field="user",
        )
    return owner


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tools:
    """Every AI tool, for naming what a subscription covers."""

    rows: Sequence[AiTool]

    def names(self, slugs: Iterable[str]) -> list[str]:
        by_slug = {t.slug: t.name for t in self.rows}
        return [by_slug.get(s, s) for s in slugs]

    def of_provider(self, provider: str) -> list[str]:
        return [t.name for t in self.rows if t.default_provider == provider]


async def _tools(session: AsyncSession) -> _Tools:
    return _Tools(await _service.tools.list_tools(session, active_only=False))


def _covers(tools: _Tools, provider: str, tool_slugs: Sequence[str]) -> str:
    if tool_slugs:
        return ", ".join(tools.names(tool_slugs))
    names = tools.of_provider(provider)
    if not names:
        return f"the tools of {provider} (none known yet)"
    return f"{', '.join(names)} (the tools of {provider})"


def _money(amount: Any, currency: str) -> str:
    return f"{format_decimal_plain(Money(amount, currency).quantize().amount)} {currency}"


def _period(billing_period: str) -> str:
    return "a year" if billing_period == "year" else "a month"


def _dates(starts_on: date, ends_on: date | None) -> str:
    return f"{starts_on.isoformat()} to {ends_on.isoformat()}" if ends_on else f"from {starts_on.isoformat()}, open"


def _line(sub: AiSubscription, tools: _Tools) -> str:
    text = (
        f"#{sub.id} {sub.plan_name} [{sub.plan_key}] {sub.provider}, "
        f"{_money(sub.amount, sub.currency_code)} {_period(sub.billing_period)}, "
        f"{_dates(sub.starts_on, sub.ends_on)}; covers: {_covers(tools, sub.provider, sub.tool_slugs or [])}"
    )
    if sub.notes:
        text += f"; notes: {sub.notes}"
    return text


def _preset_line(preset: PlanPreset, tools: _Tools) -> str:
    return (
        f"{preset.key}: {preset.name}, {preset.provider}, {_money(preset.amount, preset.currency)} "
        f"{_period(preset.billing_period)}; covers: {_covers(tools, preset.provider, preset.tool_slugs)}"
    )


# ---------------------------------------------------------------------------
# Overlaps and gaps
# ---------------------------------------------------------------------------


def family_key(sub: AiSubscription) -> tuple[str, tuple[str, ...]]:
    """The plan family of *sub*: its provider and the tools it lists."""
    return sub.provider, tuple(sorted(sub.tool_slugs or []))


def _family_label(key: tuple[str, tuple[str, ...]]) -> str:
    provider, slugs = key
    return f"{provider} plans for {', '.join(slugs)}" if slugs else f"{provider} plans"


def _short(sub: AiSubscription) -> str:
    return f"#{sub.id} {sub.plan_name} ({_dates(sub.starts_on, sub.ends_on)})"


def period_warnings(
    subs: Iterable[AiSubscription], families: set[tuple[str, tuple[str, ...]]] | None = None
) -> list[str]:
    """Overlaps and gaps between the periods of each plan family (of *families* only, when given)."""
    grouped: dict[tuple[str, tuple[str, ...]], list[AiSubscription]] = defaultdict(list)
    for sub in subs:
        key = family_key(sub)
        if families is None or key in families:
            grouped[key].append(sub)
    warnings: list[str] = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda s: (s.starts_on, s.id))
        label = _family_label(key)
        # The period that reaches furthest so far; None as its end is "still running".
        reach = rows[0]
        for sub in rows[1:]:
            reach_end = reach.ends_on
            if reach_end is None or sub.starts_on <= reach_end:
                last = min((d for d in (reach_end, sub.ends_on) if d is not None), default=None)
                span = f"{sub.starts_on.isoformat()} to {last.isoformat()}" if last else f"from {sub.starts_on}, open"
                warnings.append(f"Overlap in {label}: {_short(reach)} and {_short(sub)} both run {span}.")
            elif sub.starts_on > reach_end + timedelta(days=1):
                first_gap, last_gap = reach_end + timedelta(days=1), sub.starts_on - timedelta(days=1)
                warnings.append(
                    f"Gap in {label}: none from {first_gap.isoformat()} to {last_gap.isoformat()}, "
                    f"between {_short(reach)} and {_short(sub)}."
                )
            if reach.ends_on is not None and (sub.ends_on is None or sub.ends_on > reach.ends_on):
                reach = sub
    return warnings


async def _all_of(session: AsyncSession, user_id: int) -> list[AiSubscription]:
    rows = await session.execute(
        select(AiSubscription).where(AiSubscription.user_id == user_id).order_by(AiSubscription.starts_on)
    )
    return list(rows.scalars().all())


async def _warnings_block(
    session: AsyncSession, user_id: int, families: set[tuple[str, tuple[str, ...]]] | None
) -> list[str]:
    warnings = period_warnings(await _all_of(session, user_id), families)
    if not warnings:
        return []
    return ["Warnings (nothing was refused because of them; check them against the invoices):"] + [
        f"  {w}" for w in warnings
    ]


def _whose(owner: User, caller: User) -> str:
    return "your" if owner.id == caller.id else f"{owner.login}'s"


# ---------------------------------------------------------------------------
# specivo_ai_subscriptions
# ---------------------------------------------------------------------------


async def _ai_subscriptions(
    session: AsyncSession,
    caller: User,
    op: str,
    *,
    user_login: str | None = None,
    periods: Sequence[SubscriptionPeriod] | None = None,
    subscription_id: int | None = None,
    changes: dict[str, Any] | None = None,
    ends_on: str | None = None,
    active_on: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """List, add, change, end or delete AI subscriptions; see ``specivo_ai_subscriptions``."""
    if op not in AI_SUBSCRIPTION_OPS:
        raise ValidationError(
            message=f"Invalid op '{op}'. Must be one of: {', '.join(AI_SUBSCRIPTION_OPS)}", field="op"
        )
    if op == "presets":
        return await _presets(session, caller)
    _require_person_key(caller)
    if op == "list":
        return await _list(session, caller, user_login, _parse_date(active_on, "active_on"), offset, limit)
    if op == "add":
        return await _add(session, caller, user_login, periods or [])

    if subscription_id is None:
        raise ValidationError(message=f"op='{op}' needs subscription_id", field="subscription_id")
    sub = await _service.get_visible(session, caller, subscription_id)
    owner = sub.user_id
    family = family_key(sub)
    if op == "delete":
        text = f"Deleted {_line(sub, await _tools(session))}."
        await _service.delete(session, sub, actor=caller)
        await _log_change(session, caller, op, [subscription_id], owner)
        lines = [text, *await _warnings_block(session, owner, {family})]
        return "\n".join(lines)

    if op == "end":
        values: dict[str, Any] = {"ends_on": _parse_date(ends_on, "ends_on") or datetime.now(UTC).date()}
    else:
        values = _update_values(changes or {})
    sub = await _service.update(session, sub, values, actor=caller)
    await session.flush()
    await session.refresh(sub)
    await _log_change(session, caller, op, [sub.id], owner, fields=sorted(values))
    verb = "Ended" if op == "end" else "Updated"
    lines = [f"{verb} {_line(sub, await _tools(session))}"]
    lines += await _warnings_block(session, owner, {family, family_key(sub)})
    return "\n".join(lines)


def _update_values(changes: dict[str, Any]) -> dict[str, Any]:
    """The service's changes from the tool's arguments: ``None`` leaves a field, ``''`` clears it."""
    values: dict[str, Any] = {}
    for name in UPDATE_FIELDS:
        if name not in changes or changes[name] is None:
            continue
        value = changes[name]
        if name in ("starts_on", "ends_on"):
            parsed = _parse_date(value, name)
            if parsed is None and name == "starts_on":
                raise ValidationError(message="starts_on cannot be cleared", field="starts_on")
            values[name] = parsed
        elif name == "amount":
            values[name] = _amount(value)
        else:
            values[name] = value
    if not values:
        raise ValidationError(
            message=f"op='update' needs at least one field to change: {', '.join(UPDATE_FIELDS)}", field="op"
        )
    return values


async def _log_change(
    session: AsyncSession,
    caller: User,
    op: str,
    ids: list[int],
    owner_id: int,
    *,
    fields: list[str] | None = None,
) -> None:
    owner = caller if owner_id == caller.id else await session.get(User, owner_id)
    details: dict[str, Any] = {
        "op": op,
        "subscription_ids": ids,
        "target_login": owner.login if owner is not None else None,
    }
    if fields:
        details["fields"] = fields
    await _log_tool(session, caller, AuditEvent.AI_SUBSCRIPTIONS_CHANGED, "ai_subscriptions", details)


async def _presets(session: AsyncSession, caller: User) -> str:
    tools = await _tools(session)
    await _log_tool(session, caller, AuditEvent.RESOURCE_ACCESS, "ai_subscriptions", {"op": "presets"})
    lines = [
        "Plan presets. Prices are the providers' US list prices and only hints: register what the invoice "
        "says, tax included, in its currency.",
        *(f"  {_preset_line(p, tools)}" for p in PLAN_PRESETS),
        "  custom: any other plan; give provider, plan_name, amount and currency.",
    ]
    return "\n".join(lines)


async def _list(
    session: AsyncSession,
    caller: User,
    user_login: str | None,
    active_on: date | None,
    offset: int,
    limit: int,
) -> str:
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValidationError(message=f"limit must be between 1 and {_MAX_LIMIT}", field="limit")
    if offset < 0:
        raise ValidationError(message="offset must not be negative", field="offset")
    owner = await _owner(session, caller, user_login)
    rows, total = await _service.list_subscriptions(
        session, user_id=owner.id, active_on=active_on, offset=offset, limit=limit
    )
    await _log_tool(
        session,
        caller,
        AuditEvent.RESOURCE_ACCESS,
        "ai_subscriptions",
        {"op": "list", "target_login": owner.login},
    )
    tools = await _tools(session)
    shown = f"{offset + 1}-{offset + len(rows)}" if rows else "none"
    scope = f" running on {active_on.isoformat()}" if active_on else ""
    lines = [f"AI subscriptions, {_whose(owner, caller)}{scope} ({total} total, showing {shown}), newest first:"]
    lines += [f"  {_line(r, tools)}" for r in rows] or ["  (none)"]
    if not await _service.can_edit_own(session, owner) and owner.id == caller.id:
        lines.append("Self-service is off on this instance: ask an administrator to change them.")
    lines += await _warnings_block(session, owner.id, None)
    return "\n".join(lines)


async def _add(session: AsyncSession, caller: User, user_login: str | None, periods: Sequence[Any]) -> str:
    if not periods:
        raise ValidationError(message="op='add' needs periods: one or more subscription periods", field="periods")
    if len(periods) > MAX_PERIODS:
        raise ValidationError(message=f"Add at most {MAX_PERIODS} periods in one call", field="periods")
    owner = await _owner(session, caller, user_login)
    created: list[AiSubscription] = []
    errors: list[str] = []
    for index, raw in enumerate(periods):
        try:
            period = raw if isinstance(raw, SubscriptionPeriod) else SubscriptionPeriod.model_validate(raw)
        except ValueError as exc:
            errors.append(f"periods[{index}]: {exc}")
            continue
        savepoint = await session.begin_nested()
        try:
            sub = await _service.create(session, _period_input(period), owner=owner, actor=caller)
            await session.flush()
        except AppError as exc:
            await savepoint.rollback()
            if exc.status_code == 403:
                # Self-service is off, or not allowed at all: the same for every period.
                raise
            errors.append(f"{_period_label(index, period)}: {exc.message}")
            continue
        await savepoint.commit()
        created.append(sub)
    if errors:
        raise ValidationError(
            message="Nothing was added. Fix these periods and send them all again:\n"
            + "\n".join(f"  {e}" for e in errors),
            field="periods",
        )
    for sub in created:
        await session.refresh(sub)
    await _log_change(session, caller, "add", [s.id for s in created], owner.id)
    tools = await _tools(session)
    lines = [f"Added {len(created)} subscription period(s) to {_whose(owner, caller)} AI subscriptions:"]
    lines += [f"  {_line(s, tools)}" for s in sorted(created, key=lambda s: (s.starts_on, s.id))]
    lines += await _warnings_block(session, owner.id, {family_key(s) for s in created})
    return "\n".join(lines)
