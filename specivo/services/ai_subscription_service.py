"""AI tools and personal AI subscriptions: the register, who may change it, coverage and seat cost.

- **Tools** (``ai_tools``): a lookup administrators edit. :meth:`AiToolService.ensure`
  creates a slug a reporter sends on first use, inactive, so third-party
  adapters need no server change.
- **Subscriptions** (``ai_subscriptions``): one person's flat-rate plans.
  Administrators manage everyone's; people manage their own while the
  instance setting ``ai_subscriptions_self_service`` is on (the default). People
  see only their own, administrators see all; nobody else does, project
  managers included. Service accounts and system accounts hold none.
- **Coverage** (:meth:`AiSubscriptionService.subscription_for`) and **seat cost**
  (:meth:`AiSubscriptionService.seat_cost`) apply the rules in
  :mod:`specivo.core.ai_subscriptions`.

An administrator's change to another person's subscription is audited
(``ai_subscription_*``), as is the self-service switch. People's changes to
their own subscriptions are not.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import PROVIDER_RE, normalize_provider
from specivo.core.ai_subscriptions import (
    AUTO_TOOL_POSITION,
    BILLING_PERIODS,
    CUSTOM_PLAN_KEY,
    Coverage,
    SeatCharge,
    get_preset,
    is_plan_key,
    month_first,
    month_last,
    normalize_tool_slug,
    pick_covering,
    subscription_charges,
    utc_day,
)
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.money import Money, MoneyError, check_minor_units, format_decimal_plain, to_decimal
from specivo.models.ai_subscription import AiSubscription, AiTool
from specivo.models.user import User
from specivo.services.api_key_scope import is_global_admin
from specivo.services.currency_service import CurrencyService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService
from specivo.services.settings_service import SettingsService

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

SELF_SERVICE_SETTING_KEY = "ai_subscriptions_self_service"

PLAN_NAME_MAX_LENGTH = 100
TOOL_NAME_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 2000
MAX_TOOL_SLUGS = 20
#: Largest fee: the integer digits ``NUMERIC(20, 8)`` holds.
AMOUNT_MAX_INTEGER_DIGITS = 12

#: Fields a subscription change may carry.
SUBSCRIPTION_FIELDS: tuple[str, ...] = (
    "provider",
    "plan_key",
    "plan_name",
    "tool_slugs",
    "amount",
    "currency",
    "billing_period",
    "starts_on",
    "ends_on",
    "notes",
)

_audit = SecurityAuditService()
_settings = SettingsService()
_currencies = CurrencyService()


def _invalid(message: str, field_name: str, code: str = "ai_subscription_invalid") -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field_name)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class AiToolService:
    """The ``ai_tools`` lookup."""

    async def list_tools(self, session: AsyncSession, *, active_only: bool = True) -> list[AiTool]:
        """Tools in display order (position, then name)."""
        stmt = select(AiTool).order_by(AiTool.position, AiTool.name, AiTool.slug)
        if active_only:
            stmt = stmt.where(AiTool.is_active.is_(True))
        return list((await session.execute(stmt)).scalars().all())

    async def get(self, session: AsyncSession, slug: str) -> AiTool:
        normalized = normalize_tool_slug(slug)
        tool = await session.get(AiTool, normalized) if normalized else None
        if tool is None:
            raise NotFoundError(f"AI tool '{slug}' not found")
        return tool

    async def ensure(self, session: AsyncSession, slug: str) -> AiTool:
        """The tool *slug*, created inactive when it is new (for reporters).

        Safe under concurrency: the insert does nothing when another request
        created the slug first. An invalid slug is a 422 ``ai_tool_invalid``.
        """
        normalized = normalize_tool_slug(slug)
        if normalized is None:
            raise _invalid(
                "A tool slug is 2 to 50 lower case letters, digits and dashes, starting with a letter or digit",
                "tool",
                "ai_tool_invalid",
            )
        existing = await session.get(AiTool, normalized)
        if existing is not None:
            return existing
        await session.execute(
            pg_insert(AiTool)
            .values(slug=normalized, name=normalized, is_active=False, position=AUTO_TOOL_POSITION)
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        tool = await session.get(AiTool, normalized)
        assert tool is not None
        logger.info("Added AI tool %r on first use (inactive)", normalized)
        return tool

    async def create(
        self,
        session: AsyncSession,
        *,
        slug: str,
        name: str,
        default_provider: str | None = None,
        is_active: bool = True,
        position: int = 0,
    ) -> AiTool:
        """Add a tool (administrators). 409 ``ai_tool_exists`` when the slug is taken."""
        normalized = normalize_tool_slug(slug)
        if normalized is None:
            raise _invalid(
                "A tool slug is 2 to 50 lower case letters, digits and dashes, starting with a letter or digit",
                "slug",
                "ai_tool_invalid",
            )
        if await session.get(AiTool, normalized) is not None:
            raise AppError(code="ai_tool_exists", message=f"Tool '{normalized}' exists", status_code=409, field="slug")
        tool = AiTool(
            slug=normalized,
            name=self._name(name),
            default_provider=self._provider(default_provider),
            is_active=is_active,
            position=position,
        )
        session.add(tool)
        await session.flush()
        return tool

    async def update(self, session: AsyncSession, slug: str, changes: Mapping[str, Any]) -> AiTool:
        """Change a tool's name, default provider, active flag or position (administrators)."""
        tool = await self.get(session, slug)
        values: dict[str, Any] = {}
        if "name" in changes and changes["name"] is not None:
            values["name"] = self._name(changes["name"])
        if "default_provider" in changes:
            values["default_provider"] = self._provider(changes["default_provider"])
        if "is_active" in changes and changes["is_active"] is not None:
            values["is_active"] = bool(changes["is_active"])
        if "position" in changes and changes["position"] is not None:
            values["position"] = int(changes["position"])
        for key, value in values.items():
            setattr(tool, key, value)
        await session.flush()
        return tool

    @staticmethod
    def _name(value: object) -> str:
        name = str(value or "").strip()
        if not name or len(name) > TOOL_NAME_MAX_LENGTH:
            raise _invalid(f"Enter a name of at most {TOOL_NAME_MAX_LENGTH} characters", "name", "ai_tool_invalid")
        return name

    @staticmethod
    def _provider(value: object) -> str | None:
        provider = normalize_provider(str(value)) if value else None
        if provider is not None and not PROVIDER_RE.match(provider):
            raise _invalid("Not a valid provider name", "default_provider", "ai_tool_invalid")
        return provider


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubscriptionInput:
    """A subscription as someone entered it; ``None`` takes the preset's value where there is one."""

    plan_key: str = CUSTOM_PLAN_KEY
    provider: str | None = None
    plan_name: str | None = None
    tool_slugs: Iterable[str] | str | None = None
    amount: object = None
    currency: str | None = None
    billing_period: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    notes: str | None = None

    def as_changes(self) -> dict[str, Any]:
        return {
            "plan_key": self.plan_key,
            "provider": self.provider,
            "plan_name": self.plan_name,
            "tool_slugs": self.tool_slugs,
            "amount": self.amount,
            "currency": self.currency,
            "billing_period": self.billing_period,
            "starts_on": self.starts_on,
            "ends_on": self.ends_on,
            "notes": self.notes,
        }


def parse_amount(value: object, field_name: str = "amount") -> Decimal:
    """A fee someone typed: a positive decimal string; floats refused."""
    if isinstance(value, Decimal):
        text = format_decimal_plain(value)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
    else:
        raise _invalid("Enter the fee as a decimal number such as 20.00", field_name)
    try:
        amount = to_decimal(text)
    except (MoneyError, TypeError):
        raise _invalid("Enter the fee as a decimal number such as 20.00", field_name) from None
    if amount <= 0:
        raise _invalid("The fee must be more than zero", field_name)
    if amount.adjusted() >= AMOUNT_MAX_INTEGER_DIGITS:
        raise _invalid("The fee is too large", field_name)
    return amount


def parse_tool_slugs(value: Iterable[str] | str | None) -> list[str]:
    """Tool slugs from a list or a space or comma separated string: normalised, unique, sorted."""
    if value is None:
        return []
    items = value.replace(",", " ").split() if isinstance(value, str) else list(value)
    slugs: set[str] = set()
    for item in items:
        slug = normalize_tool_slug(item)
        if slug is None:
            raise _invalid(f"Not a tool: {str(item)[:60]!r}", "tool_slugs")
        slugs.add(slug)
    if len(slugs) > MAX_TOOL_SLUGS:
        raise _invalid(f"List at most {MAX_TOOL_SLUGS} tools", "tool_slugs")
    return sorted(slugs)


def subscription_details(sub: AiSubscription, owner: User | None) -> dict[str, Any]:
    """Audit details of a subscription (never its notes)."""
    return {
        "subscription_id": sub.id,
        "target_user_id": sub.user_id,
        "target_login": owner.login if owner is not None else None,
        "provider": sub.provider,
        "plan_key": sub.plan_key,
        "plan_name": sub.plan_name,
        "tool_slugs": list(sub.tool_slugs or []),
        "amount": format_decimal_plain(sub.fee.quantize().amount),
        "currency": sub.currency_code,
        "billing_period": sub.billing_period,
        "starts_on": sub.starts_on.isoformat(),
        "ends_on": sub.ends_on.isoformat() if sub.ends_on else None,
    }


def _audit_value(value: Any, currency: str | None = None) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Amounts at the currency's minor units, so "20" and "20.00000000" read the same.
        return format_decimal_plain(Money(value, currency).quantize().amount if currency else value)
    if isinstance(value, list):
        return list(value)
    return value


class AiSubscriptionService:
    """Reads and changes the subscription register."""

    tools = AiToolService()

    # -- self-service setting --------------------------------------------------

    async def self_service_enabled(self, session: AsyncSession) -> bool:
        """Whether people may edit their own subscriptions (default on)."""
        value = await _settings.get(session, SELF_SERVICE_SETTING_KEY)
        return value is None or value.strip().lower() not in ("false", "0", "off", "no")

    async def set_self_service(
        self, session: AsyncSession, enabled: bool, *, actor: User, request: Request | None = None
    ) -> bool:
        """Switch self-service on or off; audited when it changes."""
        changed = await self.self_service_enabled(session) != enabled
        await _settings.set_many(session, {SELF_SERVICE_SETTING_KEY: "true" if enabled else "false"})
        if changed:
            await self._audit(
                session, AuditEvent.AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED, actor, request, {"enabled": enabled}, None
            )
        return enabled

    # -- access ----------------------------------------------------------------

    @staticmethod
    def can_hold(user: User) -> bool:
        """Whether *user* may hold subscriptions: people, not service or system accounts."""
        return not user.is_service_account and not user.is_system_principal

    async def can_edit_own(self, session: AsyncSession, user: User) -> bool:
        """Whether *user* may change their own subscriptions now."""
        if not self.can_hold(user):
            return False
        return is_global_admin(user) or await self.self_service_enabled(session)

    async def _require_can_edit(self, session: AsyncSession, actor: User, owner_id: int) -> None:
        if is_global_admin(actor):
            return
        if actor.id != owner_id:
            raise NotFoundError("AI subscription not found")
        if not self.can_hold(actor):
            raise AppError(
                code="ai_subscription_owner_invalid",
                message="Service accounts do not hold AI subscriptions",
                status_code=422,
                field="user_id",
            )
        if not await self.self_service_enabled(session):
            raise PermissionDeniedError(
                "An administrator manages AI subscriptions on this instance; ask one to change yours"
            )

    # -- reads -----------------------------------------------------------------

    async def get(self, session: AsyncSession, subscription_id: int) -> AiSubscription:
        sub = await session.get(AiSubscription, subscription_id)
        if sub is None:
            raise NotFoundError("AI subscription not found")
        return sub

    async def get_visible(self, session: AsyncSession, viewer: User, subscription_id: int) -> AiSubscription:
        """The subscription if *viewer* holds it or is an administrator; otherwise 404."""
        sub = await self.get(session, subscription_id)
        if sub.user_id != viewer.id and not is_global_admin(viewer):
            raise NotFoundError("AI subscription not found")
        return sub

    async def get_own(self, session: AsyncSession, viewer: User, subscription_id: int) -> AiSubscription:
        """*viewer*'s own subscription; 404 for anybody else's, administrators included."""
        sub = await self.get(session, subscription_id)
        if sub.user_id != viewer.id:
            raise NotFoundError("AI subscription not found")
        return sub

    async def list_subscriptions(
        self,
        session: AsyncSession,
        *,
        user_id: int | None = None,
        provider: str | None = None,
        active_on: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AiSubscription], int]:
        """Subscriptions, newest start first. Callers decide who may list whose."""
        stmt = select(AiSubscription)
        if user_id is not None:
            stmt = stmt.where(AiSubscription.user_id == user_id)
        if provider:
            stmt = stmt.where(AiSubscription.provider == (normalize_provider(provider) or provider))
        if active_on is not None:
            stmt = stmt.where(
                AiSubscription.starts_on <= active_on,
                or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= active_on),
            )
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (
            (
                await session.execute(
                    stmt.order_by(AiSubscription.starts_on.desc(), AiSubscription.id.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def owners(self, session: AsyncSession, user_ids: Iterable[int]) -> dict[int, User]:
        ids = sorted(set(user_ids))
        if not ids:
            return {}
        rows = (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all()
        return {u.id: u for u in rows}

    # -- writes ----------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        data: SubscriptionInput,
        *,
        owner: User,
        actor: User,
        request: Request | None = None,
    ) -> AiSubscription:
        """Add a subscription for *owner*. Audited when *actor* is somebody else (an administrator)."""
        await self._require_can_edit(session, actor, owner.id)
        if not self.can_hold(owner):
            raise AppError(
                code="ai_subscription_owner_invalid",
                message="Service accounts and system accounts do not hold AI subscriptions",
                status_code=422,
                field="user_id",
            )
        values = await self._validated(session, data.as_changes(), current=None)
        sub = AiSubscription(user_id=owner.id, created_by_id=actor.id, **values)
        session.add(sub)
        await session.flush()
        if actor.id != owner.id:
            await self._audit(
                session,
                AuditEvent.AI_SUBSCRIPTION_CREATED,
                actor,
                request,
                subscription_details(sub, owner),
                sub.id,
            )
        return sub

    async def update(
        self,
        session: AsyncSession,
        sub: AiSubscription,
        changes: Mapping[str, Any],
        *,
        actor: User,
        request: Request | None = None,
    ) -> AiSubscription:
        """Change the fields in *changes* (keys of :data:`SUBSCRIPTION_FIELDS`).

        Audited when *actor* is not the holder.
        """
        await self._require_can_edit(session, actor, sub.user_id)
        unknown = set(changes) - set(SUBSCRIPTION_FIELDS)
        if unknown:
            raise _invalid(f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        values = await self._validated(session, dict(changes), current=sub)
        diff: dict[str, Any] = {}
        for key, value in values.items():
            old = getattr(sub, key)
            if old == value or (key == "tool_slugs" and list(old or []) == list(value)):
                continue
            if key == "notes":
                diff["notes"] = {"changed": True}
            else:
                diff[key] = {
                    "old": _audit_value(old, sub.currency_code),
                    "new": _audit_value(value, values.get("currency_code", sub.currency_code)),
                }
            setattr(sub, key, value)
        if not diff:
            return sub
        await session.flush()
        if actor.id != sub.user_id:
            owner = await session.get(User, sub.user_id)
            await self._audit(
                session,
                AuditEvent.AI_SUBSCRIPTION_UPDATED,
                actor,
                request,
                {**subscription_details(sub, owner), "changes": diff},
                sub.id,
            )
        return sub

    async def delete(
        self, session: AsyncSession, sub: AiSubscription, *, actor: User, request: Request | None = None
    ) -> None:
        """Delete a subscription. Audited when *actor* is not the holder."""
        await self._require_can_edit(session, actor, sub.user_id)
        owner = await session.get(User, sub.user_id) if actor.id != sub.user_id else None
        details = subscription_details(sub, owner)
        sub_id = sub.id
        await session.delete(sub)
        await session.flush()
        if actor.id != details["target_user_id"]:
            await self._audit(session, AuditEvent.AI_SUBSCRIPTION_DELETED, actor, request, details, sub_id)

    async def _validated(
        self, session: AsyncSession, changes: Mapping[str, Any], *, current: AiSubscription | None
    ) -> dict[str, Any]:
        """Column values for a new subscription (*current* ``None``) or the changed fields of one.

        A new subscription takes missing values from its preset. Every check
        that reads only the request runs on what changed; the period and
        the minor units are checked on the result.
        """
        out: dict[str, Any] = {}
        creating = current is None

        plan_key = changes.get("plan_key")
        if plan_key is not None or creating:
            key = str(plan_key or CUSTOM_PLAN_KEY).strip().lower()
            if not is_plan_key(key):
                raise _invalid("Choose a plan from the list, or custom", "plan_key")
            out["plan_key"] = key
        preset = get_preset(out.get("plan_key")) if creating else None

        def pick(name: str, preset_value: Any = None) -> Any:
            value = changes.get(name)
            if isinstance(value, str) and not value.strip() and name not in ("notes",):
                value = None
            if value is None and creating:
                return preset_value
            return value

        provider_raw = pick("provider", preset.provider if preset else None)
        if provider_raw is not None or creating:
            provider = normalize_provider(str(provider_raw)) if provider_raw else None
            if provider is None or not PROVIDER_RE.match(provider):
                raise _invalid("Enter the provider, for example anthropic or openai", "provider")
            out["provider"] = provider

        name_raw = pick("plan_name", preset.name if preset else None)
        if name_raw is not None or creating:
            name = str(name_raw or "").strip()
            if not name or len(name) > PLAN_NAME_MAX_LENGTH:
                raise _invalid(f"Enter a plan name of at most {PLAN_NAME_MAX_LENGTH} characters", "plan_name")
            out["plan_name"] = name

        if "tool_slugs" in changes or creating:
            raw_tools = changes.get("tool_slugs")
            if raw_tools is None and creating and preset is not None:
                raw_tools = list(preset.tool_slugs)
            slugs = parse_tool_slugs(raw_tools)
            if slugs:
                known = set((await session.execute(select(AiTool.slug).where(AiTool.slug.in_(slugs)))).scalars().all())
                missing = [s for s in slugs if s not in known]
                if missing:
                    raise _invalid(f"Unknown tools: {', '.join(missing)}", "tool_slugs", "ai_subscription_unknown_tool")
            out["tool_slugs"] = slugs

        amount_raw = pick("amount", preset.amount if preset else None)
        if amount_raw is not None or creating:
            if amount_raw is None:
                raise _invalid("Enter the fee", "amount")
            out["amount"] = parse_amount(amount_raw)

        currency_raw = pick("currency", preset.currency if preset else None)
        if currency_raw is not None or creating:
            if not currency_raw:
                raise _invalid("Choose a currency", "currency")
            changed_currency = current is None or str(currency_raw).strip().upper() != current.currency_code
            if changed_currency:
                currency = await _currencies.require_selectable(session, str(currency_raw), field="currency")
                out["currency_code"] = currency.code

        period_raw = pick("billing_period", preset.billing_period if preset else "month")
        if period_raw is not None or creating:
            period = str(period_raw or "").strip().lower()
            if period not in BILLING_PERIODS:
                raise _invalid("The billing period is month or year", "billing_period")
            out["billing_period"] = period

        if "starts_on" in changes or creating:
            starts_on = changes.get("starts_on")
            if not isinstance(starts_on, date) or isinstance(starts_on, datetime):
                raise _invalid("Enter the first day of the subscription", "starts_on")
            out["starts_on"] = starts_on
        if "ends_on" in changes:
            ends_on = changes.get("ends_on")
            if ends_on is not None and (not isinstance(ends_on, date) or isinstance(ends_on, datetime)):
                raise _invalid("Enter the last day of the subscription, or leave it empty", "ends_on")
            out["ends_on"] = ends_on

        if "notes" in changes:
            notes = (changes.get("notes") or "").strip()
            if len(notes) > NOTES_MAX_LENGTH:
                raise _invalid(f"Notes are at most {NOTES_MAX_LENGTH} characters", "notes")
            out["notes"] = notes or None

        # Checks on the resulting row.
        final_amount: Decimal = out.get("amount", current.amount if current else None)  # type: ignore[assignment]
        final_currency: str = out.get("currency_code", current.currency_code if current else "")
        if ("amount" in out or "currency_code" in out) and final_amount is not None and final_currency:
            try:
                check_minor_units(final_amount, final_currency)
            except MoneyError as exc:
                raise _invalid(str(exc), "amount") from None
        final_start: date | None = out.get("starts_on", current.starts_on if current else None)
        final_end: date | None = out.get("ends_on", current.ends_on if current else None)
        if final_start is not None and final_end is not None and final_end < final_start:
            raise _invalid("The last day cannot be before the first day", "ends_on")
        return out

    # -- coverage and seat cost ---------------------------------------------------

    async def subscription_for(
        self,
        session: AsyncSession,
        user_id: int,
        tool_slug: str | None,
        provider: str | None,
        at: datetime | date,
    ) -> Coverage | None:
        """The subscription of *user_id* that covers *tool_slug* / *provider* at *at*, or ``None``.

        *at* is an aware instant (a usage entry's ``priced_at``), matched by
        its UTC day, or a ``date`` taken as a UTC day. A listed tool matches
        first, then a subscription without tools of the usage's provider
        that the tool belongs to (see :mod:`specivo.core.ai_subscriptions`).
        """
        day = utc_day(at)
        tool = normalize_tool_slug(tool_slug) if tool_slug else None
        default_provider: str | None = None
        if tool is not None:
            row = await session.get(AiTool, tool)
            default_provider = row.default_provider if row is not None else None
        rows = (
            (
                await session.execute(
                    select(AiSubscription).where(
                        AiSubscription.user_id == user_id,
                        AiSubscription.starts_on <= day,
                        or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= day),
                    )
                )
            )
            .scalars()
            .all()
        )
        return pick_covering(
            rows,
            tool_slug=tool,
            provider=normalize_provider(provider) if provider else None,
            at=day,
            tool_default_provider=default_provider,
        )

    async def seat_charges_between(
        self, session: AsyncSession, first: date, last: date, *, user_ids: Iterable[int] | None = None
    ) -> list[SeatCharge]:
        """Every subscription's fee share per month for the days from *first* to *last* (inclusive).

        Optionally only the subscriptions of *user_ids*. For reports: amounts
        stay in each subscription's currency.
        """
        stmt = select(AiSubscription).where(
            AiSubscription.starts_on <= last,
            or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= first),
        )
        if user_ids is not None:
            stmt = stmt.where(AiSubscription.user_id.in_(list(user_ids)))
        charges: list[SeatCharge] = []
        for sub in (await session.execute(stmt.order_by(AiSubscription.id))).scalars().all():
            charges.extend(subscription_charges(sub, first, last))
        return charges

    async def seat_cost(self, session: AsyncSession, user_id: int, year: int, month: int) -> list[SeatCharge]:
        """*user_id*'s fee share of each subscription for the month *year*-*month*."""
        return await self.seat_charges_between(
            session, month_first(year, month), month_last(year, month), user_ids=[user_id]
        )

    # -- helpers ---------------------------------------------------------------

    async def _audit(
        self,
        session: AsyncSession,
        event: AuditEvent,
        actor: User | None,
        request: Request | None,
        details: dict[str, Any],
        resource_id: int | None,
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_ai_subscription_change(
                session,
                event,
                user_id=actor.id if actor is not None else None,
                details=details,
                resource_id=resource_id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)
