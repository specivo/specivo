"""What the web pages show of AI spend budgets: progress rows, the budget form and its errors.

Shared by the billing account pages (a budget per account, the payer budgets
on the admin page) and the instance AI spend report. The numbers come from
:meth:`specivo.services.ai_spend_budget_service.AiSpendBudgetService.progress`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.i18n import format_date, get_locale
from specivo.core.money import format_decimal_plain
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_spend_budget import DEFAULT_THRESHOLDS, AiSpendBudget
from specivo.services.ai_spend_budget_service import PAYER_BUDGET_NAMES, AiSpendBudgetService, BudgetProgress

_service = AiSpendBudgetService()

#: Fields of the budget form.
BUDGET_FORM_FIELDS: tuple[str, ...] = ("amount", "currency", "thresholds", "is_active")


def thresholds_text(thresholds: Sequence[int]) -> str:
    return ", ".join(str(t) for t in thresholds)


def budget_view(
    budget: AiSpendBudget, progress: BudgetProgress | None, account: AiBillingAccount | None = None
) -> dict[str, Any]:
    """A budget and its month so far, formatted in the request's language."""
    locale = get_locale()
    payer_name = PAYER_BUDGET_NAMES.get(budget.payer or "", budget.payer or "")
    name = account.name if account is not None else str(payer_name)
    view: dict[str, Any] = {
        "id": budget.id,
        "name": name,
        "payer": budget.payer,
        "account_id": budget.billing_account_id,
        "personal": account is not None and account.owner_user_id is not None,
        "amount": budget.amount.format(locale),
        "currency": budget.amount_currency,
        "thresholds": list(budget.thresholds),
        "thresholds_text": thresholds_text(budget.thresholds),
        "is_active": budget.is_active,
        "has_progress": progress is not None,
    }
    if progress is not None:
        percent = progress.percent
        view.update(
            spend=progress.spend.format(locale),
            percent=format_decimal_plain(percent),
            # A meter shows at most the whole budget; the text says how far past it the spend is.
            meter=format_decimal_plain(min(percent, Decimal(100))),
            low=min(budget.thresholds),
            over=progress.over,
            warn=bool(progress.reached) and not progress.over,
            reached=list(progress.reached),
            alerted=list(progress.alerted),
            unconverted=progress.unconverted,
            month=format_date(progress.period_start, "yMMMM", locale),
        )
    return view


async def budget_views(
    db: AsyncSession, budgets: Sequence[AiSpendBudget], accounts: dict[int, AiBillingAccount] | None = None
) -> dict[int, dict[str, Any]]:
    """The view of each budget, by budget id (one spend report per currency)."""
    if not budgets:
        return {}
    accounts = accounts if accounts is not None else await _service.accounts(db, budgets)
    progress = await _service.progress(db, budgets)
    return {
        b.id: budget_view(
            b, progress.get(b.id), accounts.get(b.billing_account_id) if b.billing_account_id is not None else None
        )
        for b in budgets
    }


async def account_budget_views(db: AsyncSession, accounts: Sequence[AiBillingAccount]) -> dict[int, dict[str, Any]]:
    """The budget view of each account of *accounts* that has a budget, by account id."""
    by_account = await _service.for_accounts(db, (a.id for a in accounts))
    if not by_account:
        return {}
    views = await budget_views(db, list(by_account.values()), {a.id: a for a in accounts})
    return {account_id: views[b.id] for account_id, b in by_account.items()}


def blank_budget_form(currency: str) -> dict[str, Any]:
    return {"amount": "", "currency": currency, "thresholds": thresholds_text(DEFAULT_THRESHOLDS), "is_active": True}


def budget_form_from(budget: AiSpendBudget) -> dict[str, Any]:
    return {
        "amount": format_decimal_plain(budget.amount_amount.normalize()),
        "currency": budget.amount_currency,
        "thresholds": thresholds_text(budget.thresholds),
        "is_active": budget.is_active,
    }


def read_budget_form(*, amount: str, currency: str, thresholds: str, is_active: bool) -> dict[str, Any]:
    return {
        "amount": amount.strip(),
        "currency": currency.strip().upper(),
        "thresholds": thresholds.strip(),
        "is_active": is_active,
    }


def budget_error(exc: Exception) -> dict[str, str]:
    """The error a budget form shows: a code and field (``self_service_off`` for a refusal)."""
    if isinstance(exc, PermissionDeniedError):
        return {"code": "self_service_off", "field": ""}
    if isinstance(exc, AppError):
        return {"code": exc.code, "field": exc.field or ""}
    raise exc


async def currency_choices(db: AsyncSession, current: str | None) -> list[str]:
    """The active currencies, plus the one a budget already holds."""
    from specivo.services.currency_service import CurrencyService

    return [c.code for c in await CurrencyService().list_currencies(db, active_only=True, include=current)]


async def reporting_currency(db: AsyncSession) -> str:
    from specivo.services.currency_service import CurrencyService

    return await CurrencyService().reporting_currency_for(db, None)


__all__ = [
    "BUDGET_FORM_FIELDS",
    "account_budget_views",
    "blank_budget_form",
    "budget_error",
    "budget_form_from",
    "budget_view",
    "budget_views",
    "currency_choices",
    "read_budget_form",
    "reporting_currency",
    "thresholds_text",
]
