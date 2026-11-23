"""AI spend by payer and billing account: report dimensions, filters, organization scope and reimbursable.

The spend reports (:mod:`specivo.services.ai_spend_report_service`) group by
``payer``, ``billing_account`` and ``route``, filter by them, and split their
totals per payer. This module holds what those surfaces share:

- :class:`BillingFilter`: the ``payer``, ``billing_account``, ``route`` and
  ``scope`` filters, as WHERE conditions the reports, the charts and the entry
  lists apply the same way. A seat fee never matches one (``excludes_seats``).
- :func:`personal_entry_clause`: the entries the ``organization`` scope leaves
  out. An entry is *personal* when it has no project, when its project's payer
  default is ``employee`` (a person's own project), or when that default
  decided an ``employee`` payer for it (``billing_method`` ``project_default``).
- **Reimbursable**: ``reimburse_share x (allocated seat cost + marginal)`` of
  the entries paid by an employee (``payer`` ``employee``) in a project whose
  payer default is ``employer``. The share is read from the entry's billing
  account at report time (entries store no reimbursable amount; ``NULL`` is
  0). :func:`reimburse_factors` gives the factor of each (account, project).
- **Who sees what**: a personal billing account is named only to its owner
  and administrators (``AiBillingAccountService.can_view``); anyone else sees
  "Personal account" (:data:`HIDDEN_ACCOUNT`, one row for all of them). The
  reimbursable amount follows the seat-cost rule and is shown only to
  administrators and to a person reporting on their own usage
  (:func:`reimbursable_allowed`), and only when the report holds
  employee-paid usage (otherwise it is 0 by definition).

Pure SQL expressions and small lookups; no service state.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.ai_billing import (
    BILLING_METHOD_PROJECT_DEFAULT,
    ENTRY_PAYERS,
    PAYER_EMPLOYEE,
    PAYER_EMPLOYER,
    PROJECT_PAYER_DEFAULT_KEY,
    normalize_account_slug,
)
from specivo.core.exceptions import AppError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project

if TYPE_CHECKING:
    from specivo.models.user import User

#: The report key of a billing account the viewer may not see (a personal account of someone else).
HIDDEN_ACCOUNT = 0
#: How a report names a billing account the viewer may not see.
PERSONAL_ACCOUNT_LABEL = "Personal account"
#: Labels of the ``payer`` rows.
PAYER_LABELS: dict[str, str] = {"employer": "Employer", "employee": "Employee", "unknown": "Unknown"}
#: The filter value for "without one" (no billing account, no route).
NO_VALUE = "-"
#: Values of the ``scope`` filter: every entry, or the organization's (personal entries left out).
SCOPE_ALL = "all"
SCOPE_ORGANIZATION = "organization"
REPORT_SCOPES: tuple[str, ...] = (SCOPE_ALL, SCOPE_ORGANIZATION)
#: Longest route a filter compares (the column's length).
ROUTE_FILTER_MAX_LENGTH = 100


def _invalid(message: str, field_name: str) -> AppError:
    return AppError(code="ai_spend_report_invalid", message=message, status_code=422, field=field_name)


@dataclass(frozen=True, slots=True)
class BillingFilter:
    """The payer, billing account, route and scope filters of a report or an entry list.

    ``billing_account_id`` ``-1`` matches nothing (an account the viewer may
    not see, or an unknown one); ``organization`` leaves personal entries out
    (:func:`personal_entry_clause`).
    """

    payer: str | None = None
    billing_account_id: int | None = None
    without_billing_account: bool = False
    route: str | None = None
    without_route: bool = False
    organization: bool = False

    @property
    def active(self) -> bool:
        """Whether any of the filters is set (a seat fee then matches none)."""
        return bool(
            self.payer
            or self.billing_account_id is not None
            or self.without_billing_account
            or self.route
            or self.without_route
            or self.organization
        )

    def conditions(self) -> list[ColumnElement[bool]]:
        """The WHERE conditions over ``ai_usage_entries``."""
        out: list[ColumnElement[bool]] = []
        if self.payer:
            out.append(AiUsageEntry.payer == self.payer)
        if self.billing_account_id is not None:
            out.append(AiUsageEntry.billing_account_id == self.billing_account_id)
        if self.without_billing_account:
            out.append(AiUsageEntry.billing_account_id.is_(None))
        if self.route:
            out.append(AiUsageEntry.route == self.route)
        if self.without_route:
            out.append(AiUsageEntry.route.is_(None))
        if self.organization:
            out.append(~personal_entry_clause())
        return out

    def cache_parts(self) -> list[Any]:
        return [
            self.payer,
            self.billing_account_id,
            self.without_billing_account,
            self.route,
            self.without_route,
            self.organization,
        ]


def _personal_projects() -> Any:
    """The ids of projects whose payer default is ``employee`` (a person's own project)."""
    return select(Project.id).where(Project.settings[PROJECT_PAYER_DEFAULT_KEY].astext == PAYER_EMPLOYEE)


def personal_entry_clause() -> ColumnElement[bool]:
    """Entries that are personal: no project, a project whose payer default is ``employee``, or an
    ``employee`` payer that the project default decided."""
    return or_(
        AiUsageEntry.project_id.is_(None),
        and_(
            AiUsageEntry.billing_method == BILLING_METHOD_PROJECT_DEFAULT,
            AiUsageEntry.payer == PAYER_EMPLOYEE,
        ),
        AiUsageEntry.project_id.in_(_personal_projects()),
    )


# ---------------------------------------------------------------------------
# Parsing the filters
# ---------------------------------------------------------------------------


def parse_payer(value: str | None) -> str | None:
    """A ``payer`` filter: ``employer``, ``employee`` or ``unknown`` (blank: none); anything else is a 422."""
    text = (value or "").strip().lower()
    if not text:
        return None
    if text not in ENTRY_PAYERS:
        raise _invalid(f"payer must be one of {', '.join(ENTRY_PAYERS)}", "payer")
    return text


def parse_scope(value: str | None) -> bool:
    """Whether a ``scope`` filter keeps the organization's entries only (blank or ``all``: every entry)."""
    text = (value or "").strip().lower()
    if not text or text == SCOPE_ALL:
        return False
    if text != SCOPE_ORGANIZATION:
        raise _invalid(f"scope must be one of {', '.join(REPORT_SCOPES)}", "scope")
    return True


def route_filter(value: str | None) -> dict[str, Any]:
    """The ``BillingFilter`` fields of a ``route`` filter: a route, or ``-`` for entries without one."""
    text = (value or "").strip()
    if not text:
        return {}
    if text == NO_VALUE:
        return {"without_route": True}
    return {"route": text[:ROUTE_FILTER_MAX_LENGTH]}


def payer_text_value(value: str | None) -> str:
    """A ``payer`` filter as a form keeps it: a known payer, else nothing."""
    text = (value or "").strip().lower()
    return text if text in ENTRY_PAYERS else ""


def scope_text_value(value: str | None) -> str:
    """A ``scope`` filter as a form keeps it: ``organization``, else nothing (every entry)."""
    return SCOPE_ORGANIZATION if (value or "").strip().lower() == SCOPE_ORGANIZATION else ""


@dataclass(frozen=True, slots=True)
class ResolvedAccount:
    """A ``billing_account`` filter resolved for a viewer: its ``BillingFilter`` fields and its name."""

    fields: dict[str, Any]
    #: The account's name when the viewer may see it, "" for none, else the value as typed.
    label: str
    found: bool


async def resolve_billing_account(session: AsyncSession, viewer: User, value: str | None) -> ResolvedAccount:
    """A ``billing_account`` filter: an account id, a slug (the viewer's own first, then an instance
    account), or ``-`` for entries without an account.

    An account the viewer may not see (``AiBillingAccountService.can_view``),
    or none by that id or slug, matches nothing (``billing_account_id`` -1)
    and is named only by what was typed, so a link never tells whose it is.
    """
    from specivo.services.ai_billing_account_service import AiBillingAccountService

    text = (value or "").strip()
    if not text:
        return ResolvedAccount(fields={}, label="", found=True)
    if text == NO_VALUE:
        return ResolvedAccount(fields={"without_billing_account": True}, label="", found=True)
    account: AiBillingAccount | None = None
    if text.isdecimal() and len(text) < 10:
        account = await session.get(AiBillingAccount, int(text))
    else:
        slug = normalize_account_slug(text)
        if slug is not None:
            stmt = (
                select(AiBillingAccount)
                .where(
                    AiBillingAccount.slug == slug,
                    or_(AiBillingAccount.owner_user_id == viewer.id, AiBillingAccount.owner_user_id.is_(None)),
                )
                .order_by(AiBillingAccount.owner_user_id.is_(None))
                .limit(1)
            )
            account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None or not AiBillingAccountService.can_view(viewer, account):
        return ResolvedAccount(fields={"billing_account_id": -1}, label=text[:64], found=False)
    return ResolvedAccount(fields={"billing_account_id": account.id}, label=account.name, found=True)


# ---------------------------------------------------------------------------
# Labels and visibility
# ---------------------------------------------------------------------------


async def account_names(session: AsyncSession, viewer: User, account_ids: Iterable[Any]) -> dict[int, str | None]:
    """Per account id: its name when *viewer* may see the account, else None (show "Personal account")."""
    from specivo.services.ai_billing_account_service import AiBillingAccountService

    ids = {int(i) for i in account_ids if i is not None and i != HIDDEN_ACCOUNT}
    if not ids:
        return {}
    accounts = await AiBillingAccountService().by_ids(session, ids)
    return {a.id: (a.name if AiBillingAccountService.can_view(viewer, a) else None) for a in accounts.values()}


def reimbursable_allowed(viewer: User, user_filter: int | None, seat_shown: bool) -> bool:
    """Whether a report shows reimbursable amounts: with seat cost, to administrators, or to a person
    whose report is narrowed to their own usage (the holder of the accounts)."""
    from specivo.services.api_key_scope import is_global_admin

    if not seat_shown:
        return False
    return is_global_admin(viewer) or (user_filter is not None and user_filter == viewer.id)


# ---------------------------------------------------------------------------
# Reimbursable
# ---------------------------------------------------------------------------


async def reimburse_shares(session: AsyncSession, account_ids: Collection[int]) -> dict[int, Decimal]:
    """The reimbursable share of each employee-paid account in *account_ids* that has one above 0."""
    ids = [i for i in account_ids if i is not None]
    if not ids:
        return {}
    stmt = select(AiBillingAccount.id, AiBillingAccount.reimburse_share).where(
        AiBillingAccount.id.in_(ids),
        AiBillingAccount.payer == PAYER_EMPLOYEE,
        AiBillingAccount.reimburse_share > 0,
    )
    return {aid: Decimal(share) for aid, share in (await session.execute(stmt)).all() if share is not None}


async def employer_projects(session: AsyncSession, project_ids: Collection[int]) -> set[int]:
    """The projects of *project_ids* whose payer default is ``employer`` (work for the company)."""
    ids = [i for i in project_ids if i is not None]
    if not ids:
        return set()
    stmt = select(Project.id).where(
        Project.id.in_(ids), Project.settings[PROJECT_PAYER_DEFAULT_KEY].astext == PAYER_EMPLOYER
    )
    return set((await session.execute(stmt)).scalars().all())


async def reimburse_factors(
    session: AsyncSession, pairs: Iterable[tuple[str | None, int | None, int | None]]
) -> dict[tuple[int, int], Decimal]:
    """The reimbursable factor per ``(billing account id, project id)`` of *pairs* ``(payer, account, project)``.

    Only employee-paid usage on an account with a share, in a project whose
    payer default is ``employer``, has one; every other pair is left out (0).
    Two queries whatever the number of pairs.
    """
    wanted = {
        (account, project)
        for payer, account, project in pairs
        if payer == PAYER_EMPLOYEE and account is not None and project is not None
    }
    if not wanted:
        return {}
    shares = await reimburse_shares(session, {a for a, _p in wanted})
    if not shares:
        return {}
    employer = await employer_projects(session, {p for a, p in wanted if a in shares})
    return {(a, p): shares[a] for a, p in wanted if a in shares and p in employer}


def reimbursable_amount(share: Decimal, marginal: Decimal | None, seat: Decimal | None) -> Decimal:
    """``share x (seat + marginal)``, each part optional: what an employee may claim for one entry.

    The same rule the reports apply (they convert each part on its own day
    before summing); exposed for the reimbursement claims.
    """
    return share * ((seat or Decimal(0)) + (marginal or Decimal(0)))


__all__ = [
    "HIDDEN_ACCOUNT",
    "NO_VALUE",
    "PAYER_LABELS",
    "PERSONAL_ACCOUNT_LABEL",
    "REPORT_SCOPES",
    "SCOPE_ALL",
    "SCOPE_ORGANIZATION",
    "BillingFilter",
    "ResolvedAccount",
    "account_names",
    "employer_projects",
    "parse_payer",
    "parse_scope",
    "payer_text_value",
    "personal_entry_clause",
    "reimburse_factors",
    "reimburse_shares",
    "reimbursable_allowed",
    "reimbursable_amount",
    "resolve_billing_account",
    "route_filter",
    "scope_text_value",
]
