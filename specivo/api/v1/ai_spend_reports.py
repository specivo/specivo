"""AI spend reports API: a project's report, the instance report (administrators) and their CSV export.

- ``GET /projects/{key}/ai-usage/report/``: the project's AI usage the caller
  may read, grouped and totalled in one currency.
- ``GET /ai-usage/report/``: the whole instance; administrators only.
- ``GET /projects/{key}/ai-usage/report/people/`` and ``GET /ai-usage/report/people/``:
  a page of the people the report's person filter may name (the search behind it).
- ``GET /projects/{key}/ai-usage/report/models/`` and ``GET /ai-usage/report/models/``:
  a page of model names for the report's model filter.

Both reports take ``format=csv`` for a CSV file of every row; the JSON answer
is a page of the rows (``offset``, ``limit``) with the totals of all of them. Included before the
AI usage router, so ``/ai-usage/report/`` is matched before
``/ai-usage/{entry_id}/``. See :mod:`specivo.services.ai_spend_report_service`
for the measures, the seat allocation and who sees seat cost.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DbSession
from specivo.core.exceptions import NotFoundError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.ai_spend_report import SpendModelSuggestionOut, SpendReportOut
from specivo.schemas.ai_usage import AiUsageCostType
from specivo.schemas.project import AssigneeSuggestionOut
from specivo.services.ai_spend_report_service import (
    AiSpendReportService,
    ReportFilters,
    default_range,
    parse_group_by,
    report_csv,
)
from specivo.services.ai_usage_service import AiUsageService, ModelSuggestion
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["ai-usage"])
_service = AiSpendReportService()
_projects = ProjectService()

#: A filter value selecting entries without a project, issue or branch.
NONE_VALUE = "-"

Period = Literal["day", "week", "month"]
Preset = Literal["7d", "30d", "this_month", "last_month"]

#: Rows of a JSON report page by default, and at most (the CSV has every row).
JSON_ROWS_DEFAULT = 500
JSON_ROWS_MAX = 5000

_OFFSET = Query(0, ge=0, description="JSON: rows to skip (the totals always cover every row)")
_LIMIT = Query(JSON_ROWS_DEFAULT, ge=1, le=JSON_ROWS_MAX, description="JSON: rows per page; `row_count` has them all")

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
    project_filter: str | None = None,
    issue: str | None = None,
    branch: str | None = None,
    offset: int = 0,
    limit: int = JSON_ROWS_DEFAULT,
) -> SpendReportOut | Response:
    await enforce_ai_report_rate_limit(request, response, user)
    first, last = default_range(preset)
    narrowing = await _narrowing(db, user, project, project_filter, issue, branch)
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
        filters=ReportFilters(
            user_id=user_id, tool=tool, model=model, provider=provider, cost_type=cost_type, **narrowing
        ),
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
    return SpendReportOut.from_report(report, offset=offset, limit=limit)


async def _narrowing(
    db: DbSession,
    user: User,
    project: Project | None,
    project_filter: str | None,
    issue: str | None,
    branch: str | None,
) -> dict[str, Any]:
    """The project (instance report), issue and branch filters; ``-`` selects entries without one.

    An unknown project, or an issue the caller cannot see (or of another
    project), matches nothing.
    """
    out: dict[str, Any] = {}
    selected: Project | None = None
    if project is None and project_filter:
        if project_filter.strip() == NONE_VALUE:
            out["unassigned"] = True
        else:
            try:
                selected = await _projects.get_by_key(db, project_filter.strip().upper())
                out["project_id"] = selected.id
            except NotFoundError:
                out["project_id"] = -1
    if issue:
        if issue.strip() == NONE_VALUE:
            out["without_issue"] = True
        else:
            out["issue_id"] = -1
            try:
                found = await IssueService().get_by_display_key(db, issue.strip().upper(), user=user)
            except NotFoundError:
                found = None
            within = project or selected
            if found is not None and (within is None or found.project_id == within.id):
                out["issue_id"] = found.id
    if branch:
        if branch.strip() == NONE_VALUE:
            out["without_branch"] = True
        else:
            out["branch"] = branch.strip()
    return out


_ISSUE = Query(None, max_length=50, description="An issue key, or '-' for entries without an issue")
_BRANCH = Query(None, max_length=255, description="A git branch, or '-' for entries without one")


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
    issue: str | None = _ISSUE,
    branch: str | None = _BRANCH,
    sort: Literal["key", "spend"] = Query("key", description="By the grouping key (default), or by total spend"),
    format: Literal["json", "csv"] = Query("json"),  # noqa: A002
    offset: int = _OFFSET,
    limit: int = _LIMIT,
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
        issue=issue,
        branch=branch,
        offset=offset,
        limit=limit,
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
    project: str | None = Query(None, max_length=50, description="A project key, or '-' for unassigned entries"),
    issue: str | None = _ISSUE,
    branch: str | None = _BRANCH,
    sort: Literal["key", "spend"] = Query("key"),
    format: Literal["json", "csv"] = Query("json"),  # noqa: A002
    offset: int = _OFFSET,
    limit: int = _LIMIT,
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
        project_filter=project,
        issue=issue,
        branch=branch,
        offset=offset,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# The person filter's search
# ---------------------------------------------------------------------------

_Q = Query(None, max_length=100, description="Substring of the login or display name")
_PEOPLE_OFFSET = Query(0, ge=0, description="Matches to skip, to page through them")
_PEOPLE_LIMIT = Query(20, ge=1, le=50)


def _people_out(users: list[User]) -> list[AssigneeSuggestionOut]:
    return [
        AssigneeSuggestionOut(
            id=u.id,
            login=u.login,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            avatar_color=str((u.preferences or {}).get("avatar_color") or ""),
        )
        for u in users
    ]


@router.get("/projects/{project_key}/ai-usage/report/people/", response_model=list[AssigneeSuggestionOut])
async def project_ai_spend_people(
    project_key: str,
    db: DbSession,
    q: str | None = _Q,
    offset: int = _PEOPLE_OFFSET,
    limit: int = _PEOPLE_LIMIT,
    current_user: User = Depends(get_current_user),
) -> list[AssigneeSuggestionOut]:
    """Search the people with AI usage in the project that you may read: the report's person filter.

    Never a system account, never someone whose entries you cannot see.
    Ordered by display name and paged with ``offset``/``limit``; a page
    shorter than ``limit`` is the last.
    """
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, current_user)
    return _people_out(await AiUsageService().search_people(db, current_user, project, q, offset=offset, limit=limit))


@router.get("/ai-usage/report/people/", response_model=list[AssigneeSuggestionOut])
async def instance_ai_spend_people(
    db: DbSession,
    q: str | None = _Q,
    offset: int = _PEOPLE_OFFSET,
    limit: int = _PEOPLE_LIMIT,
    current_user: User = Depends(get_current_user),
) -> list[AssigneeSuggestionOut]:
    """Search everyone with AI usage or a subscription: the instance report's person filter (administrators only)."""
    return _people_out(await AiUsageService().search_people(db, current_user, None, q, offset=offset, limit=limit))


# ---------------------------------------------------------------------------
# The model filter's search
# ---------------------------------------------------------------------------

_MODEL_Q = Query(None, max_length=200, description="Substring of the model name or an alias")


def _models_out(models: list[ModelSuggestion]) -> list[SpendModelSuggestionOut]:
    return [
        SpendModelSuggestionOut(id=m.model, name=m.model, provider=m.provider, aliases=list(m.aliases)) for m in models
    ]


@router.get("/projects/{project_key}/ai-usage/report/models/", response_model=list[SpendModelSuggestionOut])
async def project_ai_spend_models(
    project_key: str,
    db: DbSession,
    q: str | None = _MODEL_Q,
    offset: int = _PEOPLE_OFFSET,
    limit: int = _PEOPLE_LIMIT,
    current_user: User = Depends(get_current_user),
) -> list[SpendModelSuggestionOut]:
    """Search model names for the project report's model filter: the price list's and the project's usage you may read.

    Ordered by name and paged with ``offset``/``limit``; a page shorter than
    ``limit`` is the last.
    """
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, current_user)
    return _models_out(await AiUsageService().search_models(db, current_user, project, q, offset=offset, limit=limit))


@router.get("/ai-usage/report/models/", response_model=list[SpendModelSuggestionOut])
async def instance_ai_spend_models(
    db: DbSession,
    q: str | None = _MODEL_Q,
    offset: int = _PEOPLE_OFFSET,
    limit: int = _PEOPLE_LIMIT,
    current_user: User = Depends(get_current_user),
) -> list[SpendModelSuggestionOut]:
    """Search model names for the instance report's model filter (administrators only)."""
    return _models_out(await AiUsageService().search_models(db, current_user, None, q, offset=offset, limit=limit))
