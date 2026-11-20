"""MCP tools: identity, projects, members and lookup tables."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools._common import _get_project, _log_tool, _project_svc, _require_permission, _time_entry_svc
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent


async def _whoami(session: AsyncSession, user: User) -> str:
    """Return the authenticated user's identity."""
    lines = [
        f"user_id: {user.id}",
        f"login: {user.login}",
        f"display_name: {user.display_name}",
        f"email: {user.email}",
        f"is_admin: {user.is_admin}",
        f"status: {user.status}",
    ]
    await _log_tool(session, user, AuditEvent.RESOURCE_ACCESS, "whoami")
    return "\n".join(lines)


async def _list_projects(
    session: AsyncSession,
    user: User,
    offset: int = 0,
    limit: int = 25,
) -> str:
    projects, total = await _project_svc.list_projects(session, user, offset, limit)
    lines = [f"Projects ({total} total):", ""]
    for p in projects:
        status_label = "active" if p.status == 1 else "archived"
        lines.append(f"  {p.key}  {p.name}  ({status_label})")
    if not projects:
        lines.append("  (none)")
    await _log_tool(session, user, AuditEvent.PROJECTS_LISTED, "list_projects")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def _list_lookups(session: AsyncSession, user: User) -> str:
    trackers = (await session.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    statuses = (await session.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    priorities = (await session.execute(select(IssuePriority).order_by(IssuePriority.position))).scalars().all()
    activities = await _time_entry_svc.list_activities(session)

    lines = ["Trackers:", ""]
    for t in trackers:
        lines.append(f"  {t.id}  {t.name}")  # noqa: PERF401
    lines.append("")
    lines.append("Statuses:")
    for s in statuses:
        lines.append(f"  {s.id}  {s.name}  [{s.category}]")  # noqa: PERF401
    lines.append("")
    lines.append("Priorities:")
    for p in priorities:
        default = "  [default]" if p.is_default else ""
        lines.append(f"  {p.id}  {p.name}{default}")
    lines.append("")
    lines.append("Time entry activities:")
    for a in activities:
        default = "  [default]" if a.is_default else ""
        lines.append(f"  {a.id}  {a.name}{default}")

    await _log_tool(session, user, AuditEvent.LOOKUPS_READ, "list_lookups")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def _list_members(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    members = await _project_svc.list_members(session, project)
    lines = [f"Members of {project.key} ({len(members)} total):", ""]
    for m in members:
        roles = ", ".join(m["roles"]) if m["roles"] else "(no roles)"
        lines.append(f"  {m['user_id']}  {m['login']}  —  {m['display_name']}  [{roles}]")
    if not members:
        lines.append("  (none)")
    await _log_tool(
        session,
        user,
        AuditEvent.MEMBERS_LISTED,
        "list_members",
        {"project_key": project_key},
        project_id=project.id,
    )
    return "\n".join(lines)
