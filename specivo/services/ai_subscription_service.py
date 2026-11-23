"""AI tools and personal AI subscriptions: the register, who may change it, coverage and seat cost.

- **Tools** (``ai_tools``): a lookup administrators edit. :meth:`AiToolService.ensure`
  creates a slug a reporter sends on first use, inactive, so third-party
  adapters need no server change.
- **Company seat plans**: subscriptions without a holder (``user_id`` ``NULL``),
  with a ``seat_count`` and dated members (``ai_subscription_members``), each
  the fee schedule of an instance billing account of kind ``subscription``
  paid by the employer. Administrators manage them; every change is audited,
  members included. A member's share of the fee is one seat
  (:func:`specivo.core.ai_subscriptions.member_charges`); a membership change
  resolves that person's usage of the days it touches again.
- **Subscriptions** (``ai_subscriptions``): one person's flat-rate plans.
  Administrators manage everyone's; people manage their own while the
  instance setting ``ai_subscriptions_self_service`` is on (the default). People
  see only their own, administrators see all; nobody else does, project
  managers included. Service accounts and system accounts hold none.
- **Billing account**: each subscription is the fee schedule of a personal
  billing account of kind ``subscription`` (``billing_account_id``), created
  with it (see :mod:`specivo.services.ai_billing_account_service`). Deleting
  the subscription leaves the account, which usage may point at.
- **Coverage** (:meth:`AiSubscriptionService.subscription_for`: the company
  plans a person is a member of first, then their own) and **seat cost**
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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import PROVIDER_RE, normalize_provider
from specivo.core.ai_subscriptions import (
    AUTO_TOOL_POSITION,
    BILLING_PERIODS,
    CUSTOM_PLAN_KEY,
    Coverage,
    PlanPreset,
    SeatCharge,
    get_preset,
    is_plan_key,
    member_charges,
    month_first,
    month_last,
    most_members_at_once,
    normalize_tool_slug,
    pick_covering,
    subscription_charges,
    utc_day,
    vacant_charges,
)
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.money import Money, MoneyError, check_minor_units, format_decimal_plain, to_decimal
from specivo.models.ai_billing_account import company_plan_account, subscription_account
from specivo.models.ai_subscription import AiSubscription, AiSubscriptionMember, AiTool
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
#: Most seats a company plan has.
MAX_SEATS = 100_000

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
    "seat_count",
)

#: Fields a membership change may carry.
MEMBER_FIELDS: tuple[str, ...] = ("starts_on", "ends_on")

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
        assert tool is not None  # noqa: S101
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
    #: Company seat plans only: how many seats the fee pays for.
    seat_count: object = None

    def as_changes(self) -> dict[str, Any]:
        return {
            "seat_count": self.seat_count,
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


def parse_amount(value: object, field_name: str = "amount", *, allow_zero: bool = False) -> Decimal:
    """A fee someone typed: a positive decimal string (zero too with *allow_zero*); floats refused."""
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
    if amount < 0 or (amount == 0 and not allow_zero):
        raise _invalid("The fee cannot be negative" if allow_zero else "The fee must be more than zero", field_name)
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


def parse_seat_count(value: object, field_name: str = "seat_count") -> int:
    """A company plan's seat count: a whole number from 1 to :data:`MAX_SEATS`."""
    if isinstance(value, bool):
        raise _invalid("Enter the number of seats", field_name)
    try:
        seats = int(str(value).strip()) if isinstance(value, str) else int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise _invalid("Enter the number of seats as a whole number", field_name) from None
    if isinstance(value, float) and value != seats:
        raise _invalid("Enter the number of seats as a whole number", field_name)
    if seats < 1 or seats > MAX_SEATS:
        raise _invalid(f"A plan has 1 to {MAX_SEATS} seats", field_name)
    return seats


def subscription_details(sub: AiSubscription, owner: User | None) -> dict[str, Any]:
    """Audit details of a subscription (never its notes); a company plan adds its ``seat_count``."""
    details = {
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
    if sub.user_id is None:
        details["seat_count"] = sub.seat_count
    return details


def member_details(sub: AiSubscription, member: AiSubscriptionMember, person: User | None) -> dict[str, Any]:
    """Audit details of a company plan membership."""
    return {
        "subscription_id": sub.id,
        "plan_name": sub.plan_name,
        "member_id": member.id,
        "target_user_id": member.user_id,
        "target_login": person.login if person is not None else None,
        "starts_on": member.starts_on.isoformat(),
        "ends_on": member.ends_on.isoformat() if member.ends_on else None,
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

    async def get_personal(self, session: AsyncSession, subscription_id: int) -> AiSubscription:
        """A person's subscription; 404 for a company seat plan."""
        sub = await self.get(session, subscription_id)
        if sub.user_id is None:
            raise NotFoundError("AI subscription not found")
        return sub

    async def get_company(self, session: AsyncSession, subscription_id: int) -> AiSubscription:
        """A company seat plan; 404 for a person's subscription."""
        sub = await self.get(session, subscription_id)
        if sub.user_id is not None:
            raise NotFoundError("Company AI plan not found")
        return sub

    async def list_subscriptions(
        self,
        session: AsyncSession,
        *,
        user_id: int | None = None,
        provider: str | None = None,
        active_on: date | None = None,
        company: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AiSubscription], int]:
        """Subscriptions, newest start first: people's, or the company seat plans with *company*.

        Callers decide who may list whose.
        """
        stmt = select(AiSubscription).where(
            AiSubscription.user_id.is_(None) if company else AiSubscription.user_id.is_not(None)
        )
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

    async def owners(self, session: AsyncSession, user_ids: Iterable[int | None]) -> dict[int, User]:
        ids = sorted({i for i in user_ids if i is not None})
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
        owner: User | None,
        actor: User,
        request: Request | None = None,
    ) -> AiSubscription:
        """Add a subscription for *owner*, or a company seat plan when *owner* is ``None``.

        A person's subscription is audited when *actor* is somebody else (an
        administrator). A company plan needs an administrator, a ``seat_count``
        and may cost nothing; it is always audited.
        """
        if owner is None:
            return await self._create_company(session, data, actor=actor, request=request)
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
        # Every subscription is the fee schedule of a personal billing account.
        account = subscription_account(
            subscription_id=sub.id,
            user_id=owner.id,
            plan_key=sub.plan_key,
            plan_name=sub.plan_name,
            created_by_id=actor.id,
        )
        session.add(account)
        await session.flush()
        sub.billing_account_id = account.id
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

    async def _create_company(
        self, session: AsyncSession, data: SubscriptionInput, *, actor: User, request: Request | None
    ) -> AiSubscription:
        self._require_admin(actor)
        values = await self._validated(session, data.as_changes(), current=None, company=True)
        sub = AiSubscription(user_id=None, created_by_id=actor.id, **values)
        session.add(sub)
        await session.flush()
        # A company plan is the fee schedule of an instance account the employer pays through.
        account = company_plan_account(
            subscription_id=sub.id, plan_key=sub.plan_key, plan_name=sub.plan_name, created_by_id=actor.id
        )
        session.add(account)
        await session.flush()
        sub.billing_account_id = account.id
        await session.flush()
        await self._audit(
            session, AuditEvent.AI_SUBSCRIPTION_CREATED, actor, request, subscription_details(sub, None), sub.id
        )
        return sub

    @staticmethod
    def _require_admin(actor: User) -> None:
        if not is_global_admin(actor):
            raise PermissionDeniedError("Only administrators manage company AI plans")

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

        Audited when *actor* is not the holder (always for a company plan).
        Fewer seats than members held at once on some day are refused.
        """
        company = sub.user_id is None
        if company:
            self._require_admin(actor)
        else:
            await self._require_can_edit(session, actor, sub.user_id)  # type: ignore[arg-type]
        unknown = set(changes) - set(SUBSCRIPTION_FIELDS)
        if unknown:
            raise _invalid(f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        values = await self._validated(session, dict(changes), current=sub, company=company)
        if "seat_count" in values and values["seat_count"] != sub.seat_count:
            held = most_members_at_once((m.starts_on, m.ends_on) for m in await self.list_members(session, sub))
            if values["seat_count"] < held:
                raise _invalid(
                    f"Up to {held} people hold a seat at once; the plan needs at least that many seats",
                    "seat_count",
                    "ai_subscription_seats_full",
                )
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
        """Delete a subscription (a company plan with its members). Audited when *actor* is not the holder."""
        if sub.user_id is None:
            self._require_admin(actor)
        else:
            await self._require_can_edit(session, actor, sub.user_id)
        owner = await session.get(User, sub.user_id) if sub.user_id is not None and actor.id != sub.user_id else None
        details = subscription_details(sub, owner)
        sub_id = sub.id
        await session.delete(sub)
        await session.flush()
        if actor.id != details["target_user_id"]:
            await self._audit(session, AuditEvent.AI_SUBSCRIPTION_DELETED, actor, request, details, sub_id)

    async def _validated(
        self,
        session: AsyncSession,
        changes: Mapping[str, Any],
        *,
        current: AiSubscription | None,
        company: bool = False,
    ) -> dict[str, Any]:
        """Column values for a new subscription (*current* ``None``) or the changed fields of one.

        A new subscription takes missing values from its preset. Every check
        that reads only the request runs on what changed; the period and
        the minor units are checked on the result. The fields are checked in
        a fixed order, so the first invalid one is the one reported.
        """
        out = _v_plan(changes, creating=current is None)
        req = _SubscriptionRequest(changes, current, get_preset(out.get("plan_key")) if current is None else None)
        out |= _v_provider(req)
        out |= _v_plan_name(req)
        out |= await _v_tools(session, req)
        out |= _v_amount(req, company=company)
        out |= await _v_currency(session, req)
        out |= _v_period(req)
        out |= _v_dates(req)
        out |= _v_notes(req)
        out |= _v_seats(req, company=company)
        _check_result(out, current)
        return out

    # -- company plan members ----------------------------------------------------

    async def list_members(self, session: AsyncSession, sub: AiSubscription) -> list[AiSubscriptionMember]:
        """The memberships of a company plan, earliest first."""
        stmt = (
            select(AiSubscriptionMember)
            .where(AiSubscriptionMember.subscription_id == sub.id)
            .order_by(AiSubscriptionMember.starts_on, AiSubscriptionMember.id)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def members_of(
        self, session: AsyncSession, subscription_ids: Iterable[int]
    ) -> dict[int, list[AiSubscriptionMember]]:
        """The memberships of each company plan in *subscription_ids* (one query)."""
        ids = sorted(set(subscription_ids))
        out: dict[int, list[AiSubscriptionMember]] = {i: [] for i in ids}
        if not ids:
            return out
        stmt = (
            select(AiSubscriptionMember)
            .where(AiSubscriptionMember.subscription_id.in_(ids))
            .order_by(AiSubscriptionMember.subscription_id, AiSubscriptionMember.starts_on, AiSubscriptionMember.id)
        )
        for member in (await session.execute(stmt)).scalars().all():
            out[member.subscription_id].append(member)
        return out

    async def get_member(self, session: AsyncSession, sub: AiSubscription, member_id: int) -> AiSubscriptionMember:
        member = await session.get(AiSubscriptionMember, member_id)
        if member is None or member.subscription_id != sub.id:
            raise NotFoundError("Member not found")
        return member

    async def add_member(
        self,
        session: AsyncSession,
        sub: AiSubscription,
        person: User,
        *,
        starts_on: date | None,
        ends_on: date | None = None,
        actor: User,
        request: Request | None = None,
    ) -> AiSubscriptionMember:
        """Give *person* a seat on the company plan *sub* from *starts_on* to *ends_on* (administrators).

        422 ``ai_subscription_member_invalid`` for a service or system account
        or bad days, ``ai_subscription_member_overlap`` when the person already
        holds a seat on some of those days, ``ai_subscription_seats_full`` when
        a day would have more members than seats. Audited; the person's usage
        of those days is resolved again.
        """
        self._require_company(sub)
        self._require_admin(actor)
        if not self.can_hold(person):
            raise _invalid(
                "Service accounts and system accounts hold no seats", "user", "ai_subscription_member_invalid"
            )
        start, end = _member_days(starts_on, ends_on)
        members = await self.list_members(session, sub)
        _check_members(sub, [*members, _Period(person.id, start, end)], new_user_id=person.id)
        member = AiSubscriptionMember(
            subscription_id=sub.id, user_id=person.id, starts_on=start, ends_on=end, created_by_id=actor.id
        )
        session.add(member)
        await session.flush()
        await self._audit(
            session,
            AuditEvent.AI_SUBSCRIPTION_MEMBER_ADDED,
            actor,
            request,
            member_details(sub, member, person),
            sub.id,
        )
        await self._rebill_member(session, person.id, start, actor=actor, request=request)
        return member

    async def update_member(
        self,
        session: AsyncSession,
        sub: AiSubscription,
        member: AiSubscriptionMember,
        changes: Mapping[str, Any],
        *,
        actor: User,
        request: Request | None = None,
    ) -> AiSubscriptionMember:
        """Change a membership's days (``starts_on``, ``ends_on``; ``ends_on`` ``None`` reopens it).

        Ending a membership keeps the months before it as they were. The same
        checks as :meth:`add_member`; audited, and the person's usage from the
        earliest day the change touches is resolved again.
        """
        self._require_company(sub)
        self._require_admin(actor)
        unknown = set(changes) - set(MEMBER_FIELDS)
        if unknown:
            raise _invalid(
                f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0], "ai_subscription_member_invalid"
            )
        start = changes.get("starts_on", member.starts_on)
        end = changes.get("ends_on", member.ends_on)
        start, end = _member_days(start, end)
        diff: dict[str, Any] = {}
        for key, value in (("starts_on", start), ("ends_on", end)):
            old = getattr(member, key)
            if old != value:
                diff[key] = {"old": _audit_value(old), "new": _audit_value(value)}
        if not diff:
            return member
        others = [m for m in await self.list_members(session, sub) if m.id != member.id]
        _check_members(sub, [*others, _Period(member.user_id, start, end)], new_user_id=member.user_id)
        if start != member.starts_on:
            first_touched = min(start, member.starts_on)
        else:
            # Only the last day moved: the days after the earlier of the two ends change.
            first_touched = min(d for d in (member.ends_on, end) if d is not None) + timedelta(days=1)
        member.starts_on, member.ends_on = start, end
        await session.flush()
        person = await session.get(User, member.user_id)
        await self._audit(
            session,
            AuditEvent.AI_SUBSCRIPTION_MEMBER_UPDATED,
            actor,
            request,
            {**member_details(sub, member, person), "changes": diff},
            sub.id,
        )
        await self._rebill_member(session, member.user_id, first_touched, actor=actor, request=request)
        return member

    async def remove_member(
        self,
        session: AsyncSession,
        sub: AiSubscription,
        member: AiSubscriptionMember,
        *,
        actor: User,
        request: Request | None = None,
    ) -> None:
        """Delete a membership entered by mistake: its days lose the seat (end it instead to keep the past).

        Audited; the person's usage of its days is resolved again.
        """
        self._require_company(sub)
        self._require_admin(actor)
        person = await session.get(User, member.user_id)
        details = member_details(sub, member, person)
        user_id, start = member.user_id, member.starts_on
        await session.delete(member)
        await session.flush()
        await self._audit(session, AuditEvent.AI_SUBSCRIPTION_MEMBER_REMOVED, actor, request, details, sub.id)
        await self._rebill_member(session, user_id, start, actor=actor, request=request)

    @staticmethod
    def _require_company(sub: AiSubscription) -> None:
        if sub.user_id is not None:
            raise _invalid("Only company plans have members", "subscription_id", "ai_subscription_member_invalid")

    @staticmethod
    async def _rebill_member(
        session: AsyncSession, user_id: int, from_day: date, *, actor: User, request: Request | None
    ) -> None:
        """Resolve again the usage of *user_id* from *from_day* on: coverage by the plan may have changed."""
        from specivo.services.ai_usage_service import AiUsageService

        await AiUsageService().rebill_for_member(session, user_id, from_day, actor_id=actor.id, request=request)

    # -- coverage and seat cost ---------------------------------------------------

    async def coverages(
        self,
        session: AsyncSession,
        user_id: int,
        tool_slug: str | None,
        provider: str | None,
        at: datetime | date,
    ) -> list[Coverage]:
        """Every plan that may cover the usage, in the order they are tried.

        The company seat plans *user_id* holds a seat on at *at* first, then
        their own subscriptions; within each, the rules of
        :func:`specivo.core.ai_subscriptions.pick_covering`. At most one of each.
        """
        day = utc_day(at)
        tool = normalize_tool_slug(tool_slug) if tool_slug else None
        default_provider: str | None = None
        if tool is not None:
            row = await session.get(AiTool, tool)
            default_provider = row.default_provider if row is not None else None
        running = (
            AiSubscription.starts_on <= day,
            or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= day),
        )
        held = select(AiSubscriptionMember.subscription_id).where(
            AiSubscriptionMember.user_id == user_id,
            AiSubscriptionMember.starts_on <= day,
            or_(AiSubscriptionMember.ends_on.is_(None), AiSubscriptionMember.ends_on >= day),
        )
        rows = (
            (
                await session.execute(
                    select(AiSubscription).where(
                        *running,
                        or_(
                            AiSubscription.user_id == user_id,
                            AiSubscription.user_id.is_(None) & AiSubscription.id.in_(held),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        normalized = normalize_provider(provider) if provider else None
        out: list[Coverage] = []
        for group in ([r for r in rows if r.user_id is None], [r for r in rows if r.user_id is not None]):
            found = pick_covering(
                group, tool_slug=tool, provider=normalized, at=day, tool_default_provider=default_provider
            )
            if found is not None:
                out.append(found)
        return out

    async def subscription_for(
        self,
        session: AsyncSession,
        user_id: int,
        tool_slug: str | None,
        provider: str | None,
        at: datetime | date,
    ) -> Coverage | None:
        """The plan that covers *user_id*'s usage of *tool_slug* / *provider* at *at*, or ``None``.

        *at* is an aware instant (a usage entry's ``priced_at``), matched by
        its UTC day, or a ``date`` taken as a UTC day. A company seat plan the
        person holds a seat on that day comes first, then their own plans; in
        each, a listed tool matches first, then a plan without tools of the
        usage's provider that the tool belongs to (see
        :mod:`specivo.core.ai_subscriptions`).
        """
        found = await self.coverages(session, user_id, tool_slug, provider, at)
        return found[0] if found else None

    async def company_plan_for(
        self, session: AsyncSession, account_id: int, user_id: int, at: datetime | date
    ) -> int | None:
        """The company plan whose account is *account_id* that *user_id* holds a seat on at *at*, if any."""
        day = utc_day(at)
        stmt = (
            select(AiSubscription.id)
            .join(AiSubscriptionMember, AiSubscriptionMember.subscription_id == AiSubscription.id)
            .where(
                AiSubscription.billing_account_id == account_id,
                AiSubscription.user_id.is_(None),
                AiSubscription.starts_on <= day,
                or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= day),
                AiSubscriptionMember.user_id == user_id,
                AiSubscriptionMember.starts_on <= day,
                or_(AiSubscriptionMember.ends_on.is_(None), AiSubscriptionMember.ends_on >= day),
            )
            .order_by(AiSubscription.starts_on.desc(), AiSubscription.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def seat_charges_between(
        self, session: AsyncSession, first: date, last: date, *, user_ids: Iterable[int] | None = None
    ) -> list[SeatCharge]:
        """Every subscription's fee share per month for the days from *first* to *last* (inclusive).

        A company plan gives one charge per member and month (one seat each)
        and, without *user_ids*, the fee of its vacant seats (``user_id``
        ``None``). With *user_ids*, only those people's plans and seats. For
        reports: amounts stay in each subscription's currency.
        """
        wanted = set(user_ids) if user_ids is not None else None
        stmt = select(AiSubscription).where(
            AiSubscription.starts_on <= last,
            or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= first),
        )
        if wanted is not None:
            held = select(AiSubscriptionMember.subscription_id).where(AiSubscriptionMember.user_id.in_(wanted))
            stmt = stmt.where(
                or_(AiSubscription.user_id.in_(wanted), AiSubscription.user_id.is_(None) & AiSubscription.id.in_(held))
            )
        subs = list((await session.execute(stmt.order_by(AiSubscription.id))).scalars().all())
        members = await self.members_of(session, (s.id for s in subs if s.user_id is None))
        charges: list[SeatCharge] = []
        for sub in subs:
            if sub.user_id is not None:
                charges.extend(subscription_charges(sub, first, last))
                continue
            seats = sub.seat_count or 1
            plan_members = members.get(sub.id, [])
            charges.extend(
                c
                for c in member_charges(sub, seats, plan_members, first, last)
                if wanted is None or c.user_id in wanted
            )
            if wanted is None:
                charges.extend(vacant_charges(sub, seats, plan_members, first, last))
        return charges

    async def seat_cost(self, session: AsyncSession, user_id: int, year: int, month: int) -> list[SeatCharge]:
        """*user_id*'s fee share of each subscription and company seat for the month *year*-*month*."""
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


# ---------------------------------------------------------------------------
# Validation of a subscription change
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SubscriptionRequest:
    """A requested change: the new values, the stored row (``None`` when creating) and the preset."""

    changes: Mapping[str, Any]
    current: AiSubscription | None
    preset: PlanPreset | None

    @property
    def creating(self) -> bool:
        return self.current is None

    def pick(self, name: str, preset_value: Any = None) -> Any:
        """The requested *name* (blank text counts as missing); a new subscription falls back to *preset_value*."""
        value = self.changes.get(name)
        if isinstance(value, str) and not value.strip():
            value = None
        if value is None and self.creating:
            return preset_value
        return value


def _is_plain_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _v_plan(changes: Mapping[str, Any], *, creating: bool) -> dict[str, Any]:
    plan_key = changes.get("plan_key")
    if plan_key is None and not creating:
        return {}
    key = str(plan_key or CUSTOM_PLAN_KEY).strip().lower()
    if not is_plan_key(key):
        raise _invalid("Choose a plan from the list, or custom", "plan_key")
    return {"plan_key": key}


def _v_provider(req: _SubscriptionRequest) -> dict[str, Any]:
    raw = req.pick("provider", req.preset.provider if req.preset else None)
    if raw is None and not req.creating:
        return {}
    provider = normalize_provider(str(raw)) if raw else None
    if provider is None or not PROVIDER_RE.match(provider):
        raise _invalid("Enter the provider, for example anthropic or openai", "provider")
    return {"provider": provider}


def _v_plan_name(req: _SubscriptionRequest) -> dict[str, Any]:
    raw = req.pick("plan_name", req.preset.name if req.preset else None)
    if raw is None and not req.creating:
        return {}
    name = str(raw or "").strip()
    if not name or len(name) > PLAN_NAME_MAX_LENGTH:
        raise _invalid(f"Enter a plan name of at most {PLAN_NAME_MAX_LENGTH} characters", "plan_name")
    return {"plan_name": name}


async def _v_tools(session: AsyncSession, req: _SubscriptionRequest) -> dict[str, Any]:
    if "tool_slugs" not in req.changes and not req.creating:
        return {}
    raw_tools = req.changes.get("tool_slugs")
    if raw_tools is None and req.creating and req.preset is not None:
        raw_tools = list(req.preset.tool_slugs)
    slugs = parse_tool_slugs(raw_tools)
    if slugs:
        known = set((await session.execute(select(AiTool.slug).where(AiTool.slug.in_(slugs)))).scalars().all())
        missing = [s for s in slugs if s not in known]
        if missing:
            raise _invalid(f"Unknown tools: {', '.join(missing)}", "tool_slugs", "ai_subscription_unknown_tool")
    return {"tool_slugs": slugs}


def _v_amount(req: _SubscriptionRequest, *, company: bool) -> dict[str, Any]:
    raw = req.pick("amount", req.preset.amount if req.preset else None)
    if raw is None and not req.creating:
        return {}
    if raw is None:
        raise _invalid("Enter the fee", "amount")
    # A company plan may cost nothing here (its fee is booked elsewhere); a person's plan costs something.
    return {"amount": parse_amount(raw, allow_zero=company)}


def _v_seats(req: _SubscriptionRequest, *, company: bool) -> dict[str, Any]:
    raw = req.changes.get("seat_count")
    if isinstance(raw, str) and not raw.strip():
        raw = None
    if not company:
        if raw is not None:
            raise _invalid("Only company plans have seats", "seat_count")
        return {}
    if raw is None:
        if req.creating:
            raise _invalid("Enter the number of seats", "seat_count")
        return {}
    return {"seat_count": parse_seat_count(raw)}


async def _v_currency(session: AsyncSession, req: _SubscriptionRequest) -> dict[str, Any]:
    raw = req.pick("currency", req.preset.currency if req.preset else None)
    if raw is None and not req.creating:
        return {}
    if not raw:
        raise _invalid("Choose a currency", "currency")
    if req.current is not None and str(raw).strip().upper() == req.current.currency_code:
        return {}
    currency = await _currencies.require_selectable(session, str(raw), field="currency")
    return {"currency_code": currency.code}


def _v_period(req: _SubscriptionRequest) -> dict[str, Any]:
    raw = req.pick("billing_period", req.preset.billing_period if req.preset else "month")
    if raw is None and not req.creating:
        return {}
    period = str(raw or "").strip().lower()
    if period not in BILLING_PERIODS:
        raise _invalid("The billing period is month or year", "billing_period")
    return {"billing_period": period}


def _v_dates(req: _SubscriptionRequest) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "starts_on" in req.changes or req.creating:
        starts_on = req.changes.get("starts_on")
        if not _is_plain_date(starts_on):
            raise _invalid("Enter the first day of the subscription", "starts_on")
        out["starts_on"] = starts_on
    if "ends_on" in req.changes:
        ends_on = req.changes.get("ends_on")
        if ends_on is not None and not _is_plain_date(ends_on):
            raise _invalid("Enter the last day of the subscription, or leave it empty", "ends_on")
        out["ends_on"] = ends_on
    return out


def _v_notes(req: _SubscriptionRequest) -> dict[str, Any]:
    if "notes" not in req.changes:
        return {}
    notes = (req.changes.get("notes") or "").strip()
    if len(notes) > NOTES_MAX_LENGTH:
        raise _invalid(f"Notes are at most {NOTES_MAX_LENGTH} characters", "notes")
    return {"notes": notes or None}


def _check_result(out: Mapping[str, Any], current: AiSubscription | None) -> None:
    """Checks on the row the change results in: the fee's minor units and the order of the two days."""
    final_amount: Decimal | None = out.get("amount", current.amount if current else None)
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


# ---------------------------------------------------------------------------
# Company plan members
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Period:
    """A membership being checked (not saved yet)."""

    user_id: int
    starts_on: date
    ends_on: date | None


class _PeriodLike(Protocol):
    user_id: int
    starts_on: date
    ends_on: date | None


def _member_days(starts_on: object, ends_on: object) -> tuple[date, date | None]:
    if not _is_plain_date(starts_on):
        raise _invalid("Enter the first day of the seat", "starts_on", "ai_subscription_member_invalid")
    if ends_on is not None and not _is_plain_date(ends_on):
        raise _invalid("Enter the last day of the seat, or leave it empty", "ends_on", "ai_subscription_member_invalid")
    assert isinstance(starts_on, date)  # noqa: S101
    if ends_on is not None and ends_on < starts_on:  # type: ignore[operator]
        raise _invalid("The last day cannot be before the first day", "ends_on", "ai_subscription_member_invalid")
    return starts_on, ends_on  # type: ignore[return-value]


def _overlap(a: _PeriodLike, b: _PeriodLike) -> bool:
    return (b.ends_on is None or a.starts_on <= b.ends_on) and (a.ends_on is None or b.starts_on <= a.ends_on)


def _check_members(sub: AiSubscription, periods: list[_PeriodLike], *, new_user_id: int) -> None:
    """Refuse a person holding two seats of one plan at once, and more members than seats on some day.

    The last of *periods* is the one being added or changed.
    """
    candidate = periods[-1]
    if any(p.user_id == new_user_id and _overlap(p, candidate) for p in periods[:-1]):
        raise _invalid(
            "This person already holds a seat on this plan on some of these days",
            "starts_on",
            "ai_subscription_member_overlap",
        )
    seats = sub.seat_count or 1
    if most_members_at_once((p.starts_on, p.ends_on) for p in periods) > seats:
        raise _invalid(
            f"Every one of the plan's {seats} seats is taken on some of these days",
            "starts_on",
            "ai_subscription_seats_full",
        )
