"""Web pages for a project's agent sessions: the list, one session, and its tool-call timeline.

The list is open to everyone who can read the project; sessions tied to an
issue the reader cannot see are left out. A session's detail and its timeline
need ``manage_project`` in the project (or an administrator): the timeline is
built from the security audit log, whose rows can name issues and wiki pages
across the account's work. Only metadata is shown: the tool, the event type
and the ids and keys the audit row holds, never tool arguments or results.

The timeline is its own template (``pages/agent_sessions/_timeline.html``) and
its own route (``.../timeline/``), so other pages can embed it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import DEFAULT_PAGE_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import get_locale
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.agent_session_usage import SessionUsage, session_entries, usage_by_session
from specivo.services.permission_service import Permission, check_permission
from specivo.services.security_audit_service import SecurityAuditService
from specivo.web.deps import PERMISSION_DENIED, ProjectForReader, WebUser, get_templates

router = APIRouter(tags=["web-agent-sessions"], include_in_schema=False)

_sessions = AgentSessionService()
_audit = SecurityAuditService()

#: Tool calls per page of the timeline.
TIMELINE_PAGE_SIZE = 50
#: AI usage entries shown on a session's page.
USAGE_ENTRIES_SHOWN = 50
_PAGE_SIZES = (10, 25, 50)
#: Detail keys every MCP audit row carries; the timeline shows the others.
_COMMON_DETAILS = frozenset({"tool", "source"})
_DETAIL_VALUE_MAX = 80


def _base_url(project: Project) -> str:
    return f"/projects/{project.key}/agent-sessions/"


def _parse_active(value: str | None) -> bool | None:
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def _client_label(agent_session: AgentSession) -> str:
    if not agent_session.client_name:
        return ""
    if agent_session.client_version:
        return f"{agent_session.client_name} {agent_session.client_version}"
    return agent_session.client_name


def _spend_label(usage: SessionUsage | None) -> str:
    if usage is None or not usage.spend:
        return ""
    locale = get_locale()
    return ", ".join(money.format(locale) for money in usage.spend)


async def _users(db: AsyncSession, user_ids: set[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(user_ids)))
    return {u.id: u for u in rows.scalars().all()}


async def _issues(db: AsyncSession, issue_ids: set[int]) -> dict[int, tuple[str, str]]:
    """``{issue id: (display key, subject)}``; the sessions passed the visibility check already."""
    if not issue_ids:
        return {}
    rows = await db.execute(
        select(Issue.id, Issue.project_key, Issue.sequence_number, Issue.subject).where(Issue.id.in_(issue_ids))
    )
    return {iid: (f"{key}-{number}", subject) for iid, key, number, subject in rows.all()}


async def _session_rows(db: AsyncSession, viewer: User, sessions: list[AgentSession]) -> list[dict[str, Any]]:
    """The sessions as the list and the detail header show them."""
    users = await _users(db, {s.user_id for s in sessions})
    issues = await _issues(db, {s.issue_id for s in sessions if s.issue_id is not None})
    usage = await usage_by_session(db, viewer, [s.id for s in sessions])
    now = utcnow()
    rows = []
    for s in sessions:
        account = users.get(s.user_id)
        issue = issues.get(s.issue_id) if s.issue_id is not None else None
        session_usage = usage.get(s.id)
        rows.append(
            {
                "id": s.id,
                "account_login": account.login if account else "",
                "account_name": (account.display_name or account.login) if account else "",
                "service_account": bool(account and account.is_service_account),
                "client": _client_label(s),
                "client_type": s.client_type or "",
                "issue_key": issue[0] if issue else "",
                "issue_subject": issue[1] if issue else "",
                "external_session_id": s.external_session_id or "",
                "parent_external_session_id": s.parent_external_session_id or "",
                "parent_session_id": s.parent_session_id,
                "started_at": s.started_at,
                "last_activity_at": s.last_activity_at,
                "ended_at": s.ended_at,
                "active": _sessions.is_active(s, now),
                "tool_calls": s.tool_call_count,
                "usage_entries": session_usage.entries if session_usage else 0,
                "spend": _spend_label(session_usage),
            }
        )
    return rows


async def _can_manage(db: AsyncSession, user: User, project: Project, request: Request | None = None) -> bool:
    return await check_permission(user, project.id, Permission.MANAGE_PROJECT, db, request=request)


async def _managed_session(
    request: Request, db: AsyncSession, user: User, project: Project, session_id: int
) -> AgentSession:
    """Session *session_id* of *project* for a reader with ``manage_project``: 403 (audited) without it, else 404."""
    if not await _can_manage(db, user, project, request):
        raise HTTPException(status_code=403, detail=PERMISSION_DENIED)
    try:
        return await _sessions.get_for_project(db, project.id, session_id, user)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Agent session not found") from None


def _detail_items(details: dict[str, Any]) -> list[tuple[str, str]]:
    """The ids and keys of an audit row's details, minus the ones every row has, cut for display."""
    items = []
    for key, value in details.items():
        if key in _COMMON_DETAILS or value is None or value == "" or value == [] or value == {}:
            continue
        text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        if len(text) > _DETAIL_VALUE_MAX:
            text = text[: _DETAIL_VALUE_MAX - 1] + "…"
        items.append((key, text))
    return items


async def timeline_context(
    db: AsyncSession, project: Project, agent_session: AgentSession, offset: int = 0
) -> dict[str, Any]:
    """The context ``pages/agent_sessions/_timeline.html`` renders: one page of the session's tool calls.

    Rows of this project and rows without a project; newest first, at most
    ``TIMELINE_PAGE_SIZE`` per page. Embedding pages pass the result as
    ``timeline`` and include the template.
    """
    items, total = await _audit.list_events(
        db,
        agent_session_id=agent_session.id,
        project_id=project.id,
        include_projectless=True,
        offset=offset,
        limit=TIMELINE_PAGE_SIZE,
    )
    _logins, keys = await _audit.names_for(db, items)
    return {
        "rows": [
            {
                "created_at": item.created_at,
                "event_type": item.event_type,
                "tool": str(item.details.get("tool") or ""),
                "details": _detail_items(item.details),
                "project_key": keys.get(item.project_id, "") if item.project_id is not None else "",
            }
            for item in items
        ],
        "total": total,
        "offset": offset,
        "limit": TIMELINE_PAGE_SIZE,
        "base_url": f"{_base_url(project)}{agent_session.id}/",
        "partial_url": f"{_base_url(project)}{agent_session.id}/timeline/",
    }


@router.get("/projects/{project_key}/agent-sessions/", response_class=HTMLResponse)
async def agent_session_list(
    request: Request,
    user: WebUser,
    project: ProjectForReader,
    account: str = "",
    issue: str = "",
    active: str = "",
    offset: int = 0,
    limit: int = DEFAULT_PAGE_LIMIT,
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The project's agent sessions, most recently active first, with filters and pagination."""
    offset = max(offset, 0)
    limit = limit if limit in _PAGE_SIZES else DEFAULT_PAGE_LIMIT
    account, issue = account.strip()[:100], issue.strip()[:50]
    active_value = _parse_active(active)
    filters = await _sessions.filters_for(
        db, project.id, user, account=account or None, issue_ref=issue or None, active=active_value
    )
    sessions: list[AgentSession] = []
    total = 0
    if filters is not None:
        sessions, total = await _sessions.list_for_project(db, project.id, user, filters, offset=offset, limit=limit)
    query = {
        k: v
        for k, v in (("account", account), ("issue", issue), ("active", active if active_value is not None else ""))
        if v
    }
    return get_templates().TemplateResponse(
        request,
        "pages/projects/agent_sessions.html",
        context={
            "user": user,
            "active_page": "agent_sessions",
            "active_project": project,
            "project": project,
            "rows": await _session_rows(db, user, sessions),
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": {"account": account, "issue": issue, "active": active if active_value is not None else ""},
            "page_query": urlencode(query),
            "can_inspect": await _can_manage(db, user, project),
        },
    )


@router.get("/projects/{project_key}/agent-sessions/{session_id}/", response_class=HTMLResponse)
async def agent_session_detail(
    request: Request,
    session_id: int,
    user: WebUser,
    project: ProjectForReader,
    offset: int = 0,
    db: AsyncSession = DB_SESSION,
) -> Response:
    """One session: who and what ran it, its tool calls from the audit log, and its AI usage. Needs manage_project."""
    from specivo.services.ai_usage_service import AiUsageService
    from specivo.web.pages.ai_usage.rows import _rows

    agent_session = await _managed_session(request, db, user, project, session_id)
    [row] = await _session_rows(db, user, [agent_session])
    lineage = await _sessions.lineage(db, agent_session)
    entries, usage_total = await session_entries(db, user, agent_session, limit=USAGE_ENTRIES_SHOWN)
    refs = await AiUsageService().references(db, user, entries)
    usage_rows = await _rows(db, user, entries, refs)
    await _audit.log_resource_viewed(
        db,
        user_id=user.id,
        resource="agent_session",
        resource_key=str(agent_session.id),
        resource_id=agent_session.id,
        project_id=project.id,
        request=request,
    )
    return get_templates().TemplateResponse(
        request,
        "pages/projects/agent_session_detail.html",
        context={
            "user": user,
            "active_page": "agent_sessions",
            "active_project": project,
            "project": project,
            "session": row,
            "lineage": [
                {
                    "id": s.id,
                    "external_session_id": s.external_session_id or "",
                    "client": _client_label(s),
                    "started_at": s.started_at,
                    "is_parent": s.id == agent_session.parent_session_id,
                }
                for s in lineage
            ],
            "timeline": await timeline_context(db, project, agent_session, max(offset, 0)),
            "usage_rows": usage_rows,
            "usage_total": usage_total,
            "usage_shown": USAGE_ENTRIES_SHOWN,
            "return_to": f"{_base_url(project)}{agent_session.id}/",
        },
    )


@router.get("/projects/{project_key}/agent-sessions/{session_id}/timeline/", response_class=HTMLResponse)
async def agent_session_timeline(
    request: Request,
    session_id: int,
    user: WebUser,
    project: ProjectForReader,
    offset: int = 0,
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The session's tool-call timeline alone, for pages that embed it (htmx). Needs manage_project."""
    agent_session = await _managed_session(request, db, user, project, session_id)
    return get_templates().TemplateResponse(
        request,
        "pages/agent_sessions/_timeline.html",
        context={"user": user, "timeline": await timeline_context(db, project, agent_session, max(offset, 0))},
    )
