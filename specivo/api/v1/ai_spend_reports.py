"""AI spend reports API: a project's report, the instance report (administrators) and their CSV export.

- ``GET /projects/{key}/ai-usage/report/``: the project's AI usage the caller
  may read, grouped and totalled in one currency.
- ``GET /ai-usage/report/``: the whole instance; administrators only.

Both take ``format=csv`` for a CSV file of the same rows. Included before the
AI usage router, so ``/ai-usage/report/`` is matched before
``/ai-usage/{entry_id}/``. See :mod:`specivo.services.ai_spend_report_service`
for the measures, the seat allocation and who sees seat cost.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DbSession
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.ai_spend_report import SpendReportOut
from specivo.schemas.ai_usage import AiUsageCostType
from specivo.services.ai_spend_report_service import (
    AiSpendReportService,
    ReportFilters,
    default_range,
    parse_group_by,
    report_csv,
)
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["ai-usage"])
_service = AiSpendReportService()
_projects = ProjectService()

Period = Literal["day", "week", "month"]
Preset = Literal["7d", "30d", "this_month", "last_month"]

_GROUP_BY = Query(
    "user",
    description="Comma-separated: user, project, issue, tool, provider, model, cost_type, branch (may be empty)",
)


async def _report(
    request: Request,
    response: Response,
    db: DbSession,
    user: User,
    project: Project | None,
    *,
    from_date: date | None,
    to_date: date | None,
    preset: str | None,
    group_by: str,
    period: str | None,
    currency: str | None,
    rate_policy: str | None,
    include_seat_cost: bool,
    user_id: int | None,
    tool: str | None,
    model: str | None,
    provider: str | None,
    cost_type: str | None,
    sort: str,
    fmt: str,
) -> SpendReportOut | Response:
    await enforce_ai_report_rate_limit(request, response, user)
    first, last = default_range(preset)
    report = await _service.report(
        db,
        user,
        project=project,
        date_from=from_date or first,
        date_to=to_date or last,
        group_by=parse_group_by(group_by),
        period=period,
        currency=currency,
        rate_policy=rate_policy,
        include_seat_cost=include_seat_cost,
        filters=ReportFilters(user_id=user_id, tool=tool, model=model, provider=provider, cost_type=cost_type),
        sort=sort,
    )
    if fmt == "csv":
        scope = project.key if project is not None else "instance"
        name = f"ai-spend-{scope}-{report.date_from.isoformat()}-{report.date_to.isoformat()}.csv"
        return Response(
            content=report_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return SpendReportOut.from_report(report)


@router.get(
    "/projects/{project_key}/ai-usage/report/",
    response_model=SpendReportOut,
    responses={429: {"description": "rate_limit_exceeded: the per-user AI report budget is spent; see Retry-After"}},
)
async def project_ai_spend_report(
    project_key: str,
    request: Request,
    response: Response,
    db: DbSession,
    from_date: date | None = Query(None, description="First UTC day; default from the preset"),
    to_date: date | None = Query(None, description="Last UTC day (included)"),
    preset: Preset | None = Query(None, description="Period preset when no dates are given; default 30d"),
    group_by: str = _GROUP_BY,
    period: Period | None = Query(
        None, description="Also group by day, ISO week or month (coarsened past 120 periods; see `period`)"
    ),
    currency: str | None = Query(None, max_length=3, description="Default: the project's reporting currency"),
    rate_policy: str | None = Query(None, description="'entry_date' (default), 'latest' or a date YYYY-MM-DD"),
    include_seat_cost: bool = Query(True, description="Allocate subscription fees (if you may see them)"),
    user_id: int | None = Query(None),
    tool: str | None = Query(None, max_length=50),
    model: str | None = Query(None, max_length=200),
    provider: str | None = Query(None, max_length=50),
    cost_type: AiUsageCostType | None = Query(None),
    sort: Literal["key", "spend"] = Query("key", description="By the grouping key (default), or by total spend"),
    format: Literal["json", "csv"] = Query("json"),  # noqa: A002
    current_user: User = Depends(get_current_user),
) -> SpendReportOut | Response:
    """The project's AI spend: rows from the entries you may read, in one reporting currency.

    Seat cost (subscription fees allocated to the entries that used them) is
    included only for administrators and for viewers who read every entry of
    the project in the period; otherwise ``seat_cost_hidden`` is true.
    """
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, current_user)
    return await _report(
        request,
        response,
        db,
        current_user,
        project,
        from_date=from_date,
        to_date=to_date,
        preset=preset,
        group_by=group_by,
        period=period,
        currency=currency,
        rate_policy=rate_policy,
        include_seat_cost=include_seat_cost,
        user_id=user_id,
        tool=tool,
        model=model,
        provider=provider,
        cost_type=cost_type,
        sort=sort,
        fmt=format,
    )


@router.get(
    "/ai-usage/report/",
    response_model=SpendReportOut,
    responses={429: {"description": "rate_limit_exceeded: the per-user AI report budget is spent; see Retry-After"}},
)
async def instance_ai_spend_report(
    request: Request,
    response: Response,
    db: DbSession,
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    preset: Preset | None = Query(None),
    group_by: str = _GROUP_BY,
    period: Period | None = Query(None),
    currency: str | None = Query(None, max_length=3, description="Default: the instance reporting currency"),
    rate_policy: str | None = Query(None),
    include_seat_cost: bool = Query(True),
    user_id: int | None = Query(None),
    tool: str | None = Query(None, max_length=50),
    model: str | None = Query(None, max_length=200),
    provider: str | None = Query(None, max_length=50),
    cost_type: AiUsageCostType | None = Query(None),
    sort: Literal["key", "spend"] = Query("key"),
    format: Literal["json", "csv"] = Query("json"),  # noqa: A002
    current_user: User = Depends(get_current_user),
) -> SpendReportOut | Response:
    """The whole instance's AI spend; administrators only (403 otherwise).

    Includes unallocated seat cost: fees no entry carries, when no tool, model
    or provider filter (or a cost type other than subscription) is applied.
    """
    return await _report(
        request,
        response,
        db,
        current_user,
        None,
        from_date=from_date,
        to_date=to_date,
        preset=preset,
        group_by=group_by,
        period=period,
        currency=currency,
        rate_policy=rate_policy,
        include_seat_cost=include_seat_cost,
        user_id=user_id,
        tool=tool,
        model=model,
        provider=provider,
        cost_type=cost_type,
        sort=sort,
        fmt=format,
    )
