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
  subscription.

Lookups for ingest (:meth:`AiBillingAccountService.find_by_slug`,
:meth:`~AiBillingAccountService.instance_accounts`,
:meth:`~AiBillingAccountService.personal_accounts`) return active accounts
only; route and tool matching is :func:`specivo.core.ai_billing.route_matches`
and :func:`~specivo.core.ai_billing.covers_tool`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import (
    ACCOUNT_DEFAULT_COST_TYPES,
    ACCOUNT_PAYERS,
    BILLING_ACCOUNT_KINDS,
    MAX_ROUTE_PATTERNS,
    PAYER_DEFAULT_NONE,
    PAYER_EMPLOYEE,
    PROJECT_PAYER_DEFAULT_KEY,
    PROJECT_PAYER_DEFAULTS,
    normalize_account_slug,
    normalize_route_pattern,
)
from specivo.core.ai_subscriptions import normalize_tool_slug
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_subscription_service import AiSubscriptionService
from specivo.services.api_key_scope import is_global_admin

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
        self, session: AsyncSession, data: BillingAccountInput, *, owner: User | None, actor: User
    ) -> AiBillingAccount:
        """Add an instance account (*owner* ``None``, administrators) or a personal account of *owner*."""
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
        return account

    async def update(
        self, session: AsyncSession, account: AiBillingAccount, changes: Mapping[str, Any], *, actor: User
    ) -> AiBillingAccount:
        """Change the fields in *changes* (keys of :data:`ACCOUNT_FIELDS`).

        Switching the payer to ``employer`` drops the reimbursement share
        unless *changes* sets one (which is then refused).
        """
        await self._require_can_manage(session, actor, account.owner_user_id)
        unknown = set(changes) - set(ACCOUNT_FIELDS)
        if unknown:
            raise _invalid(f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        values = self._validated(dict(changes), current=account)
        if "slug" in values and values["slug"] != account.slug:
            await self._require_free_slug(session, values["slug"], account.owner_user_id, account.id)
        for key, value in values.items():
            setattr(account, key, value)
        await session.flush()
        return account

    async def deactivate(self, session: AsyncSession, account: AiBillingAccount, *, actor: User) -> AiBillingAccount:
        """Switch an account off; usage keeps pointing at it."""
        return await self.update(session, account, {"is_active": False}, actor=actor)

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
