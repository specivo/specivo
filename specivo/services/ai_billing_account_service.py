"""AI billing accounts: the register, who may change it, lookups for ingest, and the project payer default.

- **Instance accounts** (no owner: corporate gateways, company plans) are
  managed by administrators and visible to every person, so they can name one
  in their reporter's configuration.
- **Personal accounts** are managed by their owner while the instance setting
  ``ai_subscriptions_self_service`` is on (the same switch as subscriptions),
  and by administrators. Only the owner and administrators see them. Service
  accounts and system accounts own none.
- Accounts are deactivated rather than deleted: usage entries keep pointing
  at them.
- Every subscription is the fee schedule of a personal account of kind
  ``subscription``; :class:`AiSubscriptionService` creates it with the
  subscription. A company seat plan's is an instance account of that kind,
  paid by the employer, and stays so.

Creating an account with route patterns, or changing an account's payer,
patterns, tools, default cost type or state, resolves the usage entries it may
affect again and re-prices them
(:meth:`specivo.services.ai_usage_service.AiUsageService.rebill_for_account`),
in the same transaction.

Changes to instance accounts, and an administrator's changes to somebody
else's personal account, are audited (``ai_billing_account_created`` /
``_updated`` / ``_deactivated``); a person's changes to their own accounts are
not, as for subscriptions. A changed project payer default is audited too
(``ai_usage_payer_default_changed``) and re-resolves the project's entries
(:meth:`AiBillingAccountService.change_project_payer_default`).

Lookups for ingest (:meth:`AiBillingAccountService.find_by_slug`,
:meth:`~AiBillingAccountService.instance_accounts`,
:meth:`~AiBillingAccountService.personal_accounts`) return active accounts
only; route and tool matching is :func:`specivo.core.ai_billing.route_matches`
and :func:`~specivo.core.ai_billing.covers_tool`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import (
    ACCOUNT_DEFAULT_COST_TYPES,
    ACCOUNT_PAYERS,
    BILLING_ACCOUNT_KINDS,
    MAX_ROUTE_PATTERNS,
    PAYER_DEFAULT_NONE,
    PAYER_EMPLOYEE,
    PAYER_EMPLOYER,
    PROJECT_PAYER_DEFAULT_KEY,
    PROJECT_PAYER_DEFAULTS,
    normalize_account_slug,
    normalize_route_pattern,
    project_payer_default,
)
from specivo.core.ai_subscriptions import normalize_tool_slug
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_subscription import AiSubscription
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_subscription_service import AiSubscriptionService
from specivo.services.api_key_scope import is_global_admin
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

NAME_MAX_LENGTH = 100
MAX_TOOL_SLUGS = 20
#: Decimal places a reimbursement share keeps (``NUMERIC(5, 4)``).
SHARE_PLACES = 4

#: Fields an account change may carry.
ACCOUNT_FIELDS: tuple[str, ...] = (
    "slug",
    "name",
    "kind",
    "payer",
    "reimburse_share",
    "route_patterns",
    "tool_slugs",
    "default_cost_type",
    "is_active",
)

_subscriptions = AiSubscriptionService()
_audit = SecurityAuditService()

#: ``owner`` of an instance account in audit details and listings.
INSTANCE_OWNER = "instance"

#: Fields whose change makes usage entries resolve to another account, payer or cost type.
RESOLUTION_FIELDS: tuple[str, ...] = ("payer", "route_patterns", "tool_slugs", "default_cost_type", "is_active")


def _invalid(message: str, field_name: str, code: str = "ai_billing_account_invalid") -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field_name)


@dataclass(frozen=True, slots=True)
class BillingAccountInput:
    """A billing account as someone entered it."""

    slug: str
    name: str
    kind: str
    payer: str
    reimburse_share: object = None
    route_patterns: Iterable[str] | str | None = None
    tool_slugs: Iterable[str] | str | None = None
    default_cost_type: str | None = None
    is_active: bool = True

    def as_changes(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "kind": self.kind,
            "payer": self.payer,
            "reimburse_share": self.reimburse_share,
            "route_patterns": self.route_patterns,
            "tool_slugs": self.tool_slugs,
            "default_cost_type": self.default_cost_type,
            "is_active": self.is_active,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _items(value: Iterable[str] | str | None) -> list[object]:
    if value is None:
        return []
    return list(value.replace(",", " ").split()) if isinstance(value, str) else list(value)


def parse_route_patterns(value: Iterable[str] | str | None) -> list[str]:
    """Route patterns from a list or a space or comma separated string: lower case, unique, in order."""
    patterns: list[str] = []
    for item in _items(value):
        pattern = normalize_route_pattern(item)
        if pattern is None:
            raise _invalid(f"Not a route pattern: {str(item)[:60]!r}", "route_patterns")
        if pattern not in patterns:
            patterns.append(pattern)
    if len(patterns) > MAX_ROUTE_PATTERNS:
        raise _invalid(f"List at most {MAX_ROUTE_PATTERNS} route patterns", "route_patterns")
    return patterns


def parse_account_tool_slugs(value: Iterable[str] | str | None) -> list[str]:
    """Tool slugs an account covers: normalised, unique, sorted (empty: every tool)."""
    slugs: set[str] = set()
    for item in _items(value):
        slug = normalize_tool_slug(item)
        if slug is None:
            raise _invalid(f"Not a tool: {str(item)[:60]!r}", "tool_slugs")
        slugs.add(slug)
    if len(slugs) > MAX_TOOL_SLUGS:
        raise _invalid(f"List at most {MAX_TOOL_SLUGS} tools", "tool_slugs")
    return sorted(slugs)


def parse_share(value: object) -> Decimal | None:
    """A reimbursement share from 0 to 1 with at most four places; blank is ``None``; floats refused."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise _invalid("Enter the share as a decimal number from 0 to 1, such as 0.5", "reimburse_share")
    try:
        share = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation:
        raise _invalid("Enter the share as a decimal number from 0 to 1, such as 0.5", "reimburse_share") from None
    if not share.is_finite() or share < 0 or share > 1:
        raise _invalid("The share is a number from 0 to 1", "reimburse_share")
    if share != share.quantize(Decimal(1).scaleb(-SHARE_PLACES)):
        raise _invalid(f"Use at most {SHARE_PLACES} decimal places", "reimburse_share")
    return share


def parse_slug(value: object) -> str:
    slug = normalize_account_slug(value)
    if slug is None:
        raise _invalid(
            "A slug is 1 to 64 lower case letters, digits, dots, dashes and underscores, "
            "starting with a letter or digit",
            "slug",
        )
    return slug


def parse_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or len(name) > NAME_MAX_LENGTH:
        raise _invalid(f"Enter a name of at most {NAME_MAX_LENGTH} characters", "name")
    return name


def _choice(field_name: str, label: str, choices: tuple[str, ...]) -> Callable[[object], str]:
    def parse(value: object) -> str:
        if not isinstance(value, str) or value not in choices:
            raise _invalid(f"The {label} is one of: {', '.join(choices)}", field_name)
        return value

    return parse


def parse_default_cost_type(value: object) -> str | None:
    """A default cost type, or ``None`` (blank)."""
    if not value:
        return None
    return _choice("default_cost_type", "default cost type", ACCOUNT_DEFAULT_COST_TYPES)(value)


#: How each field of :data:`ACCOUNT_FIELDS` is read from a change.
_FIELD_PARSERS: dict[str, Callable[[Any], Any]] = {
    "slug": parse_slug,
    "name": parse_name,
    "kind": _choice("kind", "kind", BILLING_ACCOUNT_KINDS),
    "payer": _choice("payer", "payer", ACCOUNT_PAYERS),
    "reimburse_share": parse_share,
    "route_patterns": parse_route_patterns,
    "tool_slugs": parse_account_tool_slugs,
    "default_cost_type": parse_default_cost_type,
    "is_active": bool,
}


def _audit_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, list):
        return list(value)
    return value


def account_details(account: AiBillingAccount, owner: User | None) -> dict[str, Any]:
    """Audit details of an account (no free text beyond its name)."""
    return {
        "account_id": account.id,
        "slug": account.slug,
        "name": account.name,
        "kind": account.kind,
        "owner": INSTANCE_OWNER if account.owner_user_id is None else (owner.login if owner else None),
        "owner_user_id": account.owner_user_id,
        "payer": account.payer,
        "reimburse_share": _audit_value(account.reimburse_share),
        "route_patterns": list(account.route_patterns or []),
        "tool_slugs": list(account.tool_slugs or []),
        "default_cost_type": account.default_cost_type,
        "is_active": account.is_active,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AiBillingAccountService:
    """Reads and changes the billing account register."""

    # -- access ----------------------------------------------------------------

    async def can_manage(self, session: AsyncSession, actor: User, owner_user_id: int | None) -> bool:
        """Whether *actor* may create or change an account of *owner_user_id* (``None``: an instance account)."""
        if is_global_admin(actor):
            return True
        if owner_user_id is None or owner_user_id != actor.id:
            return False
        return await _subscriptions.can_edit_own(session, actor)

    async def _require_can_manage(self, session: AsyncSession, actor: User, owner_user_id: int | None) -> None:
        if is_global_admin(actor):
            return
        if owner_user_id is None:
            raise PermissionDeniedError("Administrators manage the instance billing accounts")
        if owner_user_id != actor.id:
            raise NotFoundError("AI billing account not found")
        if not _subscriptions.can_hold(actor):
            raise AppError(
                code="ai_billing_account_owner_invalid",
                message="Service accounts do not own AI billing accounts",
                status_code=422,
                field="owner_user_id",
            )
        if not await _subscriptions.self_service_enabled(session):
            raise PermissionDeniedError(
                "An administrator manages AI billing accounts on this instance; ask one to change yours"
            )

    @staticmethod
    def can_view(viewer: User, account: AiBillingAccount) -> bool:
        """Instance accounts: everyone. Personal accounts: their owner and administrators."""
        return account.owner_user_id is None or account.owner_user_id == viewer.id or is_global_admin(viewer)

    # -- reads -----------------------------------------------------------------

    async def get(self, session: AsyncSession, account_id: int) -> AiBillingAccount:
        account = await session.get(AiBillingAccount, account_id)
        if account is None:
            raise NotFoundError("AI billing account not found")
        return account

    async def get_visible(self, session: AsyncSession, viewer: User, account_id: int) -> AiBillingAccount:
        """The account if *viewer* may see it; otherwise 404."""
        account = await self.get(session, account_id)
        if not self.can_view(viewer, account):
            raise NotFoundError("AI billing account not found")
        return account

    async def list_visible(
        self, session: AsyncSession, viewer: User, *, active_only: bool = False
    ) -> list[AiBillingAccount]:
        """Instance accounts, then *viewer*'s own, each by name."""
        stmt = select(AiBillingAccount).where(
            or_(AiBillingAccount.owner_user_id.is_(None), AiBillingAccount.owner_user_id == viewer.id)
        )
        if active_only:
            stmt = stmt.where(AiBillingAccount.is_active.is_(True))
        stmt = stmt.order_by(AiBillingAccount.owner_user_id.is_not(None), AiBillingAccount.name, AiBillingAccount.id)
        return list((await session.execute(stmt)).scalars().all())

    async def list_accounts(
        self,
        session: AsyncSession,
        *,
        scope: str = "all",
        owner_user_id: int | None = None,
        include_inactive: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AiBillingAccount], int]:
        """Accounts for administrators, instance ones first, by name.

        *scope* ``instance`` (company accounts), ``personal`` (people's; with
        *owner_user_id* one person's) or ``all``. Callers decide who may list whose.
        """
        stmt = select(AiBillingAccount)
        if scope == "instance":
            stmt = stmt.where(AiBillingAccount.owner_user_id.is_(None))
        elif scope == "personal":
            stmt = stmt.where(
                AiBillingAccount.owner_user_id == owner_user_id
                if owner_user_id is not None
                else AiBillingAccount.owner_user_id.is_not(None)
            )
        if not include_inactive:
            stmt = stmt.where(AiBillingAccount.is_active.is_(True))
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = (
            stmt.order_by(AiBillingAccount.owner_user_id.is_not(None), AiBillingAccount.name, AiBillingAccount.id)
            .offset(offset)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all()), int(total)

    async def subscription_links(self, session: AsyncSession, account_ids: Iterable[int]) -> dict[int, list[int]]:
        """The subscriptions each account is the fee schedule of (account id to subscription ids)."""
        ids = sorted(set(account_ids))
        if not ids:
            return {}
        rows = await session.execute(
            select(AiSubscription.billing_account_id, AiSubscription.id)
            .where(AiSubscription.billing_account_id.in_(ids))
            .order_by(AiSubscription.id)
        )
        links: dict[int, list[int]] = {}
        for account_id, sub_id in rows.all():
            if account_id is not None:
                links.setdefault(account_id, []).append(sub_id)
        return links

    async def by_ids(self, session: AsyncSession, account_ids: Iterable[int | None]) -> dict[int, AiBillingAccount]:
        """Accounts by id (callers check visibility)."""
        ids = sorted({i for i in account_ids if i is not None})
        if not ids:
            return {}
        rows = await session.execute(select(AiBillingAccount).where(AiBillingAccount.id.in_(ids)))
        return {a.id: a for a in rows.scalars().all()}

    async def instance_accounts(self, session: AsyncSession, *, active_only: bool = True) -> list[AiBillingAccount]:
        """Instance accounts, oldest first (the order ingest tries them in)."""
        stmt = select(AiBillingAccount).where(AiBillingAccount.owner_user_id.is_(None))
        if active_only:
            stmt = stmt.where(AiBillingAccount.is_active.is_(True))
        return list((await session.execute(stmt.order_by(AiBillingAccount.id))).scalars().all())

    async def personal_accounts(
        self, session: AsyncSession, user_id: int, *, active_only: bool = True
    ) -> list[AiBillingAccount]:
        """*user_id*'s own accounts, oldest first."""
        stmt = select(AiBillingAccount).where(AiBillingAccount.owner_user_id == user_id)
        if active_only:
            stmt = stmt.where(AiBillingAccount.is_active.is_(True))
        return list((await session.execute(stmt.order_by(AiBillingAccount.id))).scalars().all())

    async def find_by_slug(
        self, session: AsyncSession, slug: str, *, user_id: int, active_only: bool = True
    ) -> AiBillingAccount | None:
        """The account *slug* names for *user_id*: their own first, else an instance account."""
        normalized = normalize_account_slug(slug)
        if normalized is None:
            return None
        stmt = select(AiBillingAccount).where(
            AiBillingAccount.slug == normalized,
            or_(AiBillingAccount.owner_user_id == user_id, AiBillingAccount.owner_user_id.is_(None)),
        )
        if active_only:
            stmt = stmt.where(AiBillingAccount.is_active.is_(True))
        stmt = stmt.order_by(AiBillingAccount.owner_user_id.is_(None)).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    # -- writes ----------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        data: BillingAccountInput,
        *,
        owner: User | None,
        actor: User,
        request: Request | None = None,
    ) -> AiBillingAccount:
        """Add an instance account (*owner* ``None``, administrators) or a personal account of *owner*.

        Audited for an instance account and when *actor* is not *owner*.
        """
        owner_id = owner.id if owner is not None else None
        await self._require_can_manage(session, actor, owner_id)
        if owner is not None and not _subscriptions.can_hold(owner):
            raise AppError(
                code="ai_billing_account_owner_invalid",
                message="Service accounts and system accounts do not own AI billing accounts",
                status_code=422,
                field="owner_user_id",
            )
        values = self._validated(data.as_changes(), current=None)
        await self._require_free_slug(session, values["slug"], owner_id, None)
        account = AiBillingAccount(owner_user_id=owner_id, created_by_id=actor.id, **values)
        session.add(account)
        await session.flush()
        if account.route_patterns and account.is_active:
            # A new account takes the routed entries its patterns now match.
            await self._rebill(session, account, actor, request)
        if self._audited(account, actor):
            await self._log(
                session,
                AuditEvent.AI_BILLING_ACCOUNT_CREATED,
                actor,
                request,
                account_details(account, owner),
                account.id,
            )
        return account

    async def update(
        self,
        session: AsyncSession,
        account: AiBillingAccount,
        changes: Mapping[str, Any],
        *,
        actor: User,
        request: Request | None = None,
    ) -> AiBillingAccount:
        """Change the fields in *changes* (keys of :data:`ACCOUNT_FIELDS`).

        Switching the payer to ``employer`` drops the reimbursement share
        unless *changes* sets one (which is then refused). Audited (as
        ``_deactivated`` when it switches the account off) for an instance
        account and when *actor* is not the owner, only when something
        changed.
        """
        await self._require_can_manage(session, actor, account.owner_user_id)
        unknown = set(changes) - set(ACCOUNT_FIELDS)
        if unknown:
            raise _invalid(f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        values = self._validated(dict(changes), current=account)
        if values.get("payer", account.payer) != PAYER_EMPLOYER and await self._is_company_plan_account(
            session, account
        ):
            # A company seat plan is the employer's: its seat cost is never the employee's to claim back.
            raise _invalid("A company plan's account is paid by the employer", "payer")
        if "slug" in values and values["slug"] != account.slug:
            await self._require_free_slug(session, values["slug"], account.owner_user_id, account.id)
        before = {name: getattr(account, name) for name in ACCOUNT_FIELDS}
        for key, value in values.items():
            setattr(account, key, value)
        diff = {
            name: {"old": _audit_value(before[name]), "new": _audit_value(getattr(account, name))}
            for name in ACCOUNT_FIELDS
            if getattr(account, name) != before[name]
        }
        if not diff:
            return account
        await session.flush()
        if any(name in diff for name in RESOLUTION_FIELDS):
            await self._rebill(session, account, actor, request)
        if self._audited(account, actor):
            owner = await session.get(User, account.owner_user_id) if account.owner_user_id is not None else None
            deactivated = before["is_active"] and not account.is_active
            await self._log(
                session,
                AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED if deactivated else AuditEvent.AI_BILLING_ACCOUNT_UPDATED,
                actor,
                request,
                {**account_details(account, owner), "changes": diff},
                account.id,
            )
        return account

    @staticmethod
    async def _is_company_plan_account(session: AsyncSession, account: AiBillingAccount) -> bool:
        """Whether *account* is the fee schedule of a company seat plan."""
        if account.owner_user_id is not None:
            return False
        stmt = select(AiSubscription.id).where(
            AiSubscription.billing_account_id == account.id, AiSubscription.user_id.is_(None)
        )
        return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None

    @staticmethod
    async def _rebill(
        session: AsyncSession, account: AiBillingAccount, actor: User, request: Request | None = None
    ) -> None:
        """Resolve and re-price the usage entries the change of *account* may affect, like a price change."""
        from specivo.services.ai_usage_service import AiUsageService

        await AiUsageService().rebill_for_account(session, account, actor_id=actor.id, request=request)

    async def deactivate(
        self, session: AsyncSession, account: AiBillingAccount, *, actor: User, request: Request | None = None
    ) -> AiBillingAccount:
        """Switch an account off; usage keeps pointing at it."""
        return await self.update(session, account, {"is_active": False}, actor=actor, request=request)

    @staticmethod
    def _audited(account: AiBillingAccount, actor: User) -> bool:
        """Instance accounts always; personal ones when somebody else (an administrator) changes them."""
        return account.owner_user_id is None or account.owner_user_id != actor.id

    @staticmethod
    async def _log(
        session: AsyncSession,
        event: AuditEvent,
        actor: User | None,
        request: Request | None,
        details: dict[str, Any],
        resource_id: int | None,
        project_id: int | None = None,
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_ai_billing_change(
                session,
                event,
                user_id=actor.id if actor is not None else None,
                details=details,
                resource_id=resource_id,
                project_id=project_id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)

    async def _require_free_slug(
        self, session: AsyncSession, slug: str, owner_id: int | None, exclude_id: int | None
    ) -> None:
        owner_clause = (
            AiBillingAccount.owner_user_id.is_(None) if owner_id is None else AiBillingAccount.owner_user_id == owner_id
        )
        stmt = select(AiBillingAccount.id).where(AiBillingAccount.slug == slug, owner_clause)
        if exclude_id is not None:
            stmt = stmt.where(AiBillingAccount.id != exclude_id)
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            raise AppError(
                code="ai_billing_account_exists",
                message=f"An account '{slug}' exists",
                status_code=409,
                field="slug",
            )

    @staticmethod
    def _validated(changes: Mapping[str, Any], *, current: AiBillingAccount | None) -> dict[str, Any]:
        """Column values for a new account (*current* ``None``) or the changed fields of one."""
        values = {
            key: parse(changes.get(key, True if key == "is_active" else None))
            for key, parse in _FIELD_PARSERS.items()
            if key in changes or current is None
        }
        payer = values.get("payer", current.payer if current is not None else None)
        if payer != PAYER_EMPLOYEE:
            if values.get("reimburse_share") is not None:
                raise _invalid("Only accounts the employee pays for have a reimbursement share", "reimburse_share")
            if current is not None and current.reimburse_share is not None:
                values["reimburse_share"] = None
        return values

    # -- project payer default -------------------------------------------------

    @staticmethod
    def set_project_payer_default(project: Project, value: str) -> str:
        """Store *project*'s payer default (``employer``, ``employee`` or ``none``; ``none`` removes the key).

        Callers check the project management permission and flush.
        """
        if value not in PROJECT_PAYER_DEFAULTS:
            raise _invalid(
                f"The payer default is one of: {', '.join(PROJECT_PAYER_DEFAULTS)}",
                PROJECT_PAYER_DEFAULT_KEY,
            )
        settings = dict(project.settings or {})
        if value == PAYER_DEFAULT_NONE:
            settings.pop(PROJECT_PAYER_DEFAULT_KEY, None)
        else:
            settings[PROJECT_PAYER_DEFAULT_KEY] = value
        project.settings = settings
        return value

    async def change_project_payer_default(
        self,
        session: AsyncSession,
        project: Project,
        value: str,
        *,
        actor: User,
        request: Request | None = None,
    ) -> str:
        """Set *project*'s payer default and resolve its entries again; audited when it changes.

        Callers check the project management permission. The entries whose
        payer came from the default (or from nothing) are re-resolved in the
        same transaction, like an account change.
        """
        from specivo.services.ai_usage_service import AiUsageService

        old = project_payer_default(project.settings)
        new = self.set_project_payer_default(project, value)
        if new == old:
            return new
        session.add(project)
        await session.flush()
        outcome = await AiUsageService().rebill_for_project(session, project, actor_id=actor.id, request=request)
        await self._log(
            session,
            AuditEvent.AI_USAGE_PAYER_DEFAULT_CHANGED,
            actor,
            request,
            {
                "project_key": project.key,
                "old": old,
                "new": new,
                "entries": outcome.entries,
                "changed": outcome.changed,
            },
            None,
            project_id=project.id,
        )
        return new
