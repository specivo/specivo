"""MCP tool implementation for AI spend budgets: ``specivo_ai_spend_budgets``.

Like ``specivo.mcp.ai_billing_account_tools``: the function takes an explicit
session and user, delegates every rule to :class:`AiSpendBudgetService` (who
may see and set which budget, the self-service setting, the audit) and
returns text for the agent.

Budgets belong to an account or a payer, not to a project, so a
project-limited key is refused, as on the REST routes. Administrators list
every budget; anyone else lists the budgets on their own accounts.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.mcp.tools._common import _log_tool
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_spend_budget import AiSpendBudget
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService
from specivo.services.ai_spend_budget_service import (
    BUDGET_FIELDS,
    PAYER_BUDGET_NAMES,
    TARGET_ACCOUNT,
    TARGET_PAYER,
    AiSpendBudgetService,
    BudgetInput,
    BudgetProgress,
)
from specivo.services.api_key_scope import is_limited_key
from specivo.services.security_audit_service import AuditEvent

_service = AiSpendBudgetService()
_accounts = AiBillingAccountService()

AI_SPEND_BUDGET_OPS = ("list", "create", "update", "delete")


def _require_unlimited_key() -> None:
    if is_limited_key():
        raise PermissionDeniedError(
            _(
                "AI spend budgets belong to a billing account or a payer, not to a project: "
                "connect with an unlimited API key to manage them."
            )
        )


def _line(budget: AiSpendBudget, account: AiBillingAccount | None, progress: BudgetProgress | None) -> str:
    if account is not None:
        whose = "company account" if account.owner_user_id is None else "personal account"
        target = f"{account.name} [{account.slug}] ({whose} #{account.id})"
    else:
        target = f"{PAYER_BUDGET_NAMES.get(budget.payer or '', budget.payer)} (payer {budget.payer})"
    parts = [
        f"#{budget.id} {target}",
        f"monthly {budget.amount.quantize().amount_str} {budget.amount_currency}",
        "alerts at " + ", ".join(f"{t}%" for t in budget.thresholds),
    ]
    if progress is not None:
        month = progress.period_start.strftime("%Y-%m")
        parts.append(
            f"{month} so far {progress.spend.quantize().amount_str} {progress.spend.currency} ({progress.percent}%)"
        )
        if progress.alerted:
            parts.append("alerted at " + ", ".join(f"{t}%" for t in progress.alerted))
        if progress.unconverted:
            parts.append("some spend has no exchange rate and is not counted")
    if not budget.is_active:
        parts.append("OFF")
    return "; ".join(parts)


async def _lines(session: AsyncSession, budgets: Sequence[AiSpendBudget]) -> list[str]:
    accounts = await _service.accounts(session, budgets)
    progress = await _service.progress(session, budgets)
    return [
        _line(b, accounts.get(b.billing_account_id) if b.billing_account_id is not None else None, progress.get(b.id))
        for b in budgets
    ]


async def _account(session: AsyncSession, caller: User, value: str | int) -> AiBillingAccount:
    """The account *value* names (an id, or a slug: the caller's own first, then an instance account)."""
    text = str(value).strip()
    account: AiBillingAccount | None
    if text.isdecimal():
        account = await session.get(AiBillingAccount, int(text))
    else:
        account = await _accounts.find_by_slug(session, text, user_id=caller.id, active_only=False)
    if account is None or not _accounts.can_view(caller, account):
        raise NotFoundError(f"No AI billing account '{text}'")
    return account


def _target(budget: AiSpendBudget) -> str:
    return TARGET_ACCOUNT if budget.billing_account_id is not None else TARGET_PAYER


def _changes(values: dict[str, Any]) -> dict[str, Any]:
    out = {name: values[name] for name in BUDGET_FIELDS if values.get(name) is not None}
    if not out:
        raise ValidationError(
            message=f"op='update' needs at least one field to change: {', '.join(BUDGET_FIELDS)}", field="op"
        )
    return out


async def _log_change(session: AsyncSession, caller: User, op: str, budget_id: int, target: str) -> None:
    details = {"op": op, "budget_ids": [budget_id], "target": target}
    await _log_tool(session, caller, AuditEvent.AI_SPEND_BUDGETS_CHANGED, "ai_spend_budgets", details)


async def _ai_spend_budgets(
    session: AsyncSession,
    caller: User,
    op: str,
    *,
    budget_id: int | None = None,
    billing_account: str | int | None = None,
    payer: str | None = None,
    values: dict[str, Any] | None = None,
) -> str:
    """List, set, change or remove AI spend budgets; see ``specivo_ai_spend_budgets``."""
    if op not in AI_SPEND_BUDGET_OPS:
        raise ValidationError(
            message=f"Invalid op '{op}'. Must be one of: {', '.join(AI_SPEND_BUDGET_OPS)}", field="op"
        )
    _require_unlimited_key()
    values = values or {}
    if op == "list":
        budgets = await _service.list_visible(session, caller)
        await _log_tool(session, caller, AuditEvent.RESOURCE_ACCESS, "ai_spend_budgets", {"op": "list"})
        lines = [f"AI spend budgets ({len(budgets)}):"]
        lines += [f"  {line}" for line in await _lines(session, budgets)] or ["  (none)"]
        return "\n".join(lines)
    if op == "create":
        if (billing_account is None or str(billing_account).strip() == "") == (not payer):
            raise ValidationError(
                message="op='create' needs billing_account or payer, not both", field="billing_account"
            )
        account = await _account(session, caller, billing_account) if billing_account not in (None, "") else None
        missing = [name for name in ("amount", "currency") if values.get(name) in (None, "")]
        if missing:
            raise ValidationError(message=f"op='create' needs {', '.join(missing)}", field=missing[0])
        budget = await _service.create(
            session,
            BudgetInput(
                billing_account_id=account.id if account is not None else None,
                payer=payer if account is None else None,
                amount=values["amount"],
                currency=values["currency"],
                thresholds=values.get("thresholds"),
                is_active=values.get("is_active") is not False,
            ),
            actor=caller,
        )
        await session.flush()
        await session.refresh(budget)
        await _log_change(session, caller, op, budget.id, _target(budget))
        [line] = await _lines(session, [budget])
        return f"Set {line}"

    if budget_id is None:
        raise ValidationError(message=f"op='{op}' needs budget_id", field="budget_id")
    budget = await _service.get_visible(session, caller, budget_id)
    if op == "delete":
        [line] = await _lines(session, [budget])
        removed, target = budget.id, _target(budget)
        await _service.delete(session, budget, actor=caller)
        await _log_change(session, caller, op, removed, target)
        return f"Removed {line}"
    budget = await _service.update(session, budget, _changes(values), actor=caller)
    await session.flush()
    await session.refresh(budget)
    await _log_change(session, caller, op, budget.id, _target(budget))
    [line] = await _lines(session, [budget])
    return f"Updated {line}"
