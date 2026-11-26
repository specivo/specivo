"""The Agents tab of an issue: what agents did for it, and what it cost, one block per agent session.

- ``/partials/issues/{key}/agents/``: the tab, loaded by htmx when it opens on
  the issue page, and swapped in again by its own links;
- ``/issue/{key}/agents/``: the same view as a page of its own, so the
  sub-issue switch and the pages also work without JavaScript.

Each block is a lineage root with its subagents (``AgentTimelineService``):
the account and client, the state, counters (tool calls, approvals and how
they were resolved, subagents), the newest steps from the audit log (the
session's page has them all) and the AI cost of the block from the usage
entries.

Who sees it: the readers of the issue with ``manage_project`` in its project
(or an administrator), as for the agent session pages, whose steps come from
the same audit rows. An issue the reader cannot see is a 404 before the
permission is checked, so the 403 says nothing about hidden issues. The cost
line needs ``view_ai_usage`` too and shows only the entries the reader may
read there. Each request spends one AI report from the reader's budget, as
the AI usage tab does.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, ClassVar

from babel.dates import format_timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import get_locale
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.role import AI_USAGE_VISIBILITY_ALL
from specivo.models.user import User
from specivo.services.agent_timeline_service import (
    BLOCKS_PER_PAGE,
    STEPS_SHOWN,
    AgentBlock,
    AgentPage,
    AgentTimelineService,
    BlockCost,
    SessionNode,
)
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, ai_usage_visibility, check_permission
from specivo.web.deps import PERMISSION_DENIED, get_templates, require_user
from specivo.web.pages.agent_sessions import _detail_items
from specivo.web.pages.ai_issue_usage import TabState
from specivo.web.pages.ai_spend_reports.view import _short_int
from specivo.web.pages.ai_usage._common import _fmt_int, _project_for

router = APIRouter(tags=["web-issue-agents"], include_in_schema=False)

_timeline = AgentTimelineService()
_issues = IssueService()

#: Detail keys of a step that say nothing to a reader: delivery and call ids, the raw event name.
_HIDDEN_DETAILS = frozenset({"event", "delivery_id", "tool_call_id", "turn_id", "child_session_id"})


class AgentsTabState(TabState):
    """The Agents tab's URL state: the sub-issue switch and the page of blocks."""

    segment: ClassVar[str] = "agents"
    page_sizes: ClassVar[tuple[int, ...]] = (BLOCKS_PER_PAGE,)


async def can_view_agents(db: AsyncSession, user: User, project: Project, request: Request | None = None) -> bool:
    """Whether *user* may see the Agents tab of the project's issues: ``manage_project`` or an administrator."""
    if user.is_anonymous:
        return False
    return await check_permission(user, project.id, Permission.MANAGE_PROJECT, db, request=request)


async def tab_session_count(db: AsyncSession, user: User, project: Project, issue: Issue) -> int:
    """How many blocks the tab lists by default (sub-issues included when there are any), for its badge."""
    has_children = bool(await db.scalar(select(exists().where(Issue.parent_id == issue.id))))
    return await _timeline.count_for_issue(db, user, project, issue, include_subtree=has_children)


def _client_label(agent_session: AgentSession) -> str:
    if not agent_session.client_name:
        return ""
    if agent_session.client_version:
        return f"{agent_session.client_name} {agent_session.client_version}"
    return agent_session.client_name


def _distinct(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _duration(start: datetime, end: datetime) -> str:
    return format_timedelta(max(end - start, timedelta(0)), threshold=1.0, locale=get_locale())


def _node_view(node: SessionNode, page: AgentPage, state: str) -> dict[str, Any]:
    primary = node.primary
    account = page.users.get(primary.user_id)
    rows = sorted(node.rows, key=lambda s: (s.started_at, s.id))
    return {
        "id": primary.id,
        "session_ids": [s.id for s in rows],
        "account_login": account.login if account else "",
        "account_name": (account.display_name or account.login) if account else "",
        "service_account": bool(account and account.is_service_account),
        "clients": _distinct([_client_label(s) for s in rows]),
        "client_types": _distinct([s.client_type or "" for s in rows]),
        "external_session_id": primary.external_session_id or "",
        "started_at": node.started_at,
        "last_activity_at": node.last_activity_at,
        "ended_at": node.ended_at,
        "duration": _duration(node.started_at, node.ended_at or node.last_activity_at),
        "state": state,
    }


def _cost_view(cost: BlockCost | None, locale: str) -> dict[str, Any] | None:
    if cost is None:
        return None
    return {
        "entries": cost.entries,
        "tokens": _fmt_int(cost.tokens, locale),
        "tokens_short": _short_int(cost.tokens, locale),
        "spend": cost.marginal.total.format(locale) if cost.marginal is not None else "",
        "spend_unconverted": bool(cost.marginal and cost.marginal.has_unconverted),
        "api": cost.api_equivalent.total.format(locale) if cost.api_equivalent is not None else "",
        "profiles": cost.profiles,
        "unattended": cost.unattended,
    }


async def _steps(db: AsyncSession, project: Project, block: AgentBlock, detail_url: str) -> dict[str, Any]:
    """The block's newest steps, in the shape ``pages/agent_sessions/_timeline.html`` renders."""
    items, total = await _timeline.steps_for(db, project, block)
    return {
        "rows": [
            {
                "created_at": item.created_at,
                "event_type": item.event_type,
                "tool": str(item.details.get("tool") or ""),
                "details": _detail_items({k: v for k, v in item.details.items() if k not in _HIDDEN_DETAILS}),
                "project_key": project.key if item.project_id == project.id else "",
            }
            for item in items
        ],
        "total": total,
        "offset": 0,
        "limit": STEPS_SHOWN,
        "more_url": detail_url,
    }


async def _blocks(db: AsyncSession, project: Project, page: AgentPage, sees_all_cost: bool) -> list[dict[str, Any]]:
    """The blocks as the template shows them.

    A block has a cost line when it has usage the reader may read, or, for a
    reader who sees every entry of the project, a "not reported yet" line: to
    a reader who sees only some entries, a missing cost says nothing.
    """
    locale = get_locale()
    now = utcnow()
    out = []
    for block in page.blocks:
        counters = page.counters[block.root.key]
        detail_url = f"/projects/{project.key}/agent-sessions/{block.root.primary.id}/"
        root = _node_view(block.root, page, _timeline.state_of(block.root, counters, now))
        children = [
            {
                **_node_view(child, page, _timeline.state_of(child, counters, now)),
                "detail_url": f"/projects/{project.key}/agent-sessions/{child.primary.id}/",
            }
            for child in block.child_nodes()
        ]
        resolved = sorted(counters.approvals_resolved.items(), key=lambda kv: (-kv[1], kv[0]))
        out.append(
            {
                **root,
                "detail_url": detail_url,
                "children": children,
                "tool_calls": counters.tool_calls,
                "approvals_requested": counters.approvals_requested,
                "approvals_resolved": resolved,
                "approvals_resolved_total": sum(n for _c, n in resolved),
                "cost_visible": page.costs is not None and (block.root.key in page.costs or sees_all_cost),
                "cost": _cost_view((page.costs or {}).get(block.root.key), locale),
                "timeline": await _steps(db, project, block, detail_url),
            }
        )
    return out


async def _context(
    request: Request, response: Response, db: AsyncSession, user: User, issue_ref: str, sub: str, offset: str
) -> dict[str, Any]:
    """Everything the tab shows. ``NotFoundError`` for an issue the reader cannot see, 403 without the permission."""
    issue = await _issues.get_by_display_key(db, issue_ref, user=user)
    project = await _project_for(db, user, issue.project_key)
    if not await can_view_agents(db, user, project, request):
        raise HTTPException(status_code=403, detail=PERMISSION_DENIED)
    await enforce_ai_report_rate_limit(request, response, user)
    has_children = bool(await db.scalar(select(exists().where(Issue.parent_id == issue.id))))
    state = AgentsTabState.read(issue.display_key, has_children, sub, offset, "")
    # ai_usage_visibility is None without view_ai_usage, else "own" or "all".
    visibility = await ai_usage_visibility(db, user, project)
    cost_visible = visibility is not None
    page = await _timeline.page(
        db,
        user,
        project,
        issue,
        include_subtree=state.sub,
        offset=state.offset,
        limit=state.limit,
        with_cost=cost_visible,
    )
    state.offset = page.offset
    current = state.offset // state.limit
    last_page = (page.total - 1) // state.limit if page.total else 0
    return {
        "user": user,
        "issue": issue,
        "project": project,
        "state": state,
        "blocks": await _blocks(db, project, page, visibility == AI_USAGE_VISIBILITY_ALL),
        "total": page.total,
        "first": state.offset + 1 if page.total else 0,
        "last": min(state.offset + state.limit, page.total),
        "prev": state.link(offset=state.offset - state.limit) if current > 0 else None,
        "next": state.link(offset=state.offset + state.limit) if current < last_page else None,
        "sub_links": {"on": state.link(sub=True, offset=0), "off": state.link(sub=False, offset=0)},
        "sessions_url": f"/projects/{project.key}/agent-sessions/?issue={issue.display_key}",
    }


@router.get("/partials/issues/{issue_ref}/agents/", response_class=HTMLResponse)
async def issue_agents_partial(
    issue_ref: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    sub: str = Query(""),
    offset: str = Query(""),
) -> Response:
    """The issue's Agents tab (loaded when opened, and by the tab's own links)."""
    try:
        context = await _context(request, response, db, user, issue_ref, sub, offset)
    except NotFoundError:
        return HTMLResponse("", status_code=404)
    return get_templates().TemplateResponse(request, "pages/issues/_agents_tab.html", context=context)


@router.get("/issue/{issue_ref}/agents/", response_class=HTMLResponse)
async def issue_agents_page(
    issue_ref: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    sub: str = Query(""),
    offset: str = Query(""),
) -> Response:
    """The issue's Agents tab as a page of its own: the tab's links without JavaScript."""
    context = await _context(request, response, db, user, issue_ref, sub, offset)
    context["active_project"] = context["project"]
    return get_templates().TemplateResponse(request, "pages/issues/agents.html", context=context)
