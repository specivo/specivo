"""AI spend budgets: a monthly budget per billing account or payer, its progress, and the alerts it sends.

A budget covers one of:

- an **instance billing account** (a corporate gateway, a company plan):
  administrators set it, and only they see it;
- a **payer as a whole** (``employer``: everything the company pays;
  ``employee``: everything people pay themselves): administrators only;
- a **personal billing account**: its owner sets it (while AI subscription
  self-service is on, like the account itself) and administrators do; only
  the owner and administrators see it.

**Spend** is what the instance spend report counts for the calendar month
(UTC) so far, in the budget's currency: marginal spend plus seat cost,
allocated and unallocated, each amount converted on its own day
(:meth:`specivo.services.ai_spend_report_service.AiSpendReportService.budget_spend`,
one report per currency whatever the number of budgets). An account's spend
is its row of that report; a payer's is the report's total for that payer.
Amounts without a usable exchange rate are left out and flagged
(``unconverted``), never guessed.

**Alerts.** :meth:`AiSpendBudgetService.check` (the hourly beat task
``specivo.tasks.ai_usage.check_ai_spend_budgets``) compares each active
budget's spend with its thresholds (percent of the amount, default 80 and
100). A threshold reached records an ``ai_spend_budget_alerts`` row, unique
per budget, month and threshold, and only a row that was inserted leads to a
mail, so a threshold alerts once per month however often the check runs; the
next month starts again. On the first day of a month the check also looks at
the month that just ended, for usage reported late. Mails go to the active
administrators and, for a personal account, its owner, each in their own
language (:func:`specivo.core.i18n.recipient_locale`); in-app notifications
need a project and budgets have none.

**Audit.** Setting, changing and removing a budget writes
``ai_spend_budget_created`` / ``_updated`` / ``_deleted`` for instance
accounts and payers, and for an administrator's change to somebody else's
budget; a person's changes to their own are not audited, as for their
accounts. Every alert writes ``ai_spend_budget_alert_sent`` (no user).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_subscriptions import month_first, month_last
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.i18n import format_date, recipient_locale
from specivo.core.i18n import gettext_lazy as _l
from specivo.core.money import Money, MoneyError, check_minor_units, to_decimal
from specivo.core.utils import utcnow
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_spend_budget import (
    BUDGET_PAYERS,
    DEFAULT_THRESHOLDS,
    MAX_THRESHOLD,
    MAX_THRESHOLDS,
    AiSpendBudget,
    AiSpendBudgetAlert,
)
from specivo.models.user import User
from specivo.services.ai_billing_account_service import INSTANCE_OWNER, AiBillingAccountService
from specivo.services.api_key_scope import is_global_admin
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

_audit = SecurityAuditService()
_accounts = AiBillingAccountService()

#: What a budget covers: one billing account, or everything one payer pays.
TARGET_ACCOUNT = "billing_account"
TARGET_PAYER = "payer"
#: Fields a budget change may carry.
BUDGET_FIELDS: tuple[str, ...] = ("amount", "currency", "thresholds", "is_active")
#: Most integer digits of a budget amount (the column holds 12).
_AMOUNT_DIGITS = 12
_PERCENT = Decimal("0.1")
#: Most administrators one alert mails.
_MAX_ADMIN_MAILS = 50

# Mail text, formatted per recipient inside ``recipient_locale``.
_ALERT_SUBJECT = _l("AI spend budget: {name} reached {threshold}% of its budget for {month}")
_ALERT_BODY = _l("{name} has spent {spend} of its monthly budget of {amount} in {month} ({percent}%).")
_ALERT_UNCONVERTED = _l("Some of the spend has no exchange rate into {currency} yet and is not counted.")
_VIEW_BUDGET = _l("View the budget")
#: How a payer budget is named.
PAYER_BUDGET_NAMES: dict[str, Any] = {
    "employer": _l("Employer total"),
    "employee": _l("Employee total"),
}


def _invalid(message: str, field_name: str | None, code: str = "ai_spend_budget_invalid") -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field_name)


def _not_found() -> NotFoundError:
    return NotFoundError("AI spend budget not found")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_thresholds(value: object) -> list[int]:
    """Thresholds from a list or a space or comma separated string: whole percents from 1 to 1000, unique,
    ascending, at most :data:`MAX_THRESHOLDS`; blank gives the default (80 and 100)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return list(DEFAULT_THRESHOLDS)
    items = value.replace(",", " ").replace("%", " ").split() if isinstance(value, str) else value
    if not isinstance(items, Iterable):
        raise _invalid("List the thresholds as whole percents, such as 80, 100", "thresholds")
    out: set[int] = set()
    for item in items:
        if isinstance(item, bool):
            raise _invalid("List the thresholds as whole percents, such as 80, 100", "thresholds")
        try:
            number = int(str(item).strip())
        except ValueError:
            raise _invalid("List the thresholds as whole percents, such as 80, 100", "thresholds") from None
        if not 1 <= number <= MAX_THRESHOLD:
            raise _invalid(f"A threshold is a percent from 1 to {MAX_THRESHOLD}", "thresholds")
        out.add(number)
    if not out:
        return list(DEFAULT_THRESHOLDS)
    if len(out) > MAX_THRESHOLDS:
        raise _invalid(f"Give at most {MAX_THRESHOLDS} thresholds", "thresholds")
    return sorted(out)


def parse_amount(value: object, currency: str) -> Decimal:
    """A budget amount above zero, in *currency*'s minor units at most (floats refused)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _invalid("Enter the monthly budget", "amount")
    try:
        amount = to_decimal(value)
        check_minor_units(amount, currency)
    except (TypeError, MoneyError, InvalidOperation) as exc:
        message = str(exc) if isinstance(exc, MoneyError) else "Enter the amount as a number, such as 500"
        raise _invalid(message, "amount") from None
    if amount <= 0:
        raise _invalid("The budget is an amount above zero", "amount")
    if amount.adjusted() >= _AMOUNT_DIGITS:
        raise _invalid("The budget is too large", "amount")
    return amount


def _money_text(money: Money) -> str:
    return money.quantize().amount_str


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetInput:
    """A budget as someone entered it: on an account (``billing_account_id``) or a payer, not both."""

    amount: object
    currency: str
    thresholds: object = None
    is_active: bool = True
    billing_account_id: int | None = None
    payer: str | None = None


@dataclass(frozen=True, slots=True)
class BudgetProgress:
    """A budget's month so far: spend against the amount, both in the budget's currency."""

    budget_id: int
    period_start: date
    period_end: date
    #: The last day counted (today, or the month's last day for a month that has ended).
    as_of: date
    spend: Money
    amount: Money
    #: Spend / amount x 100, one decimal place.
    percent: Decimal
    #: Some spend has no usable exchange rate and is left out of ``spend``.
    unconverted: bool
    #: The budget's thresholds the spend has reached.
    reached: tuple[int, ...]
    #: The thresholds an alert went out for this month.
    alerted: tuple[int, ...]

    @property
    def over(self) -> bool:
        return self.spend.amount >= self.amount.amount


@dataclass(frozen=True, slots=True)
class Recipient:
    """Who an alert mails (read before the check commits, so no ORM object is needed afterwards)."""

    user_id: int
    email: str
    language: str | None
    is_admin: bool


@dataclass
class PendingAlert:
    """A budget that reached thresholds this run: the mails :meth:`AiSpendBudgetService.notify` sends."""

    budget_id: int
    name: str | None
    payer: str | None
    account_id: int | None
    personal: bool
    progress: BudgetProgress
    thresholds: list[int]
    recipients: list[Recipient] = field(default_factory=list)


@dataclass
class BudgetCheck:
    """What one run of the check did."""

    budgets: int = 0
    alerts: list[PendingAlert] = field(default_factory=list)


def _reached(spend: Decimal, amount: Decimal, thresholds: Iterable[int]) -> tuple[int, ...]:
    """The thresholds *spend* has reached, compared exactly (``spend x 100 >= amount x threshold``)."""
    return tuple(t for t in sorted(thresholds) if spend * 100 >= amount * t)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AiSpendBudgetService:
    """Reads and changes AI spend budgets, measures their progress and sends their alerts."""

    # -- access ----------------------------------------------------------------

    @staticmethod
    def can_view(viewer: User, account: AiBillingAccount | None) -> bool:
        """Administrators see every budget; a person the budget on their own personal account."""
        if is_global_admin(viewer):
            return True
        return account is not None and account.owner_user_id is not None and account.owner_user_id == viewer.id

    async def can_manage(self, session: AsyncSession, actor: User, account: AiBillingAccount | None) -> bool:
        """Whether *actor* may set the budget of *account* (``None``: a payer budget, administrators only)."""
        if account is None or account.owner_user_id is None:
            return is_global_admin(actor)
        return await _accounts.can_manage(session, actor, account.owner_user_id)

    async def _require_can_manage(self, session: AsyncSession, actor: User, account: AiBillingAccount | None) -> None:
        if account is None:
            if not is_global_admin(actor):
                raise PermissionDeniedError("Administrators set the payer budgets")
            return
        await _accounts.require_can_manage(session, actor, account.owner_user_id)

    @staticmethod
    def _audited(account: AiBillingAccount | None, actor: User) -> bool:
        """Payer and instance account budgets always; a personal one when somebody else changes it."""
        return account is None or account.owner_user_id is None or account.owner_user_id != actor.id

    # -- reads -----------------------------------------------------------------

    async def get(self, session: AsyncSession, budget_id: int) -> AiSpendBudget:
        budget = await session.get(AiSpendBudget, budget_id)
        if budget is None:
            raise _not_found()
        return budget

    async def account_of(self, session: AsyncSession, budget: AiSpendBudget) -> AiBillingAccount | None:
        if budget.billing_account_id is None:
            return None
        return await session.get(AiBillingAccount, budget.billing_account_id)

    async def get_visible(self, session: AsyncSession, viewer: User, budget_id: int) -> AiSpendBudget:
        """The budget if *viewer* may see it; otherwise 404 (whether or not it exists)."""
        budget = await session.get(AiSpendBudget, budget_id)
        if budget is None or not self.can_view(viewer, await self.account_of(session, budget)):
            raise _not_found()
        return budget

    async def accounts(self, session: AsyncSession, budgets: Iterable[AiSpendBudget]) -> dict[int, AiBillingAccount]:
        """The accounts of *budgets*, by account id (one query)."""
        return await _accounts.by_ids(session, (b.billing_account_id for b in budgets))

    async def list_all(
        self, session: AsyncSession, *, offset: int = 0, limit: int = 200
    ) -> tuple[list[AiSpendBudget], int]:
        """Every budget for administrators: payer budgets first, then by account id."""
        stmt = select(AiSpendBudget)
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = stmt.order_by(AiSpendBudget.payer.is_(None), AiSpendBudget.payer, AiSpendBudget.billing_account_id)
        rows = (await session.execute(stmt.offset(offset).limit(limit))).scalars().all()
        return list(rows), int(total)

    async def list_own(self, session: AsyncSession, user: User) -> list[AiSpendBudget]:
        """The budgets on *user*'s own personal accounts."""
        stmt = (
            select(AiSpendBudget)
            .join(AiBillingAccount, AiBillingAccount.id == AiSpendBudget.billing_account_id)
            .where(AiBillingAccount.owner_user_id == user.id)
            .order_by(AiBillingAccount.name, AiSpendBudget.id)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_visible(self, session: AsyncSession, viewer: User) -> list[AiSpendBudget]:
        """Every budget for an administrator, else the viewer's own."""
        if is_global_admin(viewer):
            return (await self.list_all(session))[0]
        return await self.list_own(session, viewer)

    async def for_accounts(self, session: AsyncSession, account_ids: Iterable[int]) -> dict[int, AiSpendBudget]:
        """The budget of each account of *account_ids* that has one (callers check visibility)."""
        ids = sorted(set(account_ids))
        if not ids:
            return {}
        rows = await session.execute(select(AiSpendBudget).where(AiSpendBudget.billing_account_id.in_(ids)))
        return {b.billing_account_id: b for b in rows.scalars().all() if b.billing_account_id is not None}

    async def for_payers(self, session: AsyncSession) -> dict[str, AiSpendBudget]:
        """The payer budgets, by payer (administrators only)."""
        rows = await session.execute(select(AiSpendBudget).where(AiSpendBudget.payer.is_not(None)))
        return {b.payer: b for b in rows.scalars().all() if b.payer is not None}

    # -- writes ----------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        data: BudgetInput,
        *,
        actor: User,
        request: Request | None = None,
    ) -> AiSpendBudget:
        """Set a budget on an account or a payer. 409 ``ai_spend_budget_exists`` when it has one."""
        account: AiBillingAccount | None = None
        if (data.billing_account_id is None) == (data.payer is None or data.payer == ""):
            raise _invalid("Give a billing account or a payer, not both", "billing_account_id")
        if data.billing_account_id is not None:
            account = await session.get(AiBillingAccount, data.billing_account_id)
            if account is None or not _accounts.can_view(actor, account):
                raise NotFoundError("AI billing account not found")
            payer = None
        else:
            payer = str(data.payer).strip().lower()
            if payer not in BUDGET_PAYERS:
                raise _invalid(f"The payer is one of: {', '.join(BUDGET_PAYERS)}", "payer")
        await self._require_can_manage(session, actor, account)
        currency, amount = await self._currency_and_amount(session, data.currency, data.amount)
        thresholds = parse_thresholds(data.thresholds)
        await self._require_free(session, account, payer)
        budget = AiSpendBudget(
            billing_account_id=account.id if account is not None else None,
            payer=payer,
            amount_amount=amount,
            amount_currency=currency,
            thresholds=thresholds,
            is_active=bool(data.is_active),
            created_by_id=actor.id,
        )
        session.add(budget)
        await session.flush()
        if self._audited(account, actor):
            await self._log(
                session, AuditEvent.AI_SPEND_BUDGET_CREATED, actor, request, await self._details(session, budget)
            )
        return budget

    async def update(
        self,
        session: AsyncSession,
        budget: AiSpendBudget,
        changes: Mapping[str, Any],
        *,
        actor: User,
        request: Request | None = None,
    ) -> AiSpendBudget:
        """Change the fields in *changes* (keys of :data:`BUDGET_FIELDS`); audited only when something changed.

        Alerts already sent this month stay sent: raising the amount does not
        send a threshold again in the same month.
        """
        account = await self.account_of(session, budget)
        await self._require_can_manage(session, actor, account)
        unknown = set(changes) - set(BUDGET_FIELDS)
        if unknown:
            raise _invalid(f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        before = {
            "amount": budget.amount_amount,
            "currency": budget.amount_currency,
            "thresholds": list(budget.thresholds),
            "is_active": budget.is_active,
        }
        if "currency" in changes or "amount" in changes:
            currency, amount = await self._currency_and_amount(
                session,
                changes.get("currency") or budget.amount_currency,
                changes.get("amount", budget.amount_amount),
            )
            budget.amount_currency, budget.amount_amount = currency, amount
        if "thresholds" in changes:
            budget.thresholds = parse_thresholds(changes["thresholds"])
        if "is_active" in changes and changes["is_active"] is not None:
            budget.is_active = bool(changes["is_active"])
        after = {
            "amount": budget.amount_amount,
            "currency": budget.amount_currency,
            "thresholds": list(budget.thresholds),
            "is_active": budget.is_active,
        }
        diff = {
            name: {"old": _audit_value(before[name]), "new": _audit_value(after[name])}
            for name in BUDGET_FIELDS
            if before[name] != after[name]
        }
        if not diff:
            return budget
        await session.flush()
        if self._audited(account, actor):
            details = {**await self._details(session, budget), "changes": diff}
            await self._log(session, AuditEvent.AI_SPEND_BUDGET_UPDATED, actor, request, details)
        return budget

    async def delete(
        self, session: AsyncSession, budget: AiSpendBudget, *, actor: User, request: Request | None = None
    ) -> None:
        """Remove a budget and its alert history."""
        account = await self.account_of(session, budget)
        await self._require_can_manage(session, actor, account)
        details = await self._details(session, budget)
        await session.delete(budget)
        await session.flush()
        if self._audited(account, actor):
            await self._log(session, AuditEvent.AI_SPEND_BUDGET_DELETED, actor, request, details)

    @staticmethod
    async def _currency_and_amount(session: AsyncSession, currency: object, amount: object) -> tuple[str, Decimal]:
        from specivo.services.currency_service import CurrencyService

        if not isinstance(currency, str) or not currency.strip():
            raise _invalid("Choose a currency", "currency")
        code = (await CurrencyService().require_selectable(session, currency, field="currency")).code
        return code, parse_amount(amount, code)

    async def _require_free(self, session: AsyncSession, account: AiBillingAccount | None, payer: str | None) -> None:
        stmt = select(AiSpendBudget.id)
        stmt = (
            stmt.where(AiSpendBudget.billing_account_id == account.id)
            if account is not None
            else stmt.where(AiSpendBudget.payer == payer)
        )
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            raise AppError(
                code="ai_spend_budget_exists",
                message="This already has a budget; change that one",
                status_code=409,
                field="billing_account_id" if account is not None else "payer",
            )

    # -- audit -----------------------------------------------------------------

    async def _details(self, session: AsyncSession, budget: AiSpendBudget) -> dict[str, Any]:
        """Audit details of a budget (ids, slugs, logins and amounts only)."""
        account = await self.account_of(session, budget)
        owner: str | None = None
        if account is not None:
            if account.owner_user_id is None:
                owner = INSTANCE_OWNER
            else:
                user = await session.get(User, account.owner_user_id)
                owner = user.login if user is not None else None
        return {
            "budget_id": budget.id,
            "target": TARGET_ACCOUNT if budget.billing_account_id is not None else TARGET_PAYER,
            "account_id": budget.billing_account_id,
            "account_slug": account.slug if account is not None else None,
            "owner": owner,
            "payer": budget.payer,
            "amount": _money_text(budget.amount),
            "currency": budget.amount_currency,
            "thresholds": list(budget.thresholds),
            "is_active": budget.is_active,
        }

    @staticmethod
    async def _log(
        session: AsyncSession,
        event: AuditEvent,
        actor: User | None,
        request: Request | None,
        details: dict[str, Any],
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_ai_billing_change(
                session,
                event,
                user_id=actor.id if actor is not None else None,
                details=details,
                resource_id=details.get("budget_id"),
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)

    # -- progress --------------------------------------------------------------

    async def progress(
        self, session: AsyncSession, budgets: Sequence[AiSpendBudget], *, today: date | None = None
    ) -> dict[int, BudgetProgress]:
        """Each budget's month so far (the calendar month of *today*, UTC, up to *today*), by budget id.

        One spend report per budget currency (see the module docstring), plus
        one query for the alerts already sent.
        """
        from specivo.services.ai_spend_report_service import AiSpendReportService

        if not budgets:
            return {}
        as_of = today or utcnow().date()
        first, last = month_first(as_of.year, as_of.month), month_last(as_of.year, as_of.month)
        alerted = await self._alerted(session, [b.id for b in budgets], first)
        by_currency: dict[str, list[AiSpendBudget]] = defaultdict(list)
        for budget in budgets:
            by_currency[budget.amount_currency].append(budget)
        out: dict[int, BudgetProgress] = {}
        reports = AiSpendReportService()
        for currency, group in sorted(by_currency.items()):
            report = await reports.budget_spend(session, date_from=first, date_to=as_of, currency=currency)
            per_account = {row.key.get("billing_account"): row.total_spend for row in report.rows}
            per_payer = {row.key.get("payer"): row.total_spend for row in report.payer_totals}
            for budget in group:
                summary = (
                    per_account.get(budget.billing_account_id)
                    if budget.billing_account_id is not None
                    else per_payer.get(budget.payer)
                )
                spend = summary.total if summary is not None else Money.zero(currency)
                amount = budget.amount
                percent = (spend.amount * 100 / amount.amount).quantize(_PERCENT, rounding=ROUND_HALF_UP)
                out[budget.id] = BudgetProgress(
                    budget_id=budget.id,
                    period_start=first,
                    period_end=last,
                    as_of=as_of,
                    spend=spend,
                    amount=amount,
                    percent=percent,
                    unconverted=bool(summary is not None and summary.has_unconverted),
                    reached=_reached(spend.amount, amount.amount, budget.thresholds),
                    alerted=tuple(sorted(alerted.get(budget.id, ()))),
                )
        return out

    @staticmethod
    async def _alerted(session: AsyncSession, budget_ids: list[int], period_start: date) -> dict[int, set[int]]:
        if not budget_ids:
            return {}
        rows = await session.execute(
            select(AiSpendBudgetAlert.budget_id, AiSpendBudgetAlert.threshold).where(
                AiSpendBudgetAlert.budget_id.in_(budget_ids), AiSpendBudgetAlert.period_start == period_start
            )
        )
        out: dict[int, set[int]] = defaultdict(set)
        for budget_id, threshold in rows.all():
            out[budget_id].add(threshold)
        return out

    # -- the check -------------------------------------------------------------

    async def check(self, session: AsyncSession, *, today: date | None = None) -> BudgetCheck:
        """Record the thresholds the active budgets reached this month and gather the mails to send.

        The caller commits, then calls :meth:`notify` with ``alerts``. A
        threshold already recorded for the month is skipped (the unique row is
        the guard, so two overlapping runs cannot both alert). On the first
        day of a month the month that just ended is checked too.
        """
        day = today or utcnow().date()
        budgets = list(
            (await session.execute(select(AiSpendBudget).where(AiSpendBudget.is_active.is_(True)))).scalars().all()
        )
        outcome = BudgetCheck(budgets=len(budgets))
        if not budgets:
            return outcome
        accounts = await self.accounts(session, budgets)
        days = [day - timedelta(days=1), day] if day.day == 1 else [day]
        admins: list[Recipient] | None = None
        for as_of in days:
            progress = await self.progress(session, budgets, today=as_of)
            for budget in budgets:
                p = progress[budget.id]
                due = [t for t in p.reached if t not in p.alerted]
                if not due:
                    continue
                account = accounts.get(budget.billing_account_id) if budget.billing_account_id is not None else None
                if admins is None:
                    admins = await self._admins(session)
                recipients = await self._recipients(session, account, admins)
                sent = await self._record(session, budget, p, due, len(recipients))
                if not sent:
                    continue
                alert = PendingAlert(
                    budget_id=budget.id,
                    name=account.name if account is not None else None,
                    payer=budget.payer,
                    account_id=account.id if account is not None else None,
                    personal=account is not None and account.owner_user_id is not None,
                    progress=p,
                    thresholds=sent,
                    recipients=recipients,
                )
                outcome.alerts.append(alert)
                details = {
                    **await self._details(session, budget),
                    "month": p.period_start.strftime("%Y-%m"),
                    "thresholds": sent,
                    "spend": _money_text(p.spend),
                    "recipients": len(recipients),
                }
                await self._log(session, AuditEvent.AI_SPEND_BUDGET_ALERT_SENT, None, None, details)
        return outcome

    @staticmethod
    async def _record(
        session: AsyncSession, budget: AiSpendBudget, progress: BudgetProgress, due: list[int], recipients: int
    ) -> list[int]:
        """Insert an alert row per threshold in *due*; the thresholds whose row was new (and so alert now)."""
        sent: list[int] = []
        for threshold in due:
            stmt = (
                pg_insert(AiSpendBudgetAlert)
                .values(
                    budget_id=budget.id,
                    period_start=progress.period_start,
                    threshold=threshold,
                    spend_amount=progress.spend.amount,
                    spend_currency=progress.spend.currency,
                    budget_amount=progress.amount.amount,
                    recipients=recipients,
                )
                .on_conflict_do_nothing(constraint="uq_ai_spend_budget_alerts_once")
                .returning(AiSpendBudgetAlert.id)
            )
            if (await session.execute(stmt)).scalar_one_or_none() is not None:
                sent.append(threshold)
        return sent

    @staticmethod
    async def _admins(session: AsyncSession) -> list[Recipient]:
        stmt = (
            select(User)
            .where(User.is_admin.is_(True), User.status == "active", User.is_service_account.is_(False))
            .order_by(User.id)
            .limit(_MAX_ADMIN_MAILS)
        )
        return [
            Recipient(user_id=u.id, email=u.email, language=u.language, is_admin=True)
            for u in (await session.execute(stmt)).scalars().all()
            if u.email and not u.is_system_principal
        ]

    @staticmethod
    async def _recipients(
        session: AsyncSession, account: AiBillingAccount | None, admins: list[Recipient]
    ) -> list[Recipient]:
        """The administrators and, for a personal account, its owner (once, when the owner is one of them)."""
        out = list(admins)
        if account is None or account.owner_user_id is None:
            return out
        if any(r.user_id == account.owner_user_id for r in out):
            return out
        owner = await session.get(User, account.owner_user_id)
        if owner is not None and owner.email and owner.status == "active" and not owner.is_system_principal:
            out.append(Recipient(user_id=owner.id, email=owner.email, language=owner.language, is_admin=False))
        return out

    # -- mail ------------------------------------------------------------------

    def notify(self, alerts: Iterable[PendingAlert]) -> int:
        """Queue the mails of *alerts* (after the check committed): one per budget and recipient, naming the
        highest threshold reached. Returns how many were queued; a failure is logged, never raised."""
        from specivo.core.app_url import public_url

        queued = 0
        for alert in alerts:
            p = alert.progress
            threshold = max(alert.thresholds)
            for to in alert.recipients:
                if alert.account_id is None:
                    path = "/admin/ai-billing-accounts/#payer-budgets"
                elif to.is_admin:
                    path = f"/admin/ai-billing-accounts/{alert.account_id}/"
                else:
                    path = f"/my/ai-billing-accounts/{alert.account_id}/"
                with recipient_locale(to.language) as locale:
                    name = alert.name if alert.name is not None else str(PAYER_BUDGET_NAMES.get(alert.payer or "", ""))
                    values = {
                        "name": name,
                        "threshold": threshold,
                        "month": format_date(p.period_start, "yMMMM", locale),
                        "spend": p.spend.format(locale),
                        "amount": p.amount.format(locale),
                        "percent": p.percent,
                    }
                    lines = [str(_ALERT_BODY).format(**values)]
                    if p.unconverted:
                        lines.append(str(_ALERT_UNCONVERTED).format(currency=p.amount.currency))
                    if self._send(to, str(_ALERT_SUBJECT).format(**values), lines, public_url(path), str(_VIEW_BUDGET)):
                        queued += 1
        return queued

    @staticmethod
    def _send(to: Recipient, subject: str, lines: list[str], link: str | None, link_text: str) -> bool:
        """Queue one mail; a failure is logged and never fails the check."""
        from specivo.tasks.notifications import send_notification_email

        html = "".join(f"<p>{escape(line)}</p>" for line in lines)
        plain = "\n\n".join(lines)
        if link:
            html += f'<p><a href="{escape(link)}">{escape(link_text)}</a></p>'
            plain += f"\n\n{link_text}: {link}"
        try:
            send_notification_email.delay(to.email, subject, html, plain + "\n", None)
        except Exception:
            logger.exception("Failed to queue the AI spend budget mail to user %s", to.user_id)
            return False
        return True


def _audit_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


__all__ = [
    "BUDGET_FIELDS",
    "PAYER_BUDGET_NAMES",
    "TARGET_ACCOUNT",
    "TARGET_PAYER",
    "AiSpendBudgetService",
    "BudgetCheck",
    "BudgetInput",
    "BudgetProgress",
    "PendingAlert",
    "Recipient",
    "parse_amount",
    "parse_thresholds",
]
