"""MCP tool implementations — testable async functions.

Each ``_*`` function accepts an explicit ``session`` and ``user``,
delegates to the service layer, and returns a formatted string.
The MCP ``@mcp.tool()`` wrappers in ``server.py`` resolve auth
and session, then call these functions.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import SEARCH_SNIPPET_MAX_CHARS
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.project_service import ProjectService
from specivo.services.search_service import SearchService
from specivo.services.wiki_service import WikiService

logger = logging.getLogger(__name__)

_issue_svc = IssueService()
_project_svc = ProjectService()
_wiki_svc = WikiService()
_search_svc = SearchService()
_journal_svc = JournalService()


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
    return "\n".join(lines)


async def _show_issue(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    metadata_only: bool = False,
    search: str | None = None,
) -> str:
    issue = await _issue_svc.get_by_display_key_with_relations(session, issue_ref, user=user)

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
    return f"Updated issue {updated.display_key}: {updated.subject}\nLock version: {updated.lock_version}"


async def _edit_description(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    search_text: str,
    replace_text: str,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
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
    return "\n".join(lines)


async def _list_wiki_pages(
    session: AsyncSession,
    user: User,
    project_key: str,
) -> str:
    project = await _project_svc.get_by_key(session, project_key)
    pages = await _wiki_svc.list_pages(session, project.id)
    lines = [f"Wiki pages for {project.key} ({len(pages)} total):", ""]
    for p in pages:
        lines.append(f"  {p.slug}  —  {p.title}")
    if not pages:
        lines.append("  (none)")
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
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""
    if search_text not in current:
        return f"Error: search text not found in wiki page '{page.title}'.\nContent length: {len(current)} chars."
    new_text = current.replace(search_text, replace_text, 1)
    page, new_content = await _wiki_svc.update_page(session, page.id, new_text, user, lock_version=page.lock_version)
    await session.flush()
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
    journal = await _journal_svc.add_comment(session, issue, user, notes)
    await session.flush()
    return f"Added comment to {issue.display_key} (journal #{journal.sequence}).\nNotes: {notes[:100]}"
