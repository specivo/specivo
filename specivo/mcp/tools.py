"""MCP tool implementations — testable async functions.

Each ``_*`` function accepts an explicit ``session`` and ``user``,
delegates to the service layer, and returns a formatted string.
The MCP ``@mcp.tool()`` wrappers in ``server.py`` resolve auth
and session, then call these functions.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import SEARCH_SNIPPET_MAX_CHARS
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.schemas.time_entry import TimeEntryCreate
from specivo.schemas.version import VersionCreate, VersionUpdate
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import AuditEvent
from specivo.services.time_entry_service import TimeEntryService
from specivo.services.version_service import VersionService
from specivo.services.wiki_service import WikiService

logger = logging.getLogger(__name__)

_issue_svc = IssueService()
_project_svc = ProjectService()
_wiki_svc = WikiService()
_search_svc = SearchService()
_journal_svc = JournalService()
_time_entry_svc = TimeEntryService()
_version_svc = VersionService()


async def _log_tool(
    session: AsyncSession,
    user: User,
    event_type: str | AuditEvent,
    tool_name: str,
    details: dict | None = None,
    project_id: int | None = None,
) -> None:
    """Log an MCP tool call. Core feature — always persisted, no enterprise gate."""
    log_details = {"tool": tool_name, "source": "mcp"}
    if details:
        log_details.update(details)
    log = SecurityAuditLog(
        event_type=str(event_type),
        user_id=user.id,
        project_id=project_id,
        details=log_details,
    )
    session.add(log)
    await session.flush()


async def _require_permission(
    session: AsyncSession, user: User, project_id: int, permission: str,
) -> None:
    if not await check_permission(user, project_id, permission, session):
        raise PermissionDeniedError(f"Permission '{permission}' denied for this project")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


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
# Issues
# ---------------------------------------------------------------------------


async def _list_issues(
    session: AsyncSession,
    user: User,
    project_key: str,
    status: str = "open",
    sort: str = "created_at:desc",
    offset: int = 0,
    limit: int = 25,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    issues, total = await _issue_svc.list_issues(
        session,
        project_id=project.id,
        filters={"status": status},
        sort=sort,
        offset=offset,
        limit=limit,
        user=user,
    )
    lines = [f"Issues for {project.key} ({total} total, filter={status}):", ""]
    for i in issues:
        lines.append(f"  {i.display_key}  [{i.status.name}]  {i.subject}")
    if not issues:
        lines.append("  (none)")
    await _log_tool(session, user, AuditEvent.ISSUE_LISTED, "list_issues", {"project_key": project_key})
    return "\n".join(lines)


async def _show_issue(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    metadata_only: bool = False,
    search: str | None = None,
) -> str:
    issue = await _issue_svc.get_by_display_key_with_relations(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "view_issues")

    lines = [
        f"Issue: {issue.display_key}",
        f"Subject: {issue.subject}",
        f"Tracker: {issue.tracker.name}",
        f"Status: {issue.status.name}",
        f"Priority: {issue.priority.name}",
        f"Author: {issue.author.display_name}",
    ]
    if issue.assigned_to:
        lines.append(f"Assigned to: {issue.assigned_to.display_name}")
    lines.append(f"Created: {issue.created_at}")
    lines.append(f"Updated: {issue.updated_at}")
    if issue.start_date:
        lines.append(f"Start date: {issue.start_date}")
    if issue.due_date:
        lines.append(f"Due date: {issue.due_date}")
    if issue.estimated_hours:
        lines.append(f"Estimated hours: {issue.estimated_hours}")
    lines.append(f"Done: {issue.done_ratio}%")
    if issue.issue_metadata:
        lines.append(f"Metadata: {issue.issue_metadata}")
    lines.append(f"Lock version: {issue.lock_version}")

    if not metadata_only:
        description = issue.description or ""
        if search and description:
            section = _extract_section(description, search)
            if section is not None:
                lines.append("")
                lines.append(f"Description (section matching '{search}'):")
                lines.append(section)
            else:
                lines.append("")
                lines.append(f"Description ('{search}' not found in text):")
                lines.append(description)
        else:
            lines.append("")
            lines.append("Description:")
            lines.append(description)

    await _log_tool(session, user, AuditEvent.ISSUE_READ, "show_issue", {"issue_ref": issue_ref})
    return "\n".join(lines)


def _extract_section(text: str, search: str) -> str | None:
    """Extract the paragraph/section containing ``search``.

    Splits on double-newline (paragraph break) or markdown headings
    and returns the matching block plus its heading context.
    """
    if search not in text:
        return None

    # Split into sections by markdown headings or double newlines
    import re

    # Split on lines that start with ## (markdown heading) keeping the delimiter
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    if len(parts) <= 1:
        # Fall back to paragraph split
        parts = text.split("\n\n")

    for part in parts:
        if search in part:
            return part.strip()

    return None


async def _create_issue(
    session: AsyncSession,
    user: User,
    project_key: str,
    tracker_id: int,
    subject: str,
    description: str = "",
    status_id: int | None = None,
    priority_id: int | None = None,
    assigned_to_id: int | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "add_issues")
    data = IssueCreate(
        project_key=project_key,
        tracker_id=tracker_id,
        subject=subject,
        description=description,
        status_id=status_id,
        priority_id=priority_id,
        assigned_to_id=assigned_to_id,
    )
    issue = await _issue_svc.create(session, project, data, user)
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.ISSUE_CREATED, "create_issue",
        {"project_key": project_key, "subject": subject, "issue_ref": issue.display_key},
        project_id=project.id,
    )
    return (
        f"Created issue {issue.display_key}: {issue.subject}\nStatus: {issue.status_id}, Priority: {issue.priority_id}"
    )


async def _update_issue(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    subject: str | None = None,
    description: str | None = None,
    status_id: int | None = None,
    priority_id: int | None = None,
    assigned_to_id: int | None = None,
    notes: str | None = None,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "edit_issues")
    data = IssueUpdate(
        subject=subject,
        description=description,
        status_id=status_id,
        priority_id=priority_id,
        assigned_to_id=assigned_to_id,
        done_ratio=None,
        lock_version=issue.lock_version,
    )
    updated = await _issue_svc.update(session, issue, data, user, notes=notes)
    await session.flush()
    await _log_tool(session, user, AuditEvent.ISSUE_UPDATED, "update_issue", {"issue_ref": issue_ref})
    return f"Updated issue {updated.display_key}: {updated.subject}\nLock version: {updated.lock_version}"


async def _edit_description(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    search_text: str,
    replace_text: str,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "edit_issues")
    current = issue.description or ""
    if search_text not in current:
        return (
            f"Error: search text not found in {issue.display_key} description.\n"
            f"Description length: {len(current)} chars."
        )
    new_description = current.replace(search_text, replace_text, 1)
    data = IssueUpdate(
        subject=None,
        description=new_description,
        done_ratio=None,
        lock_version=issue.lock_version,
    )
    updated = await _issue_svc.update(session, issue, data, user)
    await session.flush()
    await _log_tool(session, user, AuditEvent.ISSUE_UPDATED, "edit_description", {"issue_ref": issue_ref})
    return (
        f"Updated description of {updated.display_key}.\n"
        f"Replaced '{search_text}' -> '{replace_text}'.\n"
        f"Lock version: {updated.lock_version}"
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def _search(
    session: AsyncSession,
    user: User,
    query: str,
    project_key: str | None = None,
    scope: str = "all",
    limit: int = 10,
) -> str:
    project_id: int | None = None
    if project_key:
        project = await _project_svc.get_by_key(session, project_key)
        project_id = project.id

    results, total, _type_counts = await _search_svc.search(
        session, query, user=user, project_id=project_id, scope=scope, limit=limit
    )
    lines = [f"Search results for '{query}' ({total} total):", ""]
    for r in results:
        lines.append(f"  [{r.result_type}] {r.title}  —  {r.subtitle or ''}")
        if r.snippet:
            lines.append(f"    {r.snippet[:SEARCH_SNIPPET_MAX_CHARS]}")
    if not results:
        lines.append("  (no results)")
    await _log_tool(
        session, user, AuditEvent.SEARCH_QUERY, "search",
        {"query": query, "scope": scope, "result_count": total},
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------


async def _read_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    metadata_only: bool = False,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    lines = [
        f"Wiki: {page.title}",
        f"Slug: {page.slug}",
        f"Version: {content.version}",
        f"Lock version: {page.lock_version}",
    ]
    if not metadata_only:
        lines.append("")
        lines.append("Content:")
        lines.append(content.text or "")
    await _log_tool(session, user, AuditEvent.WIKI_READ, "read_wiki", {"project_key": project_key, "slug": slug})
    return "\n".join(lines)


async def _list_wiki_pages(
    session: AsyncSession,
    user: User,
    project_key: str,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_wiki")
    pages = await _wiki_svc.list_pages(session, project.id)
    lines = [f"Wiki pages for {project.key} ({len(pages)} total):", ""]
    for p in pages:
        lines.append(f"  {p.slug}  —  {p.title}")
    if not pages:
        lines.append("  (none)")
    await _log_tool(session, user, AuditEvent.WIKI_LISTED, "list_wiki_pages", {"project_key": project_key})
    return "\n".join(lines)


async def _edit_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    search_text: str,
    replace_text: str,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""
    if search_text not in current:
        return f"Error: search text not found in wiki page '{page.title}'.\nContent length: {len(current)} chars."
    new_text = current.replace(search_text, replace_text, 1)
    page, new_content = await _wiki_svc.update_page(session, page.id, new_text, user, lock_version=page.lock_version)
    await session.flush()
    await _log_tool(session, user, AuditEvent.WIKI_UPDATED, "edit_wiki", {"project_key": project_key, "slug": slug})
    return (
        f"Updated wiki page '{page.title}' (version {new_content.version}).\n"
        f"Replaced '{search_text}' -> '{replace_text}'."
    )


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


async def _add_comment(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    notes: str,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "add_issue_notes")
    journal = await _journal_svc.add_comment(session, issue, user, notes)
    await session.flush()
    await _log_tool(session, user, AuditEvent.COMMENT_ADDED, "add_comment", {"issue_ref": issue_ref})
    return f"Added comment to {issue.display_key} (journal #{journal.sequence}).\nNotes: {notes[:100]}"


# ---------------------------------------------------------------------------
# Wiki — create
# ---------------------------------------------------------------------------


async def _create_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    title: str,
    text: str,
    parent_slug: str | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.create_page(
        session, project.id, title, text, user, parent_slug=parent_slug,
    )
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.WIKI_CREATED, "create_wiki",
        {"project_key": project_key, "slug": page.slug, "title": title},
        project_id=project.id,
    )
    return (
        f"Created wiki page '{page.title}' (slug: {page.slug}).\n"
        f"Version: {content.version}\nLock version: {page.lock_version}"
    )


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
        lines.append(f"  {t.id}  {t.name}")
    lines.append("")
    lines.append("Statuses:")
    for s in statuses:
        closed = "  [closed]" if s.is_closed else ""
        lines.append(f"  {s.id}  {s.name}{closed}")
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
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    members = await _project_svc.list_members(session, project)
    lines = [f"Members of {project.key} ({len(members)} total):", ""]
    for m in members:
        roles = ", ".join(m["roles"]) if m["roles"] else "(no roles)"
        lines.append(f"  {m['user_id']}  {m['login']}  —  {m['display_name']}  [{roles}]")
    if not members:
        lines.append("  (none)")
    await _log_tool(
        session, user, AuditEvent.MEMBERS_LISTED, "list_members",
        {"project_key": project_key}, project_id=project.id,
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Time logging
# ---------------------------------------------------------------------------


async def _log_time(
    session: AsyncSession,
    user: User,
    project_key: str,
    hours: Decimal,
    activity_id: int,
    issue_ref: str | None = None,
    comments: str | None = None,
    spent_on: date | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "log_time")
    issue_id: int | None = None
    if issue_ref:
        issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
        issue_id = issue.id

    data = TimeEntryCreate(
        issue_id=issue_id,
        activity_id=activity_id,
        hours=hours,
        comments=comments,
        spent_on=spent_on or date.today(),
    )
    entry = await _time_entry_svc.create(session, project.id, data, user)
    await session.flush()
    issue_label = f" on {issue_ref}" if issue_ref else ""
    await _log_tool(
        session, user, AuditEvent.TIME_LOGGED, "log_time",
        {"project_key": project_key, "hours": str(hours), "issue_ref": issue_ref},
        project_id=project.id,
    )
    return f"Logged {entry.hours}h{issue_label} in {project.key}.\nEntry ID: {entry.id}"


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


async def _list_versions(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    versions = await _version_svc.list_for_project(session, project.id)
    lines = [f"Versions for {project.key} ({len(versions)} total):", ""]
    for v in versions:
        due = f"  due: {v.effective_date}" if v.effective_date else ""
        lines.append(f"  {v.id}  [{v.status}]  {v.name}{due}")
    if not versions:
        lines.append("  (none)")
    await _log_tool(
        session, user, AuditEvent.VERSIONS_LISTED, "list_versions",
        {"project_key": project_key}, project_id=project.id,
    )
    return "\n".join(lines)


async def _create_version(
    session: AsyncSession,
    user: User,
    project_key: str,
    name: str,
    description: str | None = None,
    status: str = "open",
    due_date: date | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_versions")
    data = VersionCreate(
        name=name, description=description, status=status, effective_date=due_date,
    )
    version = await _version_svc.create(session, project, data)
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.VERSION_CREATED, "create_version",
        {"project_key": project_key, "name": name}, project_id=project.id,
    )
    return f"Created version '{version.name}' (ID: {version.id}) in {project.key}."


async def _update_version(
    session: AsyncSession,
    user: User,
    project_key: str,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    due_date: date | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)  # validate project
    await _require_permission(session, user, project.id, "manage_versions")
    version = await _version_svc.get_by_id(session, version_id)
    if version.project_id != project.id:
        raise NotFoundError(message="Version not found in this project")
    data = VersionUpdate(
        name=name, description=description, status=status, effective_date=due_date,
    )
    version = await _version_svc.update(session, version, data)
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.VERSION_UPDATED, "update_version",
        {"project_key": project_key, "version_id": version_id}, project_id=version.project_id,
    )
    return f"Updated version '{version.name}' (ID: {version.id})."
