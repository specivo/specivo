"""Agent cost tracking API — token logs, cost summaries, billing rates/reports."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.agent_cost import (
    AgentTokenLogCreate,
    AgentTokenLogOut,
    BillingRateCreate,
    BillingRateOut,
    BillingRateUpdate,
    BillingReportOut,
    CostSummaryItem,
    IssueCostOut,
)
from specivo.services.agent_cost_service import AgentCostService
from specivo.services.billing_rate_service import BillingRateService
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["agent-costs"])
_cost_service = AgentCostService()
_billing_service = BillingRateService()
_project_service = ProjectService()
_issue_service = IssueService()


# ---------------------------------------------------------------------------
# Token logging (not project-scoped)
# ---------------------------------------------------------------------------


@router.post(
    "/agent-token-logs",
    response_model=AgentTokenLogOut,
    status_code=status.HTTP_201_CREATED,
)
async def log_tokens(
    data: AgentTokenLogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentTokenLogOut:
    log = await _cost_service.log_tokens(db, data)
    return AgentTokenLogOut.model_validate(log)


# ---------------------------------------------------------------------------
# Cost summaries (project-scoped)
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/agent-costs",
    response_model=list[CostSummaryItem],
)
async def get_cost_summary(
    project_key: str,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CostSummaryItem]:
    project = await _project_service.get_by_key(db, project_key)
    items = await _cost_service.get_cost_summary(db, project.id, date_from, date_to)
    return [CostSummaryItem(**item) for item in items]


@router.get(
    "/projects/{project_key}/agent-costs/by-issue/{issue_ref}",
    response_model=IssueCostOut,
)
async def get_cost_per_issue(
    project_key: str,
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IssueCostOut:
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)
    costs = await _cost_service.get_cost_per_issue(db, issue.id)
    return IssueCostOut(issue_ref=issue_ref, **costs)


# ---------------------------------------------------------------------------
# Billing rates (project-scoped)
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_key}/billing-rates",
    response_model=BillingRateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_billing_rate(
    project_key: str,
    data: BillingRateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BillingRateOut:
    project = await _project_service.get_by_key(db, project_key)
    rate = await _billing_service.create_rate(db, project.id, data)
    return BillingRateOut.model_validate(rate)


@router.get(
    "/projects/{project_key}/billing-rates",
    response_model=list[BillingRateOut],
)
async def list_billing_rates(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[BillingRateOut]:
    project = await _project_service.get_by_key(db, project_key)
    rates = await _billing_service.list_rates(db, project.id)
    return [BillingRateOut.model_validate(r) for r in rates]


@router.patch(
    "/projects/{project_key}/billing-rates/{rate_id}",
    response_model=BillingRateOut,
)
async def update_billing_rate(
    project_key: str,
    rate_id: int,
    data: BillingRateUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BillingRateOut:
    project = await _project_service.get_by_key(db, project_key)
    rate = await _billing_service.update_rate(db, rate_id, project.id, data)
    return BillingRateOut.model_validate(rate)


@router.delete(
    "/projects/{project_key}/billing-rates/{rate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_billing_rate(
    project_key: str,
    rate_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    project = await _project_service.get_by_key(db, project_key)
    await _billing_service.delete_rate(db, rate_id, project.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Billing report (project-scoped)
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/billing-report",
    response_model=BillingReportOut,
)
async def get_billing_report(
    project_key: str,
    date_from: date = Query(...),
    date_to: date = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BillingReportOut:
    project = await _project_service.get_by_key(db, project_key)
    report = await _billing_service.billable_report(db, project.id, date_from, date_to)
    return BillingReportOut(**report)
