"""Agent cost tracking API — token logs, cost summaries, billing rates/reports."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import Project
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
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["agent-costs"])
_cost_service = AgentCostService()
_billing_service = BillingRateService()
_project_service = ProjectService()
_issue_service = IssueService()


async def _require_project_permission(
    db: AsyncSession, project_key: str, user: User, permission: Permission, request: Request, message: str
) -> Project:
    """Return the project *user* may open and holds *permission* in; 404 or 403 otherwise."""
    project = await _project_service.get_by_key(db, project_key)
    await _project_service.require_project_access(db, project, user)
    if not await check_permission(user, project.id, permission, db, request=request):
        raise PermissionDeniedError(message)
    return project


# Billing rates are project settings: there is no separate billing permission,
# so changing them takes ``manage_project``. Reading them takes
# ``view_time_entries``, like the billing report that applies them.
_MANAGE_RATES_DENIED = "You do not have permission to manage billing rates in this project"
_VIEW_RATES_DENIED = "You do not have permission to view billing rates in this project"


# ---------------------------------------------------------------------------
# Token logging (not project-scoped)
# ---------------------------------------------------------------------------


@router.post(
    "/agent-token-logs/",
    response_model=AgentTokenLogOut,
    status_code=status.HTTP_201_CREATED,
)
async def log_tokens(
    data: AgentTokenLogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> AgentTokenLogOut:
    log = await _cost_service.log_tokens(db, data, current_user)
    return AgentTokenLogOut.model_validate(log)


# ---------------------------------------------------------------------------
# Cost summaries (project-scoped)
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/agent-costs/",
    response_model=list[CostSummaryItem],
)
async def get_cost_summary(
    project_key: str,
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> list[CostSummaryItem]:
    """Agent costs by model over the issues of the project the caller may see."""
    project = await _project_service.get_by_key(db, project_key)
    await _project_service.require_project_access(db, project, current_user)
    items = await _cost_service.get_cost_summary(db, project, current_user, date_from, date_to)
    return [CostSummaryItem(**item) for item in items]


@router.get(
    "/projects/{project_key}/agent-costs/by-issue/{issue_ref}/",
    response_model=IssueCostOut,
)
async def get_cost_per_issue(
    project_key: str,
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> IssueCostOut:
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)
    costs = await _cost_service.get_cost_per_issue(db, issue.id)
    return IssueCostOut(issue_ref=issue_ref, **costs)


# ---------------------------------------------------------------------------
# Billing rates (project-scoped)
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_key}/billing-rates/",
    response_model=BillingRateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_billing_rate(
    project_key: str,
    data: BillingRateCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> BillingRateOut:
    project = await _require_project_permission(
        db, project_key, current_user, Permission.MANAGE_PROJECT, request, _MANAGE_RATES_DENIED
    )
    rate = await _billing_service.create_rate(db, project.id, data)
    return BillingRateOut.model_validate(rate)


@router.get(
    "/projects/{project_key}/billing-rates/",
    response_model=list[BillingRateOut],
)
async def list_billing_rates(
    project_key: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> list[BillingRateOut]:
    project = await _require_project_permission(
        db, project_key, current_user, Permission.VIEW_TIME_ENTRIES, request, _VIEW_RATES_DENIED
    )
    rates = await _billing_service.list_rates(db, project.id)
    return [BillingRateOut.model_validate(r) for r in rates]


@router.patch(
    "/projects/{project_key}/billing-rates/{rate_id}/",
    response_model=BillingRateOut,
)
async def update_billing_rate(
    project_key: str,
    rate_id: int,
    data: BillingRateUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> BillingRateOut:
    project = await _require_project_permission(
        db, project_key, current_user, Permission.MANAGE_PROJECT, request, _MANAGE_RATES_DENIED
    )
    rate = await _billing_service.update_rate(db, rate_id, project.id, data)
    return BillingRateOut.model_validate(rate)


@router.delete(
    "/projects/{project_key}/billing-rates/{rate_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_billing_rate(
    project_key: str,
    rate_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> Response:
    project = await _require_project_permission(
        db, project_key, current_user, Permission.MANAGE_PROJECT, request, _MANAGE_RATES_DENIED
    )
    await _billing_service.delete_rate(db, rate_id, project.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Billing report (project-scoped)
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/billing-report/",
    response_model=BillingReportOut,
)
async def get_billing_report(
    project_key: str,
    request: Request,
    date_from: date = Query(...),
    date_to: date = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> BillingReportOut:
    """Billable time in the period at the effective rates; requires ``view_time_entries`` (403 without it)."""
    project = await _project_service.get_by_key(db, project_key)
    await _project_service.require_project_access(db, project, current_user)
    if not await check_permission(current_user, project.id, Permission.VIEW_TIME_ENTRIES, db, request=request):
        raise PermissionDeniedError("You do not have permission to view time entries in this project")
    report = await _billing_service.billable_report(db, project, current_user, date_from, date_to)
    return BillingReportOut(**report)
