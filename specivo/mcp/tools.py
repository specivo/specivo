"""MCP tool implementations — testable async functions.

Each ``_*`` function accepts an explicit ``session`` and ``user``,
delegates to the service layer, and returns a formatted string.
The MCP ``@mcp.tool()`` wrappers in ``server.py`` resolve auth
and session, then call these functions.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.config import get_settings
from specivo.core.constants import SEARCH_SNIPPET_MAX_CHARS
from specivo.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.utils import utcnow
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.schemas.recurring_pattern import RecurringPatternCreate, RecurringPatternUpdate
from specivo.schemas.sprint import SprintCreate, SprintUpdate
from specivo.schemas.tag import TagCreate, TagUpdate
from specivo.schemas.time_entry import TimeEntryCreate
from specivo.schemas.version import VersionCreate, VersionUpdate
from specivo.services.computed_metadata_service import load_project_settings, merge_computed
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.recurrence import expand_occurrences
from specivo.services.recurring_pattern_service import RecurringPatternService
from specivo.services.relation_service import RelationService
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import AuditEvent
from specivo.services.sprint_service import SprintService
from specivo.services.tag_service import TagService
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
_relation_svc = RelationService()
_sprint_svc = SprintService()
_recurring_pattern_svc = RecurringPatternService()
_tag_svc = TagService()


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
    session: AsyncSession,
    user: User,
    project_id: int,
    permission: str | Permission,
) -> None:
    if not await check_permission(user, project_id, permission, session):
        raise PermissionDeniedError(f"Permission '{permission}' denied for this project")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


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
# Issues
# ---------------------------------------------------------------------------


def _parse_metadata_filters(raw: list[str] | None) -> list[tuple[str, str]]:
    """Parse `["key=value", ...]` MCP input into validated `(key, value)` pairs.

    Returns an empty list when *raw* is None or empty. Raises ``ValueError``
    with a caller-actionable message when any item is malformed (no ``=``,
    empty key, etc.) — surfaced to the agent via the standard ``Error: ...``
    return path in the calling tool.
    """
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            raise ValueError(
                f"metadata filter {item!r} must be a 'key=value' string"
            )
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"metadata filter {item!r} has an empty key")
        pairs.append((key, value))
    return pairs


async def _list_issues(
    session: AsyncSession,
    user: User,
    project_key: str,
    status: str = "open",
    sort: str = "created_at:desc",
    offset: int = 0,
    limit: int = 25,
    sprint_id: int | None = None,
    metadata_filters: list[str] | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    filters: dict = {"status": status}
    if sprint_id is not None:
        filters["sprint_id"] = sprint_id
    try:
        parsed_metadata = _parse_metadata_filters(metadata_filters)
    except ValueError as exc:
        return f"Error: {exc}"
    if parsed_metadata:
        filters["metadata_filters"] = parsed_metadata
    try:
        issues, total = await _issue_svc.list_issues(
            session,
            project_id=project.id,
            filters=filters,
            sort=sort,
            offset=offset,
            limit=limit,
            user=user,
        )
    except ValidationError as exc:
        return f"Error: {exc.message}"
    scope = f"filter={status}"
    if sprint_id is not None:
        scope += f", sprint_id={sprint_id}"
    if parsed_metadata:
        scope += ", metadata=" + ",".join(f"{k}={v}" for k, v in parsed_metadata)
    lines = [f"Issues for {project.key} ({total} total, {scope}):", ""]
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
    effective_metadata = merge_computed(
        issue.issue_metadata, await load_project_settings(session, issue.project_id)
    )
    if effective_metadata:
        lines.append(f"Metadata: {effective_metadata}")
    lines.append(f"Lock version: {issue.lock_version}")
    comments_count = await _journal_svc.count_comments(session, issue.id)
    lines.append(f"Comments: {comments_count}")

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
    fixed_version_id: int | None = None,
    sprint_id: int | None = None,
    metadata: dict | None = None,
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
        fixed_version_id=fixed_version_id,
        sprint_id=sprint_id,
        metadata=metadata or {},
    )
    issue = await _issue_svc.create(session, project, data, user)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_CREATED,
        "create_issue",
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
    done_ratio: int | None = None,
    notes: str | None = None,
    fixed_version_id: int | None = None,
    sprint_id: int | None = None,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "edit_issues")
    # Only forward kwargs the caller actually supplied. Pydantic V2 adds every
    # explicit kwarg to ``model_fields_set`` regardless of value, and the
    # service layer uses that set to distinguish "not provided" from
    # "explicitly cleared" for fixed_version_id / sprint_id. Including None
    # for an omitted field would otherwise wipe it.
    supplied: dict[str, Any] = {}
    if subject is not None:
        supplied["subject"] = subject
    if description is not None:
        supplied["description"] = description
    if status_id is not None:
        supplied["status_id"] = status_id
    if priority_id is not None:
        supplied["priority_id"] = priority_id
    if assigned_to_id is not None:
        supplied["assigned_to_id"] = assigned_to_id
    if done_ratio is not None:
        supplied["done_ratio"] = done_ratio
    if fixed_version_id is not None:
        supplied["fixed_version_id"] = fixed_version_id
    if sprint_id is not None:
        supplied["sprint_id"] = sprint_id
    data = IssueUpdate(**supplied, lock_version=issue.lock_version)
    updated = await _issue_svc.update(session, issue, data, user, notes=notes)
    await session.flush()
    await _log_tool(session, user, AuditEvent.ISSUE_UPDATED, "update_issue", {"issue_ref": issue_ref})
    return f"Updated issue {updated.display_key}: {updated.subject}\nLock version: {updated.lock_version}"


async def _move_issue(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    target_project_key: str,
    notes: str | None = None,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "edit_issues")
    old_ref = issue.display_key
    target = await _project_svc.get_by_key(session, target_project_key.upper())
    await _require_permission(session, user, target.id, "add_issues")
    issue = await _issue_svc.move(session, issue, target, user, notes=notes)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_UPDATED,
        "move_issue",
        {"issue_ref": old_ref, "target_project_key": target.key, "new_ref": issue.display_key},
        project_id=target.id,
    )
    return (
        f"Moved {old_ref} -> {issue.display_key} (project {target.key}).\n"
        f"History, relations, attachments and metadata preserved; version/sprint/"
        f"category/tags cleared. The old reference {old_ref} still resolves."
    )


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
    # Only set ``description`` — omitting every other field keeps
    # ``model_fields_set`` minimal so the service does not touch
    # fixed_version_id / sprint_id. Use ``**`` to avoid mypy insisting on
    # every other optional field being passed explicitly.
    supplied: dict[str, Any] = {"description": new_description}
    data = IssueUpdate(**supplied, lock_version=issue.lock_version)
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


SEARCH_MODES = ("hybrid", "keyword", "semantic")


async def _search(
    session: AsyncSession,
    user: User,
    query: str,
    project_key: str | None = None,
    scope: str = "all",
    limit: int = 10,
    mode: str = "hybrid",
) -> str:
    if mode not in SEARCH_MODES:
        raise ValidationError(f"Invalid mode '{mode}'. Must be one of: {', '.join(SEARCH_MODES)}")

    project_id: int | None = None
    if project_key:
        project = await _project_svc.get_by_key(session, project_key)
        project_id = project.id

    if mode == "hybrid":
        results, total, _type_counts = await _search_svc.hybrid_search(
            session, query, user=user, project_id=project_id, scope=scope, limit=limit
        )
    elif mode == "semantic":
        results, total = await _search_svc.semantic_search(
            session, query, user=user, project_id=project_id, limit=limit
        )
        if scope != "all":
            scope_map = {"issues": "issue", "wiki": "wiki", "comments": "comment", "attachments": "attachment"}
            target = scope_map.get(scope)
            if target:
                results = [r for r in results if r.result_type == target]
                total = len(results)
    else:
        results, total, _type_counts = await _search_svc.search(
            session, query, user=user, project_id=project_id, scope=scope, limit=limit
        )

    lines = [f"Search results for '{query}' [mode={mode}] ({total} total):", ""]
    for r in results:
        lines.append(f"  [{r.result_type}] {r.title}  —  {r.subtitle or ''}")
        if r.snippet:
            lines.append(f"    {r.snippet[:SEARCH_SNIPPET_MAX_CHARS]}")
    if not results:
        lines.append("  (no results)")
    await _log_tool(
        session,
        user,
        AuditEvent.SEARCH_QUERY,
        "search",
        {"query": query, "scope": scope, "mode": mode, "result_count": total},
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------


def _split_into_sections(text: str) -> list[dict]:
    """Split markdown into sections by headings.

    Returns list of {"heading": str|None, "level": int, "body": str, "start": int, "end": int}.
    First section may have heading=None for content before any heading.
    ``start`` and ``end`` are line indices (0-based) within the original text lines.
    """
    import re

    lines = text.split("\n")
    sections: list[dict] = []
    current_heading: str | None = None
    current_level: int = 0
    current_start: int = 0
    body_lines: list[str] = []

    for idx, line in enumerate(lines):
        m = re.match(r"^(#{1,6}) (.+)$", line)
        if m:
            # Flush previous section
            sections.append(
                {
                    "heading": current_heading,
                    "level": current_level,
                    "body": "\n".join(body_lines),
                    "start": current_start,
                    "end": idx - 1 if idx > 0 else 0,
                }
            )
            current_heading = line
            current_level = len(m.group(1))
            current_start = idx
            body_lines = []
        else:
            body_lines.append(line)

    # Flush last section
    sections.append(
        {
            "heading": current_heading,
            "level": current_level,
            "body": "\n".join(body_lines),
            "start": current_start,
            "end": len(lines) - 1,
        }
    )

    return sections


def _find_section(
    sections: list[dict],
    heading: str,
) -> tuple[int, dict] | None:
    """Find a section by heading text.

    Accepts both ``## Foo`` (exact heading line) and ``Foo`` (bare text, searches all levels).
    Returns (index, section_dict) or None.
    """
    # Exact heading line match first
    for idx, s in enumerate(sections):
        if s["heading"] == heading:
            return idx, s

    # Bare text: strip leading #s from heading and compare
    bare = heading.lstrip("#").strip()
    for idx, s in enumerate(sections):
        if s["heading"] is not None:
            section_bare = s["heading"].lstrip("#").strip()
            if section_bare == bare:
                return idx, s

    return None


async def _read_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    metadata_only: bool = False,
    search: str | None = None,
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
        text = content.text or ""
        if search and text:
            section = _extract_section(text, search)
            if section is not None:
                lines.append("")
                lines.append(f"Content (section matching '{search}'):")
                lines.append(section)
            else:
                lines.append("")
                lines.append(f"Content ('{search}' not found in text):")
                lines.append(text)
        else:
            lines.append("")
            lines.append("Content:")
            lines.append(text)
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


async def _list_comments(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    limit: int = 10,
    offset: int = 0,
    order: str = "desc",
) -> str:
    """Return a paginated, formatted list of comments for an issue.

    Only journals with a non-empty ``notes`` body are returned; pure
    field-change journals are excluded.
    """
    # Validate inputs up front — fail fast before DB work.
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        raise ValidationError("limit must be an integer between 1 and 50")
    if not isinstance(offset, int) or offset < 0:
        raise ValidationError("offset must be a non-negative integer")
    if order not in ("asc", "desc"):
        raise ValidationError("order must be 'asc' or 'desc'")

    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    # Permission check BEFORE any data is returned.
    await _require_permission(session, user, issue.project_id, "view_issues")

    comments, total = await _journal_svc.list_comments(session, issue.id, limit=limit, offset=offset, order=order)

    end = offset + len(comments)
    header = f"Comments for {issue.display_key} ({total} total, showing {offset}..{end}):"
    lines: list[str] = [header, ""]

    if not comments:
        lines.append("  (none)")
    else:
        for j in comments:
            author = j.user.display_name if j.user else "(unknown)"
            lines.append(f"[#{j.id}] {author}  {j.created_at}")
            body = j.notes or ""
            for body_line in body.splitlines() or [""]:
                lines.append(f"  {body_line}")
            lines.append("")

    if end < total:
        lines.append(f"(more: use offset={end})")

    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_READ,
        "list_comments",
        {"issue_ref": issue_ref, "limit": limit, "offset": offset, "order": order},
    )
    return "\n".join(lines).rstrip() + "\n"


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
        session,
        project.id,
        title,
        text,
        user,
        parent_slug=parent_slug,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_CREATED,
        "create_wiki",
        {"project_key": project_key, "slug": page.slug, "title": title},
        project_id=project.id,
    )
    return (
        f"Created wiki page '{page.title}' (slug: {page.slug}).\n"
        f"Version: {content.version}\nLock version: {page.lock_version}"
    )


# ---------------------------------------------------------------------------
# Wiki — delete / restore
# ---------------------------------------------------------------------------


async def _delete_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    cascade_children: bool = False,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "delete_wiki_pages")
    page, _content = await _wiki_svc.get_page(session, project.id, slug)
    deleted_ids = await _wiki_svc.delete_page(session, page.id, user, cascade_children=cascade_children)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_DELETED,
        "delete_wiki",
        {"project_key": project_key, "slug": slug, "deleted_ids": deleted_ids},
        project_id=project.id,
    )
    count = len(deleted_ids)
    suffix = "s" if count > 1 else ""
    return f"Deleted {count} wiki page{suffix} (slug: {slug}, cascade={cascade_children})."


async def _restore_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    cascade: bool = True,
) -> str:
    from specivo.models.wiki import Wiki, WikiPage

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "delete_wiki_pages")

    # Find deleted page by slug (without the deleted_at IS NULL filter)
    wiki_stmt = select(Wiki).where(Wiki.project_id == project.id)
    wiki_result = await session.execute(wiki_stmt)
    wiki = wiki_result.scalar_one_or_none()
    if wiki is None:
        raise NotFoundError(f"Wiki page '{slug}' not found")

    stmt = select(WikiPage).where(
        WikiPage.wiki_id == wiki.id,
        WikiPage.slug == slug,
        WikiPage.deleted_at.isnot(None),
    )
    result = await session.execute(stmt)
    page = result.scalar_one_or_none()
    if page is None:
        raise NotFoundError(f"Deleted wiki page '{slug}' not found")

    restored_ids = await _wiki_svc.restore_page(session, page.id, cascade=cascade)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_RESTORED,
        "restore_wiki",
        {"project_key": project_key, "slug": slug, "restored_ids": restored_ids},
        project_id=project.id,
    )
    count = len(restored_ids)
    suffix = "s" if count > 1 else ""
    return f"Restored {count} wiki page{suffix} (slug: {slug}, cascade={cascade})."


# ---------------------------------------------------------------------------
# Wiki — metadata (parent, title, protected)
# ---------------------------------------------------------------------------


async def _update_wiki_metadata(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    parent_slug: str | None = None,
    title: str | None = None,
    protected: bool | None = None,
) -> str:
    """Update wiki page metadata (parent, title, protected) without editing content."""
    if parent_slug is None and title is None and protected is None:
        return "Error: at least one of parent_slug, title, or protected must be provided."

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, _content = await _wiki_svc.get_page(session, project.id, slug)

    changes: list[str] = []
    audit_changes: dict[str, dict[str, object]] = {}

    # --- title (rename) ---
    if title is not None:
        old_title = page.title
        old_slug = page.slug
        page = await _wiki_svc.rename_page(session, page.id, title, page.lock_version)
        changes.append(f"title -> '{page.title}' (new slug: {page.slug})")
        audit_changes["title"] = {"old": old_title, "new": page.title}
        audit_changes["slug"] = {"old": old_slug, "new": page.slug}

    # --- parent ---
    if parent_slug is not None:
        old_parent_id = page.parent_id
        if parent_slug == "":
            page.parent_id = None
            changes.append("parent -> (root)")
        else:
            parent_page, _parent_content = await _wiki_svc.get_page(session, project.id, parent_slug)
            page.parent_id = parent_page.id
            changes.append(f"parent -> '{parent_slug}'")
        await session.flush()
        audit_changes["parent_id"] = {"old": old_parent_id, "new": page.parent_id}

    # --- protected ---
    if protected is not None:
        old_protected = page.protected
        page.protected = protected
        await session.flush()
        changes.append(f"protected -> {protected}")
        audit_changes["protected"] = {"old": old_protected, "new": protected}

    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_UPDATED,
        "update_wiki_metadata",
        {"project_key": project_key, "slug": slug, "changes": audit_changes},
        project_id=project.id,
    )
    return f"Updated wiki page '{page.title}': " + ", ".join(changes) + "."


# ---------------------------------------------------------------------------
# Wiki — section operations
# ---------------------------------------------------------------------------


async def _append_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    text: str,
    position: str = "end",
) -> str:
    """Append text to a wiki page at a given position.

    ``position`` is either ``"end"`` (default) or ``"after:## Heading Name"``
    to insert after a specific section.
    """
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""

    if position == "end":
        new_text = current.rstrip("\n") + "\n\n" + text if current.strip() else text
    elif position.startswith("after:"):
        heading_query = position[len("after:") :]
        sections = _split_into_sections(current)
        match = _find_section(sections, heading_query)
        if match is None:
            return (
                f"Error: heading '{heading_query}' not found in wiki page '{page.title}'.\n"
                f"Content length: {len(current)} chars."
            )
        sec_idx, section = match
        sec_level = section["level"]

        # Find the end of this section (including children):
        # next section at same-or-higher level
        insert_before_line: int | None = None
        for s in sections[sec_idx + 1 :]:
            if s["heading"] is not None and s["level"] <= sec_level:
                insert_before_line = s["start"]
                break

        lines = current.split("\n")
        if insert_before_line is not None:
            # Insert text before the next same-or-higher-level heading
            before = "\n".join(lines[:insert_before_line]).rstrip("\n")
            after = "\n".join(lines[insert_before_line:])
            new_text = before + "\n\n" + text + "\n\n" + after
        else:
            # No next same-level heading; append at end
            new_text = current.rstrip("\n") + "\n\n" + text
    else:
        return f"Error: invalid position '{position}'. Use 'end' or 'after:## Heading Name'."

    page, new_content = await _wiki_svc.update_page(
        session,
        page.id,
        new_text,
        user,
        lock_version=page.lock_version,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_UPDATED,
        "append_wiki",
        {"project_key": project_key, "slug": slug, "position": position},
    )
    return (
        f"Appended to wiki page '{page.title}' (version {new_content.version}).\n"
        f"Position: {position}. New content length: {len(new_text)} chars."
    )


async def _read_wiki_section(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    heading: str,
    include_children: bool = True,
) -> str:
    """Read a single section from a wiki page by heading."""
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    text = content.text or ""

    sections = _split_into_sections(text)
    match = _find_section(sections, heading)
    if match is None:
        return (
            f"Error: heading '{heading}' not found in wiki page '{page.title}'.\n"
            f"Available headings: {', '.join(s['heading'] for s in sections if s['heading'])}"
        )

    sec_idx, section = match
    sec_level = section["level"]
    lines = text.split("\n")

    if include_children:
        # Include everything until the next same-or-higher-level heading
        end_line: int | None = None
        for s in sections[sec_idx + 1 :]:
            if s["heading"] is not None and s["level"] <= sec_level:
                end_line = s["start"]
                break
        if end_line is not None:
            result_lines = lines[section["start"] : end_line]
        else:
            result_lines = lines[section["start"] :]
    else:
        # Stop at the first sub-heading
        end_line = None
        for s in sections[sec_idx + 1 :]:
            if s["heading"] is not None:
                end_line = s["start"]
                break
        if end_line is not None:
            result_lines = lines[section["start"] : end_line]
        else:
            result_lines = lines[section["start"] :]

    result_text = "\n".join(result_lines).strip()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_READ,
        "read_wiki_section",
        {"project_key": project_key, "slug": slug, "heading": heading},
    )
    return f"Wiki: {page.title} (section)\n\n{result_text}"


async def _replace_wiki_section(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    heading: str,
    text: str,
) -> str:
    """Replace a section's body while preserving the heading line."""
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""

    sections = _split_into_sections(current)
    match = _find_section(sections, heading)
    if match is None:
        return (
            f"Error: heading '{heading}' not found in wiki page '{page.title}'.\n"
            f"Available headings: {', '.join(s['heading'] for s in sections if s['heading'])}"
        )

    sec_idx, section = match
    sec_level = section["level"]
    lines = current.split("\n")

    # Find the end of this section (next same-or-higher-level heading)
    end_line: int | None = None
    for s in sections[sec_idx + 1 :]:
        if s["heading"] is not None and s["level"] <= sec_level:
            end_line = s["start"]
            break

    # Build new content: heading line + new body + rest
    heading_line = section["heading"]
    before = "\n".join(lines[: section["start"]])
    if end_line is not None:
        after = "\n".join(lines[end_line:])
    else:
        after = ""

    parts = []
    if before.strip():
        parts.append(before.rstrip("\n"))
    parts.append(heading_line + "\n\n" + text.strip())
    if after.strip():
        parts.append(after.lstrip("\n"))
    new_text = "\n\n".join(parts)

    page, new_content = await _wiki_svc.update_page(
        session,
        page.id,
        new_text,
        user,
        lock_version=page.lock_version,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_UPDATED,
        "replace_wiki_section",
        {"project_key": project_key, "slug": slug, "heading": heading},
    )
    return (
        f"Replaced section '{heading}' in wiki page '{page.title}' "
        f"(version {new_content.version}).\n"
        f"New content length: {len(new_text)} chars."
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
        lines.append(f"  {s.id}  {s.name}  [{s.category}]")
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
# Metadata schemas
# ---------------------------------------------------------------------------


async def _list_metadata_schemas(
    session: AsyncSession,
    user: User,
    project_key: str,
    tracker_id: int | None = None,
    content_type: str | None = None,
) -> str:
    """Discover metadata schemas for a project. Agents call this to learn
    what metadata fields are available before creating/updating issues."""
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")

    svc = MetadataSchemaService()
    schemas = await svc.list_for_project(session, project.id, content_type=content_type)
    if tracker_id is not None:
        schemas = [s for s in schemas if s.tracker_id is None or s.tracker_id == tracker_id]

    if not schemas:
        return f"No metadata schemas configured for project {project.key}."

    lines = [f"Metadata schemas for {project.key}:", ""]
    for s in schemas:
        scope = f"tracker_id={s.tracker_id}" if s.tracker_id else "all trackers"
        preset = f"  (preset: {s.preset_slug})" if s.preset_slug else ""
        lines.append(f"  id={s.id}  [{s.content_type}] {s.name} ({scope}){preset}")
        props = s.schema_definition.get("properties", {})
        for field_name, field_def in props.items():
            ftype = field_def.get("type", "any")
            desc = field_def.get("description", "")
            enum = field_def.get("enum")
            extra = f"  values: {enum}" if enum else ""
            lines.append(f"    {field_name}: {ftype}  {desc}{extra}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Metadata schema management (create/update/delete)
# ---------------------------------------------------------------------------


async def _create_metadata_schema(
    session: AsyncSession,
    user: User,
    project_key: str,
    name: str,
    schema: dict,
    content_type: str = "issue",
    tracker_id: int | None = None,
    description: str | None = None,
) -> str:
    """Create a metadata schema for a project.

    Requires the ``manage_project`` permission on the target project.
    The mutation is recorded in the security audit log
    (``METADATA_SCHEMA_CREATED``).

    *schema* is the full JSON Schema body (the ``schema_definition``
    field on the underlying model). Use specivo_list_metadata_schemas
    to inspect existing schemas first.
    """
    from specivo.schemas.metadata_schema import MetadataSchemaCreate
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_project")

    payload = MetadataSchemaCreate(
        name=name,
        tracker_id=tracker_id,
        content_type=content_type,
        description=description,
        schema_definition=schema,
    )
    svc = MetadataSchemaService()
    created = await svc.create(session, project.id, payload)
    await _log_tool(
        session,
        user,
        AuditEvent.METADATA_SCHEMA_CREATED,
        "create_metadata_schema",
        {
            "project_key": project.key,
            "schema_id": created.id,
            "name": created.name,
            "tracker_id": created.tracker_id,
        },
        project_id=project.id,
    )
    return (
        f"Created metadata schema id={created.id} name='{created.name}' "
        f"content_type={created.content_type} tracker_id={created.tracker_id} "
        f"in project {project.key}."
    )


async def _update_metadata_schema(
    session: AsyncSession,
    user: User,
    project_key: str,
    schema_id: int,
    name: str | None = None,
    tracker_id: int | None = None,
    schema: dict | None = None,
    description: str | None = None,
) -> str:
    """Patch a metadata schema.

    Requires the ``manage_project`` permission on the target project.
    The mutation is recorded in the security audit log
    (``METADATA_SCHEMA_UPDATED``).

    Only provided fields are changed. To inspect the current
    definition before patching, call specivo_list_metadata_schemas.
    """
    from specivo.schemas.metadata_schema import MetadataSchemaUpdate
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_project")

    svc = MetadataSchemaService()
    existing = await svc.get_by_id(session, schema_id, project.id)

    update_kwargs: dict[str, Any] = {}
    if name is not None:
        update_kwargs["name"] = name
    if tracker_id is not None:
        update_kwargs["tracker_id"] = tracker_id
    if schema is not None:
        update_kwargs["schema_definition"] = schema
    if description is not None:
        update_kwargs["description"] = description
    if not update_kwargs:
        return "Error: no fields provided to update"

    payload = MetadataSchemaUpdate(**update_kwargs)
    updated = await svc.update(session, existing, payload)
    changed_fields = sorted(update_kwargs.keys())
    await _log_tool(
        session,
        user,
        AuditEvent.METADATA_SCHEMA_UPDATED,
        "update_metadata_schema",
        {
            "project_key": project.key,
            "schema_id": updated.id,
            "name": updated.name,
            "tracker_id": updated.tracker_id,
            "changed_fields": changed_fields,
        },
        project_id=project.id,
    )
    return (
        f"Updated metadata schema id={updated.id} name='{updated.name}' "
        f"in project {project.key}. Changed: {', '.join(changed_fields)}."
    )


async def _delete_metadata_schema(
    session: AsyncSession,
    user: User,
    project_key: str,
    schema_id: int,
) -> str:
    """Delete a metadata schema.

    Requires the ``manage_project`` permission on the target project.
    The mutation is recorded in the security audit log
    (``METADATA_SCHEMA_DELETED``).

    Fails with a conflict error if any issue still has metadata
    matching the schema's defined keys.
    """
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_project")

    svc = MetadataSchemaService()
    existing = await svc.get_by_id(session, schema_id, project.id)
    schema_name = existing.name
    schema_tracker_id = existing.tracker_id
    await svc.delete_safe(session, existing)
    await _log_tool(
        session,
        user,
        AuditEvent.METADATA_SCHEMA_DELETED,
        "delete_metadata_schema",
        {
            "project_key": project.key,
            "schema_id": schema_id,
            "name": schema_name,
            "tracker_id": schema_tracker_id,
        },
        project_id=project.id,
    )
    return f"Deleted metadata schema id={schema_id} name='{schema_name}' from project {project.key}."


# ---------------------------------------------------------------------------
# Metadata (per-key set/delete/append/remove)
# ---------------------------------------------------------------------------


# Serialized metadata blob size cap (bytes of JSON output).  Enforced
# after the op is applied — prevents unbounded growth via repeated
# ``append`` calls.
_METADATA_MAX_BYTES = 16 * 1024


def _detect_lossy_number(value: Any) -> str | None:
    """Detect a numeric value that has lost precision via JSON parsing.

    When an MCP client emits an identifier-shaped token like ``49830031...e9999``
    without quoting it, the JSON-RPC layer parses it as a Python ``float`` (often
    ``inf`` or scientific notation). The original lexical form is unrecoverable.
    Return a human-readable description of the offending value, or ``None`` if
    the value is safe.
    """
    import math

    if isinstance(value, list):
        for item in value:
            offender = _detect_lossy_number(item)
            if offender is not None:
                return offender
        return None
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return repr(value)
        # Floats serialized in scientific notation are the exact ambiguity we
        # care about — a quoted string would never have produced this shape.
        import json as _json

        rendered = _json.dumps(value)
        if "e" in rendered.lower():
            return rendered
    return None


async def _metadata(
    session: AsyncSession,
    user: User,
    target_ref: str,
    key: str,
    op: str,
    value: Any = None,
) -> str:
    """Per-key metadata mutation dispatched through the target registry.

    Supported ops:

    * ``set``     -- ``metadata[key] = value`` (any JSON value).
    * ``get``     -- return ``metadata[key]`` as a JSON-serialized string,
      or the literal ``"(not set)"`` if the key is missing.  Read-only.
    * ``delete``  -- pop *key*; silent no-op if missing.
    * ``append``  -- append to a list at *key*.  Scalar values push one
      element; list values extend.  Missing key creates a new list.
      Errors if the existing value is not a list.
    * ``remove``  -- remove matching items from a list at *key*.  Scalar
      removes a single matching element; list removes each matching
      element.  Errors if the existing value is not a list.

    Recoverable errors (bad op, wrong type, stale version, oversize
    blob) are returned as ``"Error: ..."`` strings.  Hard errors
    (permission denied, unknown target, missing entity) raise.
    """
    import json

    from sqlalchemy.orm.exc import StaleDataError

    from specivo.core.metadata_targets import get_metadata_target_registry

    registry = get_metadata_target_registry()
    scheme, ref = registry.parse_ref(target_ref)
    target = registry.get(scheme)
    if target is None:
        known = ", ".join(registry.schemes()) or "(none)"
        return f"Error: unknown metadata target scheme '{scheme}'. Known schemes: {known}"

    if not key:
        return "Error: key must be a non-empty string"

    valid_ops = {"set", "get", "delete", "append", "remove"}
    if op not in valid_ops:
        return f"Error: invalid op '{op}'. Must be one of: {', '.join(sorted(valid_ops))}"

    if op in {"set", "append", "remove"}:
        offender = _detect_lossy_number(value)
        if offender is not None:
            return (
                f"Error: value {offender} arrived as a number with lost precision "
                "(scientific notation, inf, or nan). Identifier-like inputs "
                "(commit hashes, PR ids, tokens) must be passed as JSON strings — "
                "quote the value, e.g. \"4983e31...\" instead of 4983e31..."
            )

    # Resolve & permission check
    try:
        entity = await target.resolve(session, ref, user)
    except NotFoundError:
        return f"Error: {scheme} '{ref}' not found"
    required_permission = target.read_permission if op == "get" else target.permission
    await _require_permission(session, user, target.project_id_of(entity), required_permission)

    metadata = target.get_metadata(entity)

    # Read-only path — return without mutating, flushing, or journaling.
    if op == "get":
        # Overlay project-derived (computed) metadata for issues so the
        # effective value is returned even though it is never stored.
        if scheme == "issue":
            metadata = merge_computed(
                metadata, await load_project_settings(session, target.project_id_of(entity))
            )
        if key not in metadata:
            return "(not set)"
        try:
            return json.dumps(metadata[key], default=str, ensure_ascii=False)
        except TypeError:
            return f"Error: value at key '{key}' is not JSON-serializable"

    # Apply op to a local copy.
    if op == "set":
        metadata[key] = value
    elif op == "delete":
        metadata.pop(key, None)
    elif op == "append":
        existing = metadata.get(key)
        if existing is None:
            metadata[key] = list(value) if isinstance(value, list) else [value]
        elif isinstance(existing, list):
            if isinstance(value, list):
                existing.extend(value)
            else:
                existing.append(value)
        else:
            return f"Error: cannot append to key '{key}': existing value is {type(existing).__name__}, expected array"
    elif op == "remove":
        existing = metadata.get(key)
        if existing is None:
            # silent no-op — nothing to remove
            pass
        elif isinstance(existing, list):
            drop = value if isinstance(value, list) else [value]
            metadata[key] = [item for item in existing if item not in drop]
        else:
            return f"Error: cannot remove from key '{key}': existing value is {type(existing).__name__}, expected array"

    # Size cap (serialized JSON, post-op)
    try:
        serialized = json.dumps(metadata, default=str)
    except (TypeError, ValueError) as exc:
        return f"Error: metadata value is not JSON-serializable: {exc}"
    if len(serialized.encode("utf-8")) > _METADATA_MAX_BYTES:
        return f"Error: metadata blob would exceed {_METADATA_MAX_BYTES} bytes after this operation"

    # Persist through the target.
    try:
        updated_entity = await target.set_metadata(session, entity, metadata, user)
    except StaleDataError:
        return "Error: issue was modified by another request, retry"
    except ValidationError as exc:
        return f"Error: {exc.message}"
    except ConflictError as exc:
        return f"Error: {exc.message}"
    await session.flush()

    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_UPDATED,
        "metadata",
        {"target_ref": target_ref, "key": key, "op": op},
        project_id=target.project_id_of(updated_entity),
    )
    display = target.display_ref(updated_entity)
    return f"Updated metadata on {display}: {op} key={key}"


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
        session,
        user,
        AuditEvent.MEMBERS_LISTED,
        "list_members",
        {"project_key": project_key},
        project_id=project.id,
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


async def _list_relations(
    session: AsyncSession,
    user: User,
    issue_ref: str,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "view_issues")
    rows = await _relation_svc.list_for_issue(session, issue)
    await _log_tool(
        session,
        user,
        AuditEvent.RELATION_LISTED,
        "list_relations",
        {"issue_ref": issue_ref},
        project_id=issue.project_id,
    )
    if not rows:
        return f"No relations for {issue.display_key}."
    lines = [f"Relations for {issue.display_key} ({len(rows)} total):", ""]
    for r in rows:
        delay_str = f"  delay={r['delay']}d" if r.get("delay") else ""
        lines.append(f"  #{r['id']}  {r['relation_type']}  {r['issue_to_key']}{delay_str}")
    return "\n".join(lines)


async def _add_relation(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    issue_to_key: str,
    relation_type: str,
    delay: int | None = None,
) -> str:
    from specivo.schemas.relation import VALID_RELATION_TYPES

    if relation_type not in VALID_RELATION_TYPES:
        valid = ", ".join(sorted(VALID_RELATION_TYPES))
        return f"Error: invalid relation_type '{relation_type}'. Must be one of: {valid}"

    issue_from = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue_from.project_id, "manage_issue_relations")
    issue_to = await _issue_svc.get_by_display_key(session, issue_to_key, user=user)

    relation = await _relation_svc.create(
        session=session,
        issue_from=issue_from,
        issue_to=issue_to,
        relation_type=relation_type,
        delay=delay,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RELATION_ADDED,
        "add_relation",
        {"issue_ref": issue_ref, "issue_to_key": issue_to_key, "relation_type": relation_type},
        project_id=issue_from.project_id,
    )
    return f"Created relation #{relation.id}: {issue_from.display_key} {relation_type} {issue_to.display_key}"


async def _remove_relation(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    relation_id: int,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "manage_issue_relations")
    await _relation_svc.delete(session, relation_id, user)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RELATION_REMOVED,
        "remove_relation",
        {"issue_ref": issue_ref, "relation_id": relation_id},
        project_id=issue.project_id,
    )
    return f"Relation #{relation_id} removed from {issue.display_key}."


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
        session,
        user,
        AuditEvent.TIME_LOGGED,
        "log_time",
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
        session,
        user,
        AuditEvent.VERSIONS_LISTED,
        "list_versions",
        {"project_key": project_key},
        project_id=project.id,
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
        name=name,
        description=description,
        status=status,
        effective_date=due_date,
    )
    version = await _version_svc.create(session, project, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.VERSION_CREATED,
        "create_version",
        {"project_key": project_key, "name": name},
        project_id=project.id,
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
        name=name,
        description=description,
        status=status,
        effective_date=due_date,
    )
    version = await _version_svc.update(session, version, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.VERSION_UPDATED,
        "update_version",
        {"project_key": project_key, "version_id": version_id},
        project_id=version.project_id,
    )
    return f"Updated version '{version.name}' (ID: {version.id})."


async def _delete_version(
    session: AsyncSession,
    user: User,
    project_key: str,
    version_id: int,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_versions")
    version = await _version_svc.get_by_id(session, version_id)
    if version.project_id != project.id:
        raise NotFoundError(message="Version not found in this project")

    # Block deletion while issues reference this version
    from specivo.models.issue import Issue

    count_q = select(func.count()).where(Issue.fixed_version_id == version_id)
    issue_count: int = (await session.execute(count_q)).scalar_one()
    if issue_count > 0:
        return (
            f"Cannot delete version '{version.name}': "
            f"{issue_count} issue(s) still reference it. "
            f"Reassign or clear their fixed version first."
        )

    name = version.name
    await _version_svc.delete(session, version)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.VERSION_DELETED,
        "delete_version",
        {"project_key": project_key, "version_id": version_id, "name": name},
        project_id=project.id,
    )
    return f"Deleted version '{name}' (ID: {version_id}) from {project_key}."


# ---------------------------------------------------------------------------
# Recurring patterns
# ---------------------------------------------------------------------------


def _parse_byday(byday: str | None) -> list[str] | None:
    """Parse a comma-separated BYDAY string into a list of weekday tokens.

    Accepts forms like ``"MO,WE,FR"`` or ``"1MO,-1FR"``. Tokens are upper-cased
    and stripped; an empty/None input yields None (leave unset).
    """
    if not byday:
        return None
    tokens = [tok.strip().upper() for tok in byday.split(",") if tok.strip()]
    return tokens or None


def _parse_occurrence_at(value: str) -> datetime:
    """Parse an ISO-8601 datetime string into a tz-aware UTC-comparable datetime.

    A naive input is interpreted as UTC. Raises ValueError on a bad string,
    surfaced to the agent via the standard ``Error: ...`` return path.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _resolve_pattern_in_project(
    session: AsyncSession,
    project_id: int,
    pattern_id: int,
) -> Any:
    """Fetch a pattern and assert it belongs to *project_id*.

    Raises NotFoundError when the pattern is missing or scoped to another
    project (mirrors the version tools' cross-project guard).
    """
    pattern = await _recurring_pattern_svc.get_by_id(session, pattern_id)
    if pattern.project_id != project_id:
        raise NotFoundError(message="Recurring pattern not found in this project")
    return pattern


async def _list_recurring_patterns(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    patterns = await _recurring_pattern_svc.list_for_project(session, project.id)
    lines = [f"Recurring patterns for {project.key} ({len(patterns)} total):", ""]
    now = utcnow()
    for p in patterns:
        state = "enabled" if p.enabled else "disabled"
        rule = f"{p.freq}/{p.rrule_interval}"
        # Cheap next-occurrence hint: expand a short window from now.
        next_hint = ""
        try:
            exdates, _ = await _recurring_pattern_svc._load_exceptions(session, p.id)
            window_end = now + timedelta(days=p.creation_lead_time_days)
            occ = expand_occurrences(_recurring_pattern_svc.build_spec(p), now, window_end, exdates)
            if occ:
                next_hint = f"  next: {occ[0].isoformat()}"
        except Exception:
            next_hint = ""
        lines.append(
            f"  [{p.id}] {p.name} ({rule}, {p.anchor_mode}, {state}){next_hint}"
        )
    if not patterns:
        lines.append("  (none)")
    await _log_tool(
        session,
        user,
        AuditEvent.RECURRING_PATTERNS_LISTED,
        "list_recurring_patterns",
        {"project_key": project_key},
        project_id=project.id,
    )
    return "\n".join(lines)


async def _create_recurring_pattern(
    session: AsyncSession,
    user: User,
    project_key: str,
    name: str,
    template_subject: str,
    template_tracker_id: int,
    freq: str,
    dtstart: str,
    rrule_interval: int = 1,
    byday: str | None = None,
    rrule_count: int | None = None,
    rrule_raw: str | None = None,
    anchor_mode: str = "fixed",
    base_date_strategy: str = "scheduled",
    timezone: str = "UTC",
    template_description: str | None = None,
    template_priority_id: int | None = None,
    template_assigned_to_id: int | None = None,
    creation_lead_time_days: int = 30,
    enabled: bool = True,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_recurring_tasks")
    try:
        data = RecurringPatternCreate(
            name=name,
            template_subject=template_subject,
            template_tracker_id=template_tracker_id,
            template_description=template_description,
            template_priority_id=template_priority_id,
            template_assigned_to_id=template_assigned_to_id,
            freq=freq,  # type: ignore[arg-type]
            rrule_interval=rrule_interval,
            byday=_parse_byday(byday),
            rrule_count=rrule_count,
            rrule_raw=rrule_raw,
            anchor_mode=anchor_mode,  # type: ignore[arg-type]
            base_date_strategy=base_date_strategy,  # type: ignore[arg-type]
            dtstart=_parse_occurrence_at(dtstart),
            timezone=timezone,
            creation_lead_time_days=creation_lead_time_days,
            enabled=enabled,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    pattern = await _recurring_pattern_svc.create(session, project, data, user)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RECURRING_PATTERN_CREATED,
        "create_recurring_pattern",
        {"project_key": project_key, "name": name},
        project_id=project.id,
    )
    return f"Created recurring pattern '{pattern.name}' (ID: {pattern.id}) in {project.key}."


async def _update_recurring_pattern(
    session: AsyncSession,
    user: User,
    project_key: str,
    pattern_id: int,
    name: str | None = None,
    template_subject: str | None = None,
    template_description: str | None = None,
    freq: str | None = None,
    rrule_interval: int | None = None,
    byday: str | None = None,
    rrule_count: int | None = None,
    rrule_raw: str | None = None,
    anchor_mode: str | None = None,
    base_date_strategy: str | None = None,
    dtstart: str | None = None,
    timezone: str | None = None,
    creation_lead_time_days: int | None = None,
    enabled: bool | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_recurring_tasks")
    pattern = await _resolve_pattern_in_project(session, project.id, pattern_id)

    # Build the update from only the provided fields (PATCH semantics).
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if template_subject is not None:
        updates["template_subject"] = template_subject
    if template_description is not None:
        updates["template_description"] = template_description
    if freq is not None:
        updates["freq"] = freq
    if rrule_interval is not None:
        updates["rrule_interval"] = rrule_interval
    if byday is not None:
        updates["byday"] = _parse_byday(byday)
    if rrule_count is not None:
        updates["rrule_count"] = rrule_count
    if rrule_raw is not None:
        updates["rrule_raw"] = rrule_raw
    if anchor_mode is not None:
        updates["anchor_mode"] = anchor_mode
    if base_date_strategy is not None:
        updates["base_date_strategy"] = base_date_strategy
    if dtstart is not None:
        updates["dtstart"] = _parse_occurrence_at(dtstart)
    if timezone is not None:
        updates["timezone"] = timezone
    if creation_lead_time_days is not None:
        updates["creation_lead_time_days"] = creation_lead_time_days
    if enabled is not None:
        updates["enabled"] = enabled

    # Optimistic locking: the tool just loaded the pattern, so pass its current
    # lock_version. The schema requires it (mirroring the web/REST edit path);
    # a stale version raises ConflictError, surfaced to the agent as an error.
    updates["lock_version"] = pattern.lock_version

    try:
        data = RecurringPatternUpdate(**updates)
    except ValueError as exc:
        return f"Error: {exc}"
    pattern = await _recurring_pattern_svc.update(session, pattern, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RECURRING_PATTERN_UPDATED,
        "update_recurring_pattern",
        {"project_key": project_key, "pattern_id": pattern_id},
        project_id=project.id,
    )
    return f"Updated recurring pattern '{pattern.name}' (ID: {pattern.id})."


async def _delete_recurring_pattern(
    session: AsyncSession,
    user: User,
    project_key: str,
    pattern_id: int,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_recurring_tasks")
    pattern = await _resolve_pattern_in_project(session, project.id, pattern_id)
    name = pattern.name
    await _recurring_pattern_svc.delete(session, pattern)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RECURRING_PATTERN_DELETED,
        "delete_recurring_pattern",
        {"project_key": project_key, "pattern_id": pattern_id, "name": name},
        project_id=project.id,
    )
    return f"Deleted recurring pattern '{name}' (ID: {pattern_id}) from {project_key}."


async def _skip_recurrence_occurrence(
    session: AsyncSession,
    user: User,
    project_key: str,
    pattern_id: int,
    occurrence_at: str,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_recurring_tasks")
    pattern = await _resolve_pattern_in_project(session, project.id, pattern_id)
    try:
        occurrence = _parse_occurrence_at(occurrence_at)
    except ValueError as exc:
        return f"Error: invalid occurrence_at: {exc}"
    await _recurring_pattern_svc.skip_occurrence(session, pattern, occurrence)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RECURRENCE_OCCURRENCE_SKIPPED,
        "skip_recurrence_occurrence",
        {"project_key": project_key, "pattern_id": pattern_id, "occurrence_at": occurrence.isoformat()},
        project_id=project.id,
    )
    return (
        f"Skipped occurrence {occurrence.isoformat()} for recurring pattern "
        f"'{pattern.name}' (ID: {pattern_id})."
    )


async def _list_recurrence_occurrences(
    session: AsyncSession,
    user: User,
    project_key: str,
    pattern_id: int,
    days: int | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    pattern = await _resolve_pattern_in_project(session, project.id, pattern_id)

    settings = get_settings()
    window_days = days if days is not None else pattern.creation_lead_time_days
    window_days = min(window_days, settings.recurring_tasks_max_lead_time_days)

    now = utcnow()
    window_end = now + timedelta(days=window_days)

    # Skip exceptions (EXDATEs) so the preview matches generation reality.
    exdates, _overrides = await _recurring_pattern_svc._load_exceptions(session, pattern.id)
    occurrences = expand_occurrences(
        _recurring_pattern_svc.build_spec(pattern), now, window_end, exdates
    )

    lines = [
        f"Upcoming occurrences for '{pattern.name}' (ID: {pattern_id}), "
        f"next {window_days} days ({len(occurrences)} total):",
        "",
    ]
    for occ in occurrences:
        lines.append(f"  {occ.isoformat()}")
    if not occurrences:
        lines.append("  (none)")

    await _log_tool(
        session,
        user,
        AuditEvent.RECURRENCE_OCCURRENCES_LISTED,
        "list_recurrence_occurrences",
        {"project_key": project_key, "pattern_id": pattern_id, "days": window_days},
        project_id=project.id,
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sprints
# ---------------------------------------------------------------------------


async def _list_sprints(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    sprints = await _sprint_svc.list_for_project(session, project.id)
    lines = [f"Sprints for {project.key} ({len(sprints)} total):", ""]
    for s in sprints:
        dates = ""
        if s.start_date and s.end_date:
            dates = f"  {s.start_date} → {s.end_date}"
        elif s.start_date:
            dates = f"  {s.start_date} → ?"
        vel = ""
        if s.velocity_snapshot:
            vel = f" | velocity: {s.velocity_snapshot.get('completed_issues', '?')} completed"
        lines.append(f"  [{s.id}] {s.name} ({s.status}){dates}{vel}")
    if not sprints:
        lines.append("  (none)")
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "list_sprints",
        {"project_key": project_key},
        project_id=project.id,
    )
    return "\n".join(lines)


async def _create_sprint(
    session: AsyncSession,
    user: User,
    project_key: str,
    name: str,
    goal: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    data = SprintCreate(name=name, goal=goal, start_date=start_date, end_date=end_date)
    sprint = await _sprint_svc.create(session, project, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "create_sprint",
        {"project_key": project_key, "name": name, "sprint_id": sprint.id},
        project_id=project.id,
    )
    return f"Created sprint '{sprint.name}' (ID: {sprint.id}) in {project.key}. Status: {sprint.status}"


async def _update_sprint(
    session: AsyncSession,
    user: User,
    project_key: str,
    sprint_id: int,
    name: str | None = None,
    goal: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    sprint = await _sprint_svc.get_by_id(session, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(message="Sprint not found in this project")
    data = SprintUpdate(name=name, goal=goal, start_date=start_date, end_date=end_date)
    sprint = await _sprint_svc.update(session, sprint, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "update_sprint",
        {"project_key": project_key, "sprint_id": sprint_id},
        project_id=project.id,
    )
    return f"Updated sprint '{sprint.name}' (ID: {sprint.id}). Status: {sprint.status}"


async def _start_sprint(
    session: AsyncSession,
    user: User,
    project_key: str,
    sprint_id: int,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    sprint = await _sprint_svc.get_by_id(session, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(message="Sprint not found in this project")
    sprint = await _sprint_svc.start_sprint(session, sprint)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "start_sprint",
        {"project_key": project_key, "sprint_id": sprint_id},
        project_id=project.id,
    )
    return f"Started sprint '{sprint.name}' (ID: {sprint.id}). Status: active, start_date: {sprint.start_date}"


async def _complete_sprint(
    session: AsyncSession,
    user: User,
    project_key: str,
    sprint_id: int,
    move_incomplete_to_sprint_id: int | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    sprint = await _sprint_svc.get_by_id(session, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(message="Sprint not found in this project")
    sprint = await _sprint_svc.complete_sprint(session, sprint, move_incomplete_to_sprint_id)
    await session.flush()
    vel = sprint.velocity_snapshot or {}
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "complete_sprint",
        {"project_key": project_key, "sprint_id": sprint_id, "velocity": vel},
        project_id=project.id,
    )
    return (
        f"Completed sprint '{sprint.name}' (ID: {sprint.id}).\n"
        f"Total issues: {vel.get('total_issues', '?')}, "
        f"Completed: {vel.get('completed_issues', '?')}"
    )


async def _list_sprint_issues(
    session: AsyncSession,
    user: User,
    project_key: str,
    sprint_id: int,
    fields: str = "default",
    offset: int = 0,
    limit: int = 25,
) -> str:
    """List issues belonging to a sprint (or backlog if sprint_id=0)."""
    from specivo.models.issue import Issue

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")

    if fields not in ("minimal", "default", "full"):
        return "Error: fields must be one of: minimal, default, full"

    limit = max(1, min(limit, 100))

    is_backlog = sprint_id == 0

    if is_backlog:
        issues, total = await _sprint_svc.backlog_issues(
            session,
            project.id,
            offset=offset,
            limit=limit,
        )
        header = f"Backlog issues for {project.key} ({total} total, showing {offset}..{offset + len(issues)}):"
    else:
        # Validate sprint belongs to project
        sprint = await _sprint_svc.get_by_id(session, sprint_id)
        if sprint.project_id != project.id:
            raise NotFoundError(message="Sprint not found in this project")

        # Query issues for this sprint
        base_where = [Issue.project_id == project.id, Issue.sprint_id == sprint_id]

        count_result = await session.execute(select(func.count(Issue.id)).where(*base_where))
        total = count_result.scalar_one()

        stmt = (
            select(Issue)
            .where(*base_where)
            .options(
                selectinload(Issue.status),
                selectinload(Issue.tracker),
                selectinload(Issue.priority),
                selectinload(Issue.assigned_to),
            )
            .order_by(Issue.id.asc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        issues = list(result.scalars().all())
        header = (
            f'Issues in Sprint {sprint.id} "{sprint.name}" ({total} total, showing {offset}..{offset + len(issues)}):'
        )

    lines = [header, ""]
    settings_by_project = {
        pid: await load_project_settings(session, pid) for pid in {i.project_id for i in issues}
    }

    for i in issues:
        if fields == "minimal":
            lines.append(f"  {i.display_key}  {i.subject}")
        elif fields == "default":
            assigned = f" \u2192 {i.assigned_to.display_name}" if i.assigned_to else ""
            status_name = i.status.name if i.status else "?"
            tracker_name = i.tracker.name if i.tracker else "?"
            priority_name = i.priority.name if i.priority else "?"
            lines.append(f"  {i.display_key}  [{status_name}]  {tracker_name}  {priority_name}  {i.subject}{assigned}")
        else:  # full
            assigned = f" \u2192 {i.assigned_to.display_name}" if i.assigned_to else ""
            status_name = i.status.name if i.status else "?"
            tracker_name = i.tracker.name if i.tracker else "?"
            priority_name = i.priority.name if i.priority else "?"
            desc_preview = ""
            if i.description:
                desc_preview = i.description[:200].replace("\n", " ")
            lines.append(f"  {i.display_key}  [{status_name}]  {tracker_name}  {priority_name}  {i.subject}{assigned}")
            lines.append(f"    done={i.done_ratio}%  sprint_id={i.sprint_id}  version_id={i.fixed_version_id}")
            effective_metadata = merge_computed(i.issue_metadata, settings_by_project.get(i.project_id))
            if effective_metadata:
                lines.append(f"    metadata={effective_metadata}")
            if desc_preview:
                lines.append(f"    desc: {desc_preview}")
            lines.append("")

    if not issues:
        lines.append("  (none)")

    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "list_sprint_issues",
        {"project_key": project_key, "sprint_id": sprint_id, "fields": fields},
        project_id=project.id,
    )
    return "\n".join(lines)


async def _list_version_issues(
    session: AsyncSession,
    user: User,
    project_key: str,
    version_id: int,
    fields: str = "default",
    offset: int = 0,
    limit: int = 25,
) -> str:
    """List issues assigned to a version/release (or unversioned if version_id=0)."""
    from specivo.models.issue import Issue

    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")

    if fields not in ("minimal", "default", "full"):
        return "Error: fields must be one of: minimal, default, full"

    limit = max(1, min(limit, 100))

    is_unversioned = version_id == 0

    if is_unversioned:
        base_where = [Issue.project_id == project.id, Issue.fixed_version_id.is_(None)]
        header_prefix = f"Unversioned issues for {project.key}"
    else:
        version = await _version_svc.get_by_id(session, version_id)
        if version.project_id != project.id:
            raise NotFoundError(message="Version not found in this project")
        base_where = [Issue.project_id == project.id, Issue.fixed_version_id == version_id]
        header_prefix = f'Issues in version {version.id} "{version.name}"'

    count_result = await session.execute(select(func.count(Issue.id)).where(*base_where))
    total = count_result.scalar_one()

    stmt = (
        select(Issue)
        .where(*base_where)
        .options(
            selectinload(Issue.status),
            selectinload(Issue.tracker),
            selectinload(Issue.priority),
            selectinload(Issue.assigned_to),
        )
        .order_by(Issue.id.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    issues = list(result.scalars().all())
    header = f"{header_prefix} ({total} total, showing {offset}..{offset + len(issues)}):"

    lines = [header, ""]
    settings_by_project = {
        pid: await load_project_settings(session, pid) for pid in {i.project_id for i in issues}
    }

    for i in issues:
        if fields == "minimal":
            lines.append(f"  {i.display_key}  {i.subject}")
        elif fields == "default":
            assigned = f" \u2192 {i.assigned_to.display_name}" if i.assigned_to else ""
            status_name = i.status.name if i.status else "?"
            tracker_name = i.tracker.name if i.tracker else "?"
            priority_name = i.priority.name if i.priority else "?"
            lines.append(f"  {i.display_key}  [{status_name}]  {tracker_name}  {priority_name}  {i.subject}{assigned}")
        else:  # full
            assigned = f" \u2192 {i.assigned_to.display_name}" if i.assigned_to else ""
            status_name = i.status.name if i.status else "?"
            tracker_name = i.tracker.name if i.tracker else "?"
            priority_name = i.priority.name if i.priority else "?"
            desc_preview = ""
            if i.description:
                desc_preview = i.description[:200].replace("\n", " ")
            lines.append(f"  {i.display_key}  [{status_name}]  {tracker_name}  {priority_name}  {i.subject}{assigned}")
            lines.append(f"    done={i.done_ratio}%  sprint_id={i.sprint_id}  version_id={i.fixed_version_id}")
            effective_metadata = merge_computed(i.issue_metadata, settings_by_project.get(i.project_id))
            if effective_metadata:
                lines.append(f"    metadata={effective_metadata}")
            if desc_preview:
                lines.append(f"    desc: {desc_preview}")
            lines.append("")

    if not issues:
        lines.append("  (none)")

    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "list_version_issues",
        {"project_key": project_key, "version_id": version_id, "fields": fields},
        project_id=project.id,
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


async def _list_tags(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    rows = await _tag_svc.list_with_usage(session, project.id)
    lines = [f"Tags for {project.key} ({len(rows)} total):", ""]
    for tag, issue_count, wiki_count in rows:
        color = f"  {tag.color}" if tag.color else ""
        lines.append(f"  {tag.id}  {tag.name}{color}  (issues: {issue_count}, wiki: {wiki_count})")
    if not rows:
        lines.append("  (none)")
    await _log_tool(
        session, user, AuditEvent.TAGS_LISTED, "list_tags", {"project_key": project_key}, project_id=project.id
    )
    return "\n".join(lines)


async def _create_tag(
    session: AsyncSession,
    user: User,
    project_key: str,
    name: str,
    color: str | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_project")
    try:
        tag = await _tag_svc.create(session, project, TagCreate(name=name, color=color))
    except ConflictError as exc:
        return f"Error: {exc.message}"
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.TAG_CREATED, "create_tag",
        {"project_key": project_key, "name": tag.name}, project_id=project.id,
    )
    return f"Created tag '{tag.name}' (ID: {tag.id}) in {project.key}."


async def _update_tag(
    session: AsyncSession,
    user: User,
    project_key: str,
    tag_id: int,
    name: str | None = None,
    color: str | None = None,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_project")
    tag = await _tag_svc.get_by_id(session, tag_id)
    if tag.project_id != project.id:
        return f"Error: tag {tag_id} not found in project '{project.key}'"
    data = TagUpdate(name=name) if color is None else TagUpdate(name=name, color=color)
    try:
        tag = await _tag_svc.update(session, tag, data)
    except ConflictError as exc:
        return f"Error: {exc.message}"
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.TAG_UPDATED, "update_tag",
        {"project_key": project_key, "tag_id": tag_id, "name": tag.name}, project_id=project.id,
    )
    return f"Updated tag '{tag.name}' (ID: {tag.id}) in {project.key}."


async def _delete_tag(session: AsyncSession, user: User, project_key: str, tag_id: int) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    await _require_permission(session, user, project.id, "manage_project")
    tag = await _tag_svc.get_by_id(session, tag_id)
    if tag.project_id != project.id:
        return f"Error: tag {tag_id} not found in project '{project.key}'"
    name = tag.name
    await _tag_svc.delete(session, tag)
    await session.flush()
    await _log_tool(
        session, user, AuditEvent.TAG_DELETED, "delete_tag",
        {"project_key": project_key, "tag_id": tag_id, "name": name}, project_id=project.id,
    )
    return f"Deleted tag '{name}' from {project.key}."


async def _resolve_tag_target(session: AsyncSession, user: User, target_ref: str):
    """Resolve a tag target_ref to (kind, project, entity_id, display).

    Accepts an issue ref ('ACME-12' or 'issue:ACME-12') or a wiki ref
    ('wiki:ACME/some-slug').
    """
    if target_ref.startswith("wiki:"):
        ref = target_ref[len("wiki:") :]
        if "/" not in ref:
            raise ValueError("wiki target must be 'wiki:PROJECT_KEY/slug'")
        pkey, slug = ref.split("/", 1)
        project = await _project_svc.get_by_key(session, pkey.upper())
        await _project_svc.require_project_access(session, project, user)
        page, _ = await _wiki_svc.get_page(session, project.id, slug)
        return "wiki", project, page.id, f"wiki {project.key}/{page.slug}"

    ref = target_ref[len("issue:") :] if target_ref.startswith("issue:") else target_ref
    issue = await _issue_svc.get_by_display_key(session, ref, user=user)
    issue_project = await session.get(Project, issue.project_id)
    if issue_project is None:  # pragma: no cover - defensive
        raise NotFoundError(f"Project for issue '{ref}' not found")
    return "issue", issue_project, issue.id, issue.display_key


async def _tag(
    session: AsyncSession,
    user: User,
    target_ref: str,
    op: str,
    value: Any = None,
) -> str:
    """Read or mutate the tags on an issue or wiki page.

    Ops: get, add, remove, set. ``value`` is a tag name or list of names
    (ignored for get). Adding creates new tags on the fly.
    """
    valid_ops = {"get", "add", "remove", "set"}
    if op not in valid_ops:
        return f"Error: invalid op '{op}'. Must be one of: {', '.join(sorted(valid_ops))}"

    try:
        kind, project, entity_id, display = await _resolve_tag_target(session, user, target_ref)
    except NotFoundError as exc:
        return f"Error: {exc}"
    except ValueError as exc:
        return f"Error: {exc}"

    if op == "get":
        tags = (
            await _tag_svc.tags_for_issue(session, entity_id)
            if kind == "issue"
            else await _tag_svc.tags_for_wiki_page(session, entity_id)
        )
        await _log_tool(
            session, user, AuditEvent.TAGS_LISTED, "tag",
            {"target_ref": target_ref, "op": op}, project_id=project.id,
        )
        if not tags:
            return f"{display}: (no tags)"
        return f"{display}: " + ", ".join(t.name for t in tags)

    # Normalize value to a list of names.
    if value is None:
        names: list[str] = []
    elif isinstance(value, list):
        names = [str(v) for v in value]
    else:
        names = [str(value)]

    if op == "set":
        if kind == "issue":
            diff = await _tag_svc.set_issue_tags(session, project, entity_id, names, user)
        else:
            diff = await _tag_svc.set_wiki_page_tags(session, project, entity_id, names, user)
        await session.flush()
        for n in diff["added"]:
            await _log_tool(
                session, user, AuditEvent.TAG_ADDED, "tag",
                {"target_ref": target_ref, "name": n}, project_id=project.id,
            )
        for n in diff["removed"]:
            await _log_tool(
                session, user, AuditEvent.TAG_REMOVED, "tag",
                {"target_ref": target_ref, "name": n}, project_id=project.id,
            )
        return (
            f"Set tags on {display}: +{len(diff['added'])} / -{len(diff['removed'])} "
            f"(added: {', '.join(diff['added']) or 'none'}; removed: {', '.join(diff['removed']) or 'none'})"
        )

    if not names:
        return "Error: value (tag name) is required for add/remove"

    changed: list[str] = []
    if op == "add":
        for n in names:
            if kind == "issue":
                tag, created = await _tag_svc.add_to_issue(session, project, entity_id, n, user)
            else:
                tag, created = await _tag_svc.add_to_wiki_page(session, project, entity_id, n, user)
            if created:
                changed.append(tag.name)
                await _log_tool(
                    session, user, AuditEvent.TAG_ADDED, "tag",
                    {"target_ref": target_ref, "name": tag.name}, project_id=project.id,
                )
        await session.flush()
        return f"Added tags to {display}: {', '.join(changed) or '(already present)'}"

    # op == "remove"
    for n in names:
        existing_tag = await _tag_svc.get_by_name(session, project.id, n)
        if existing_tag is None:
            continue
        removed = (
            await _tag_svc.remove_from_issue(session, entity_id, existing_tag.id)
            if kind == "issue"
            else await _tag_svc.remove_from_wiki_page(session, entity_id, existing_tag.id)
        )
        if removed:
            changed.append(existing_tag.name)
            await _log_tool(
                session, user, AuditEvent.TAG_REMOVED, "tag",
                {"target_ref": target_ref, "name": existing_tag.name}, project_id=project.id,
            )
    await session.flush()
    return f"Removed tags from {display}: {', '.join(changed) or '(not present)'}"
