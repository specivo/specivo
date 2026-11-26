"""AI spend budgets API.

- ``/my/ai-spend-budgets/``: budgets on the caller's own personal billing
  accounts, with this month's spend. Setting and changing them needs the
  instance setting ``ai_subscriptions_self_service`` (as for the accounts).
  Account-wide, so a project-limited API key is refused.
- ``/admin/ai-spend-budgets/``: administrators, for every budget: on instance
  accounts, on a payer as a whole (``employer`` or ``employee``) and on
  people's personal accounts. Changes are audited.

Each response carries the month so far (``spend``, ``percent``, the
thresholds ``reached`` and ``alerted``). Alerts are mailed by the hourly
budget check, once per threshold and month.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.core.exceptions import NotFoundError
from specivo.core.security import get_account_user
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_spend_budget import AiSpendBudget
from specivo.models.user import User
from specivo.schemas.ai_spend_budget import (
    AiSpendBudgetAdminCreate,
    AiSpendBudgetCreate,
    AiSpendBudgetOut,
    AiSpendBudgetUpdate,
)
from specivo.schemas.common import PaginatedResponse
from specivo.services.ai_spend_budget_service import AiSpendBudgetService, BudgetInput

router = APIRouter(tags=["ai-spend-budgets"])
_service = AiSpendBudgetService()


async def _out(db: DbSession, rows: list[AiSpendBudget]) -> list[AiSpendBudgetOut]:
    accounts = await _service.accounts(db, rows)
    progress = await _service.progress(db, rows)
    return [
        AiSpendBudgetOut.from_row(
            r, accounts.get(r.billing_account_id) if r.billing_account_id is not None else None, progress.get(r.id)
        )
        for r in rows
    ]


async def _one(db: DbSession, budget: AiSpendBudget) -> AiSpendBudgetOut:
    await db.refresh(budget)
    [out] = await _out(db, [budget])
    return out


async def _own(db: DbSession, user: User, budget_id: int) -> AiSpendBudget:
    """A budget on *user*'s own personal account; 404 for any other (administrators included)."""
    budget = await _service.get(db, budget_id)
    account = await _service.account_of(db, budget)
    if account is None or account.owner_user_id != user.id:
        raise NotFoundError("AI spend budget not found")
    return budget


# ---------------------------------------------------------------------------
# My budgets
# ---------------------------------------------------------------------------


@router.get("/my/ai-spend-budgets/", response_model=list[AiSpendBudgetOut])
async def list_my_budgets(db: DbSession, current_user: User = Depends(get_account_user)) -> list[AiSpendBudgetOut]:
    """The budgets on the caller's own billing accounts, with this month's spend."""
    return await _out(db, await _service.list_own(db, current_user))


@router.post("/my/ai-spend-budgets/", response_model=AiSpendBudgetOut, status_code=201)
async def create_my_budget(
    data: AiSpendBudgetCreate, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiSpendBudgetOut:
    """Set a monthly budget on one of the caller's own accounts.

    404 for an account that is not the caller's (company accounts included:
    administrators budget those); 403 while self-service is off; 409
    ``ai_spend_budget_exists`` when the account has one; 422
    ``ai_spend_budget_invalid``, ``currency_unknown`` or ``currency_inactive``
    with the field.
    """
    account = await db.get(AiBillingAccount, data.billing_account_id)
    if account is None or account.owner_user_id != current_user.id:
        raise NotFoundError("AI billing account not found")
    budget = await _service.create(
        db,
        BudgetInput(
            billing_account_id=data.billing_account_id,
            amount=data.amount,
            currency=data.currency,
            thresholds=data.thresholds,
            is_active=data.is_active,
        ),
        actor=current_user,
        request=request,
    )
    return await _one(db, budget)


@router.get("/my/ai-spend-budgets/{budget_id}/", response_model=AiSpendBudgetOut)
async def get_my_budget(
    budget_id: int, db: DbSession, current_user: User = Depends(get_account_user)
) -> AiSpendBudgetOut:
    return await _one(db, await _own(db, current_user, budget_id))


@router.patch("/my/ai-spend-budgets/{budget_id}/", response_model=AiSpendBudgetOut)
async def update_my_budget(
    budget_id: int,
    data: AiSpendBudgetUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_account_user),
) -> AiSpendBudgetOut:
    """Change the amount, currency, thresholds or state of one of the caller's budgets."""
    budget = await _own(db, current_user, budget_id)
    budget = await _service.update(db, budget, data.changes(), actor=current_user, request=request)
    return await _one(db, budget)


@router.delete("/my/ai-spend-budgets/{budget_id}/", status_code=204)
async def delete_my_budget(
    budget_id: int, request: Request, db: DbSession, current_user: User = Depends(get_account_user)
) -> Response:
    """Remove one of the caller's budgets, with its alert history."""
    budget = await _own(db, current_user, budget_id)
    await _service.delete(db, budget, actor=current_user, request=request)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


@router.get("/admin/ai-spend-budgets/", response_model=PaginatedResponse[AiSpendBudgetOut])
async def admin_list_budgets(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedResponse[AiSpendBudgetOut]:
    """Every budget, payer budgets first, with this month's spend (admin only)."""
    rows, total = await _service.list_all(db, offset=offset, limit=limit)
    return PaginatedResponse[AiSpendBudgetOut](
        total_count=total, offset=offset, limit=limit, items=await _out(db, rows)
    )


@router.post("/admin/ai-spend-budgets/", response_model=AiSpendBudgetOut, status_code=201)
async def admin_create_budget(
    data: AiSpendBudgetAdminCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiSpendBudgetOut:
    """Set a budget on any billing account (``billing_account_id``) or payer (``payer``). Audited."""
    budget = await _service.create(
        db,
        BudgetInput(
            billing_account_id=data.billing_account_id,
            payer=data.payer,
            amount=data.amount,
            currency=data.currency,
            thresholds=data.thresholds,
            is_active=data.is_active,
        ),
        actor=current_user,
        request=request,
    )
    return await _one(db, budget)


@router.get("/admin/ai-spend-budgets/{budget_id}/", response_model=AiSpendBudgetOut)
async def admin_get_budget(
    budget_id: int, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiSpendBudgetOut:
    return await _one(db, await _service.get(db, budget_id))


@router.patch("/admin/ai-spend-budgets/{budget_id}/", response_model=AiSpendBudgetOut)
async def admin_update_budget(
    budget_id: int,
    data: AiSpendBudgetUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiSpendBudgetOut:
    """Change any budget (admin only). Audited for company and payer budgets and other people's."""
    budget = await _service.get(db, budget_id)
    budget = await _service.update(db, budget, data.changes(), actor=current_user, request=request)
    return await _one(db, budget)


@router.delete("/admin/ai-spend-budgets/{budget_id}/", status_code=204)
async def admin_delete_budget(
    budget_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> Response:
    """Remove any budget (admin only). Audited for company and payer budgets and other people's."""
    budget = await _service.get(db, budget_id)
    await _service.delete(db, budget, actor=current_user, request=request)
    return Response(status_code=204)
