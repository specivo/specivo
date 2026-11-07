"""Web dashboard and notifications pages."""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlencode

from babel.numbers import format_compact_decimal, format_decimal
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import ColumnElement, and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import DB_SESSION
from specivo.core.i18n import format_date, get_locale, ngettext
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.models.version import Version
from specivo.models.wiki import Wiki, WikiContent, WikiPage
from specivo.services import dashboard_service as dash
from specivo.services.ai_spend_report_service import ReportRow
from specivo.services.notification_service import NotificationService
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_current_user_optional, get_templates
from specivo.web.pages.ai_usage import _money_view

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-dashboard"], include_in_schema=False)

_project_svc = ProjectService()
_notif_svc = NotificationService()


async def _notification_actors(db: AsyncSession, notifications: list) -> dict[int, User]:
    """The users who acted in *notifications*, by id: the header's notification list shows their avatars."""
    actor_ids = {n.actor_id for n in notifications[:5]}
    if not actor_ids:
        return {}
    return {u.id: u for u in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars()}


def _visible(clause: ColumnElement[bool] | None) -> ColumnElement[bool]:
    """Return *clause*, or an always-true predicate when there is none (admins)."""
    return true() if clause is None else clause


async def _anonymous_landing(request: Request, db: AsyncSession) -> Response:
    """Where a visitor refused by the dashboard is sent.

    A visitor without any credentials goes to the project list when anonymous
    access is live, that is when the instance switch is on and at least one
    project is readable without an account, so the site's main URL does not
    look private while its public projects are open. Everyone else, and that
    includes a visitor whose session failed (they are asked to sign in again,
    as on every other page), goes to the login screen as before.

    The decision reuses ``resolve_anonymous_principal`` and
    ``ProjectService.accessible_projects_clause``, the same definitions the
    project list applies, and reads both on every request. The answer depends
    on the cookies sent and on settings that can change at any time, so it is
    marked uncacheable either way.
    """
    from specivo.core.security import has_credentials, resolve_anonymous_principal

    target = "/login/"
    if not has_credentials(request):
        try:
            anonymous = await resolve_anonymous_principal(db)
            if anonymous is not None:
                readable = await _project_svc.accessible_projects_clause(db, anonymous)
                if readable is not None and await db.scalar(select(select(Project.id).where(readable).exists())):
                    target = "/projects/"
        except Exception:
            # Fail closed, as ``get_web_reader`` does: a visitor is never sent
            # to the public pages on a guess. The login screen is what the
            # dashboard always answered.
            logger.warning("Could not resolve anonymous access for the home page", exc_info=True)
            target = "/login/"

    response = RedirectResponse(target, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie, Authorization"
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = DB_SESSION,
    period: str = Query("month"),
    who: str = Query("all"),
) -> Response:
    """Render the main dashboard page.

    The dashboard itself is never served anonymously (it is not in
    ``ANONYMOUS_WEB_ROUTES``). Only the destination of a refused visitor
    depends on anonymous access: see :func:`_anonymous_landing`.

    ``period`` picks the spend period (``today``, ``7d``, ``month``) and
    ``who`` the activity filter (``all``, ``agents``, ``people``); the
    switches also reload just their block through the htmx partials.
    """
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return await _anonymous_landing(request, db)
    user = cast("User", user_obj)

    today = datetime.date.today()
    scope = await dash.load_scope(db, user)
    notifications, _ = await _notif_svc.list_notifications(db, user.id, limit=5)
    unread_count = await _notif_svc.get_unread_count(db, user.id)

    my_issues_limit = 10
    my_issues, my_issues_total = await dash.my_open_issues(db, scope, offset=0, limit=my_issues_limit)
    _label_projects(scope, my_issues)

    context: dict[str, Any] = {
        "user": user,
        "active_page": "dashboard",
        "projects": scope.projects[:10],
        "total_projects": len(scope.projects),
        "notifications": notifications,
        "notification_actors": await _notification_actors(db, notifications),
        "unread_count": unread_count,
        "today": today,
        "attention": await dash.attention_counts(db, scope, today),
        "my_issues": my_issues,
        "my_issues_total": my_issues_total,
        "my_issues_offset": 0,
        "my_issues_limit": my_issues_limit,
        "issue_flags": await dash.issue_flags(db, scope, my_issues, today),
        "active_versions": await _active_versions(db, scope, today),
        "active_sprints": await _active_sprints(db, scope, today),
        "project_stats": (
            await _project_svc.load_project_stats(db, [p.id for p in scope.projects[:10]], reader=user)
            if scope.projects
            else {}
        ),
        "recent_wiki_pages": await _recent_wiki(db, scope),
    }
    context.update(await sessions_context(db, scope))
    # Spend is reported in UTC days, like the AI spend reports.
    context.update(await spend_context(db, scope, period, utcnow().date()))
    context.update(await activity_context(db, scope, who))
    return get_templates().TemplateResponse(request, "pages/dashboard.html", context=context)


# ---------------------------------------------------------------------------
# Blocks (shared with the htmx partials)
# ---------------------------------------------------------------------------


def _label_projects(scope: dash.DashboardScope, issues: list[Issue]) -> None:
    """Set ``project_name`` on *issues* from the scope's projects (no query)."""
    names = {p.id: p.name for p in scope.projects}
    for issue in issues:
        issue.project_name = names.get(issue.project_id, "")  # type: ignore[attr-defined]


async def sessions_context(db: AsyncSession, scope: dash.DashboardScope) -> dict[str, Any]:
    sessions, total = await dash.active_sessions(db, scope)
    return {
        "agent_sessions": sessions,
        "agent_sessions_total": total,
        "session_timeout_minutes": max(1, get_settings().agent_session_timeout // 60),
    }


def _compact(value: int, locale: str) -> str:
    return format_compact_decimal(value, locale=locale, fraction_digits=1)


def _hours(value: Decimal, locale: str) -> str:
    return format_decimal(value.quantize(Decimal("0.1")), format="#,##0.0", locale=locale)


def _breakdown(rows: list[ReportRow], dim: str, locale: str, project_url: str = "") -> dict[str, Any]:
    """Up to ``BREAKDOWN_LIMIT`` rows with the largest actual spend, shown in the report's order (by name).

    *project_url* (``{key}`` and the query) links each project row to that project's spend report.
    """

    def spend_of(row: ReportRow) -> Decimal:
        return row.total_spend.total.amount if row.total_spend is not None else Decimal(0)

    ranked = sorted(range(len(rows)), key=lambda i: -spend_of(rows[i]))
    shown = sorted(ranked[: dash.BREAKDOWN_LIMIT])
    peak = max((spend_of(rows[i]) for i in shown), default=Decimal(0))
    items = []
    for i in shown:
        row = rows[i]
        spend = row.total_spend
        amount = spend_of(row)
        if row.seat_only:
            label, kind = _("Seat cost without usage"), "seat"
        elif row.key.get(dim) is None:
            label, kind = (_("No project") if dim == "project" else _("Unknown model")), "none"
        else:
            label, kind = row.labels.get(dim) or str(row.key.get(dim)), "value"
        url = project_url.format(key=label) if project_url and dim == "project" and kind == "value" else ""
        items.append(
            {
                "label": label,
                "url": url,
                "kind": kind,
                "spend": _money_view(spend, locale) if spend is not None else None,
                "unpriced": row.unpriced_entries,
                "percent": int(amount * 100 / peak) if peak and amount > 0 else 0,
            }
        )
    return {"items": items, "more": max(0, len(rows) - len(shown))}


def _report_url(projects: list[Project] | None, first: datetime.date, last: datetime.date, group: str) -> str:
    """The full report behind the spend block, or "" when no single page covers it.

    Administrators: the admin AI spend report. A manager of one project: that
    project's spend report. A manager of several: none (each project row of
    the breakdown links to its own report).
    """
    query = _report_query(first, last, group)
    if projects is None:
        return f"/admin/ai-spend/?{query}"
    if len(projects) == 1:
        return f"/projects/{projects[0].key}/ai-usage/report/?{query}"
    return ""


def _report_query(first: datetime.date, last: datetime.date, group: str) -> str:
    return urlencode({"preset": "custom", "from_date": first.isoformat(), "to_date": last.isoformat(), "group": group})


async def spend_context(
    db: AsyncSession, scope: dash.DashboardScope, period: str, today: datetime.date
) -> dict[str, Any]:
    """The spend block's context; ``spend`` is None for a viewer who does not get the block."""
    projects = await dash.spend_projects(db, scope)
    if projects is not None and not projects:
        return {"spend": None}
    period = period if period in dash.SPEND_PERIODS else "month"
    locale = get_locale()
    summary = await dash.spend_summary(db, scope, period, today, projects)
    totals = summary.totals
    report = summary.by_project
    seat_total = marginal = None
    if report.seat_cost_shown and totals.seat_allocated is not None and totals.seat_unallocated is not None:
        seats = totals.seat_allocated.total + totals.seat_unallocated.total
        if seats.amount > 0:
            seat_total = seats.format(locale)
            marginal = totals.marginal.total.format(locale) if totals.marginal is not None else None
    tokens = totals.tokens
    return {
        "spend": {
            "period": period,
            "periods": dash.SPEND_PERIODS,
            "managed": len(projects) if projects is not None else None,
            "seat_hidden": report.seat_cost_hidden,
            "first": summary.date_from,
            "last": summary.date_to,
            "currency": report.currency,
            "entries": totals.entries,
            "unpriced": totals.unpriced_entries,
            "actual": _money_view(totals.total_spend, locale) if totals.total_spend is not None else None,
            "seats": seat_total,
            "marginal": marginal,
            "seat_shown": report.seat_cost_shown,
            "api_equivalent": _money_view(totals.api_equivalent, locale) if totals.api_equivalent else None,
            "leverage": f"{totals.leverage}×" if totals.leverage is not None else None,
            "tokens": _compact(totals.total_tokens, locale),
            "tokens_detail": {
                "input": _compact(tokens["input"], locale),
                "output": _compact(tokens["output"], locale),
                "cache_write": _compact(tokens["cache_write_5m"] + tokens["cache_write_1h"], locale),
                "cache_read": _compact(tokens["cache_read"], locale),
                "reasoning": _compact(tokens["reasoning"], locale),
            },
            "agent_hours": _hours(summary.agent_hours, locale),
            "people_hours": _hours(summary.people_hours, locale),
            "by_project": _breakdown(
                summary.by_project.rows,
                "project",
                locale,
                "/projects/{key}/ai-usage/report/?" + _report_query(summary.date_from, summary.date_to, "user"),
            ),
            "by_model": _breakdown(summary.by_model.rows, "model", locale),
            "report_url": _report_url(projects, summary.date_from, summary.date_to, "project"),
            "model_report_url": _report_url(projects, summary.date_from, summary.date_to, "model"),
        }
    }


def _group_summary(group: dash.ActivityGroup, locale: str) -> str:
    """What a group holds, in words: "4 updates, 1 comment, 2.5h logged (Development), resolved"."""
    parts: list[str] = []
    if group.count("created"):
        parts.append(_("created"))
    if updates := group.count("updated", "status"):
        parts.append(ngettext("%(num)d update", "%(num)d updates", updates) % {"num": updates})
    if comments := group.count("comment"):
        parts.append(ngettext("%(num)d comment", "%(num)d comments", comments) % {"num": comments})
    if group.count("time"):
        logged = _("%(hours)sh logged") % {"hours": _hours(group.hours, locale)}
        parts.append(f"{logged} ({', '.join(group.activities)})" if group.activities else logged)
    if commits := group.count("commit"):
        parts.append(ngettext("%(num)d commit", "%(num)d commits", commits) % {"num": commits})
    if edits := group.count("wiki"):
        parts.append(ngettext("%(num)d edit", "%(num)d edits", edits) % {"num": edits})
    if group.count("resolved"):
        parts.append(_("resolved"))
    return ", ".join(parts)


async def activity_context(db: AsyncSession, scope: dash.DashboardScope, who: str) -> dict[str, Any]:
    who = who if who in dash.ACTIVITY_FILTERS else "all"
    locale = get_locale()
    groups = await dash.activity_groups(db, scope, who)
    views = []
    for group in groups:
        views.append(
            {
                "group": group,
                "summary": _group_summary(group, locale),
                "events": [
                    {"event": e, "hours": _hours(e.hours, locale) if e.kind == "time" else ""} for e in group.events
                ],
            }
        )
    return {"activity": {"who": who, "filters": dash.ACTIVITY_FILTERS, "groups": views}}


async def _active_versions(db: AsyncSession, scope: dash.DashboardScope, today: datetime.date) -> list[dict]:
    """The two most relevant versions: open ones first (by due date), then recently closed ones."""
    if not scope.project_ids:
        return []
    done_sub = select(IssueStatus.id).where(IssueStatus.category.in_(["done", "closed"])).scalar_subquery()
    ver_stmt = (
        select(
            Version,
            Project.key.label("project_key"),
            func.count(Issue.id).label("total"),
            func.count(Issue.id).filter(Issue.status_id.in_(done_sub)).label("closed_count"),
        )
        .join(Project, Project.id == Version.project_id)
        .outerjoin(Issue, and_(Issue.fixed_version_id == Version.id, _visible(scope.issue_visibility)))
        .where(Version.project_id.in_(scope.project_ids))
        .group_by(Version.id, Project.key)
        .order_by((Version.status == "open").desc(), Version.effective_date.asc().nullslast())
        .limit(4)
    )
    open_versions: list[dict] = []
    closed_versions: list[dict] = []
    for version, project_key, total, closed in (await db.execute(ver_stmt)).all():
        closed = int(closed or 0)
        total = int(total or 0)
        v_dict: dict = {
            "id": version.id,
            "name": version.name,
            "status": version.status,
            "due_date": format_date(version.effective_date, "MMMd") if version.effective_date else None,
            "progress": int(closed / total * 100) if total > 0 else 0,
            "open_count": total - closed,
            "closed_count": closed,
            "total_count": total,
            "project_key": project_key,
        }
        if version.status == "open" and version.effective_date:
            v_dict["days_left"] = (version.effective_date - today).days
        (open_versions if version.status == "open" else closed_versions).append(v_dict)
    return (open_versions + closed_versions)[:2]


async def _active_sprints(db: AsyncSession, scope: dash.DashboardScope, today: datetime.date) -> list[dict]:
    """Active sprints across the reader's projects, with the progress of the issues they may see."""
    if not scope.project_ids:
        return []
    done_sub = select(IssueStatus.id).where(IssueStatus.category.in_(["done", "closed"])).scalar_subquery()
    sprint_stmt = (
        select(
            Sprint,
            Project.key.label("project_key"),
            Project.name.label("project_name"),
            func.count(Issue.id).label("total"),
            func.count(Issue.id).filter(Issue.status_id.in_(done_sub)).label("closed_count"),
        )
        .join(Project, Project.id == Sprint.project_id)
        .outerjoin(Issue, and_(Issue.sprint_id == Sprint.id, _visible(scope.issue_visibility)))
        .where(Sprint.project_id.in_(scope.project_ids), Sprint.status == "active")
        .group_by(Sprint.id, Project.key, Project.name)
        .order_by(Sprint.start_date.asc().nullslast())
        .limit(5)
    )
    sprints: list[dict] = []
    for sprint, project_key, project_name, total, closed in (await db.execute(sprint_stmt)).all():
        closed = int(closed or 0)
        total = int(total or 0)
        days_total = days_remaining = None
        if sprint.start_date and sprint.end_date:
            days_total = (sprint.end_date - sprint.start_date).days
            days_remaining = max(0, (sprint.end_date - today).days)
        sprints.append(
            {
                "id": sprint.id,
                "name": sprint.name,
                "project_key": project_key,
                "project_name": project_name,
                "start_date": format_date(sprint.start_date, "MMMd") if sprint.start_date else None,
                "end_date": format_date(sprint.end_date, "MMMd") if sprint.end_date else None,
                "days_total": days_total,
                "days_remaining": days_remaining,
                "total": total,
                "closed": closed,
                "progress": int(closed / total * 100) if total > 0 else 0,
            }
        )
    return sprints


async def _recent_wiki(db: AsyncSession, scope: dash.DashboardScope) -> list[dict]:
    """The three most recently updated wiki pages in the projects whose wiki the reader may read."""
    if not scope.wiki_project_ids:
        return []
    latest_version = (
        select(func.max(WikiContent.version))
        .where(WikiContent.page_id == WikiPage.id)
        .correlate(WikiPage)
        .scalar_subquery()
    )
    wiki_stmt = (
        select(
            WikiPage.title,
            WikiPage.slug,
            WikiPage.updated_at,
            Project.key.label("project_key"),
            Project.name.label("project_name"),
            User.display_name.label("author_name"),
            User.login.label("author_login"),
            User.is_service_account.label("author_is_agent"),
        )
        .join(Wiki, WikiPage.wiki_id == Wiki.id)
        .join(Project, Wiki.project_id == Project.id)
        .outerjoin(WikiContent, (WikiContent.page_id == WikiPage.id) & (WikiContent.version == latest_version))
        .outerjoin(User, WikiContent.author_id == User.id)
        .where(WikiPage.deleted_at.is_(None), Wiki.project_id.in_(scope.wiki_project_ids))
        .order_by(WikiPage.updated_at.desc())
        .limit(3)
    )
    return [
        {
            "title": r.title,
            "slug": r.slug,
            "project_key": r.project_key,
            "project_name": r.project_name,
            "updated_at": r.updated_at,
            "author_name": (r.author_login if r.author_is_agent else r.author_name) or "",
        }
        for r in (await db.execute(wiki_stmt)).all()
    ]


@router.get("/my/notifications/", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    db: AsyncSession = DB_SESSION,
    unread_only: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the notifications page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    notifications, total = await _notif_svc.list_notifications(
        db, user.id, unread_only=unread_only, offset=offset, limit=limit
    )
    unread_count = await _notif_svc.get_unread_count(db, user.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/notifications.html",
        context={
            "user": user,
            "active_page": "notifications",
            "notifications": notifications,
            "notification_actors": await _notification_actors(db, notifications),
            "total": total,
            "unread_count": unread_count,
            "unread_only": unread_only,
            "offset": offset,
            "limit": limit,
        },
    )
