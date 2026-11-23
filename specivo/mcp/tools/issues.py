"""MCP tools: list, show, create, update and move issues."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import (
    NotFoundError,
    ValidationError,
)
from specivo.mcp.client_context import mcp_api_key_id_var, record_working_issue
from specivo.mcp.tools._common import (
    _extract_section,
    _get_project,
    _issue_svc,
    _journal_svc,
    _log_tool,
    _notification_svc,
    _project_svc,
    _require_permission,
    _tag_svc,
    _version_svc,
)
from specivo.mcp.tools.links import _link_lines
from specivo.mcp.tools.tags import _add_tags, _clean_tag_names
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.tag import Tag
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.services.activity_lookups import metadata_value_names
from specivo.services.computed_metadata_service import load_project_settings, merge_computed
from specivo.services.issue_source_ref_service import imported_from
from specivo.services.metadata_fields import field_labels, humanise, issue_schemas
from specivo.services.permission_service import check_permission
from specivo.services.security_audit_service import AuditEvent


def _parse_metadata_filters(raw: list[str] | None) -> list[tuple[str, str]]:
    """Parse `["key=value", ...]` MCP input into validated `(key, value)` pairs.

    Returns an empty list when *raw* is None or empty. Raises ``ValueError``
    with a caller-actionable message when any item is malformed (no ``=``,
    empty key, etc.), which reaches the agent as a tool error.
    """
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            raise ValueError(f"metadata filter {item!r} must be a 'key=value' string")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"metadata filter {item!r} has an empty key")
        pairs.append((key, value))
    return pairs


async def _resolve_parent_ref(
    session: AsyncSession, user: User, parent_ref: str, project_id: int | None = None
) -> Issue:
    """Resolve *parent_ref* (a display key such as ``ACME-12``) to an issue *user* may see.

    With *project_id*, the parent must belong to that project too (subtasks
    never span projects). Raises ``NotFoundError`` with one message for every
    refusal: a malformed or unknown ref, a bare database id, an issue *user*
    may not see, one outside the API key's project limit and one in another
    project, so a refusal tells nothing about which issues exist.
    """
    not_found = NotFoundError(f"parent issue '{parent_ref}' not found")
    if "-" not in parent_ref:
        raise not_found
    try:
        parent = await _issue_svc.get_by_display_key(session, parent_ref.strip(), user=user)
    except NotFoundError:
        raise not_found from None
    if not await check_permission(user, parent.project_id, "view_issues", session):
        raise not_found
    if project_id is not None and parent.project_id != project_id:
        raise not_found
    return parent


async def _resolve_list_filters(
    session: AsyncSession,
    user: User,
    project: Project,
    *,
    assignee: str | None,
    tag: str | None,
    release: str | None,
) -> dict[str, object]:
    """The ``list_issues`` filters for the assignee, tag and release arguments, as the issue list reads them.

    Each takes a name or a numeric id; ``assignee`` also ``me`` and ``none``,
    ``release`` also ``none`` (see :mod:`specivo.services.issue_list_filters`).
    Only what the issue list's own pickers offer resolves: people issues of
    the project can be assigned to, the project's tags, and the versions the
    caller may see from the project (shared ones included). Anything else is
    a ``ValueError`` naming the argument, with the same message whether it
    does not exist or the caller may not see it.
    """
    from specivo.services.issue_list_filters import NO_VALUE

    filters: dict[str, object] = {}

    value = (assignee or "").strip()
    if value:
        lowered = value.casefold()
        if lowered == NO_VALUE:
            filters["assigned_to_id"] = NO_VALUE
        elif lowered == "me":
            filters["assigned_to_id"] = user.id
        else:
            match = User.id == int(value) if value.isdecimal() else func.lower(User.login) == lowered
            found = await session.scalar(
                select(User.id).where(match, _project_svc.assignable_users_clause(project)).limit(1)
            )
            if found is None:
                raise ValueError(
                    f"Unknown assignee '{value}' in {project.key}: use a login or id of someone issues "
                    "here can be assigned to, 'me' or 'none'."
                )
            filters["assigned_to_id"] = found

    value = (tag or "").strip()
    if value:
        match = Tag.id == int(value) if value.isdecimal() else func.lower(Tag.name) == value.casefold()
        found = await session.scalar(select(Tag.id).where(Tag.project_id == project.id, match).limit(1))
        if found is None:
            raise ValueError(f"Unknown tag '{value}' in {project.key}.")
        filters["tag_id"] = found

    value = (release or "").strip()
    if value:
        if value.casefold() == NO_VALUE:
            filters["version_id"] = NO_VALUE
        else:
            versions = await _version_svc.visible_versions(session, project, user)
            wanted = int(value) if value.isdecimal() else None
            matches = [
                v for v in versions if (v.id == wanted if wanted is not None else v.name.casefold() == value.casefold())
            ]
            if not matches:
                raise ValueError(
                    f"Unknown release '{value}' in {project.key}: use a version name or id visible here, or 'none'."
                )
            # A name shared by this project's own version and a shared one: the project's own wins.
            matches.sort(key=lambda v: v.project_id != project.id)
            filters["version_id"] = matches[0].id
    return filters


async def _list_issues(  # noqa: PLR0913
    session: AsyncSession,
    user: User,
    project_key: str,
    status: str = "open",
    sort: str = "created_at:desc",
    offset: int = 0,
    limit: int = 25,
    sprint_id: int | None = None,
    metadata_filters: list[str] | None = None,
    parent_ref: str | None = None,
    assignee: str | None = None,
    tag: str | None = None,
    release: str | None = None,
) -> str:
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    filters: dict = {"status": status}
    if sprint_id is not None:
        filters["sprint_id"] = sprint_id
    filters.update(await _resolve_list_filters(session, user, project, assignee=assignee, tag=tag, release=release))
    parent: Issue | None = None
    if parent_ref:
        parent = await _resolve_parent_ref(session, user, parent_ref)
        filters["parent_id"] = parent.id
    parsed_metadata = _parse_metadata_filters(metadata_filters)
    if parsed_metadata:
        filters["metadata_filters"] = parsed_metadata
    issues, total = await _issue_svc.list_issues(
        session,
        project_id=project.id,
        filters=filters,
        sort=sort,
        offset=offset,
        limit=limit,
        user=user,
    )
    scope = f"filter={status}"
    if sprint_id is not None:
        scope += f", sprint_id={sprint_id}"
    if parsed_metadata:
        scope += ", metadata=" + ",".join(f"{k}={v}" for k, v in parsed_metadata)
    if parent is not None:
        scope += f", parent={parent.display_key}"
    for name, value in (("assignee", assignee), ("tag", tag), ("release", release)):
        if value is not None and value.strip():
            scope += f", {name}={value.strip()}"
    lines = [f"Issues for {project.key} ({total} total, {scope}):", ""]
    # One batched query for every issue on this page — never per-issue lookups.
    tags_by_issue = await _tag_svc.tags_for_issues(session, [i.id for i in issues])
    for i in issues:
        line = f"  {i.display_key}  [{i.status.name}]  {i.subject}"
        tags = tags_by_issue.get(i.id) or []
        if tags:
            line += "  [tags: " + ", ".join(t.name for t in tags) + "]"
        lines.append(line)
    if not issues:
        lines.append("  (none)")
    await _log_tool(session, user, AuditEvent.ISSUE_LISTED, "list_issues", {"project_key": project_key})
    return "\n".join(lines)


async def _metadata_field_lines(session: AsyncSession, user: User, issue: Issue, metadata: dict) -> list[str]:
    """The issue's metadata fields by name, a person or version a field holds by name when *user* may see it.

    Named as the issue page names them (:mod:`specivo.services.activity_lookups`), people by display name.
    """
    schemas = await issue_schemas(session, {issue.project_id})
    applicable = [s for s in schemas if s.tracker_id is None or s.tracker_id == issue.tracker_id]
    labels = field_labels(schemas)
    names = await metadata_value_names(session, user, applicable, metadata, people_by_login=False)

    def shown(key: str, value: object) -> str:
        known = names.get(key, {})
        values = value if isinstance(value, list) else [value]
        texts = [f"{known[str(v)]} (id {v})" if str(v) in known else str(v) for v in values]
        return ", ".join(texts) if isinstance(value, list) else texts[0]

    lines = ["Metadata fields:"]
    for key, value in metadata.items():
        lines.append(f"  {labels.get(key) or humanise(key)} ({key}): {shown(key, value)}")
    return lines


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
    source = await imported_from(session, issue.id)
    if source is not None:
        lines.append(f"Imported from: {source.label}")
    lines.append(f"Created: {issue.created_at}")
    lines.append(f"Updated: {issue.updated_at}")
    if issue.start_date:
        lines.append(f"Start date: {issue.start_date}")
    if issue.due_date:
        lines.append(f"Due date: {issue.due_date}")
    if issue.estimated_hours:
        lines.append(f"Estimated hours: {issue.estimated_hours}")
    if issue.original_estimate is not None:
        lines.append(f"Original estimate hours: {issue.original_estimate}")
    if issue.remaining_estimate is not None:
        lines.append(f"Remaining estimate hours: {issue.remaining_estimate}")
    lines.append(f"Done: {issue.done_ratio}%")
    lines.extend(await _hierarchy_lines(session, user, issue))
    effective_metadata = merge_computed(issue.issue_metadata, await load_project_settings(session, issue.project_id))
    if effective_metadata:
        lines.append(f"Metadata: {effective_metadata}")
        lines.extend(await _metadata_field_lines(session, user, issue, effective_metadata))
    tags = await _tag_svc.tags_for_issue(session, issue.id)
    if tags:
        lines.append("Tags: " + ", ".join(t.name for t in tags))
    lines.append(f"Lock version: {issue.lock_version}")
    comments_count = await _journal_svc.count_comments(session, issue.id)
    lines.append(f"Comments: {comments_count}")
    lines.extend(await _commit_lines(session, issue.id))
    lines.extend(await _pull_request_lines(session, issue.id))
    lines.extend(await _link_lines(session, user, ("issue", issue.id)))

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


async def _hierarchy_lines(session: AsyncSession, user: User, issue: Issue) -> list[str]:
    """``Parent: REF subject`` and ``Children: REF, ...`` (direct children), naming only issues *user* may see."""
    lines: list[str] = []
    if issue.parent_id is not None:
        try:
            parent = await _issue_svc.get_by_id(session, issue.parent_id, user=user)
        except NotFoundError:
            parent = None
        if parent is not None and await check_permission(user, parent.project_id, "view_issues", session):
            lines.append(f"Parent: {parent.display_key} {parent.subject}")
    children = await _issue_svc.list_visible_children(session, issue, user)
    if children:
        lines.append("Children: " + ", ".join(c.display_key for c in children))
    return lines


async def _commit_lines(session: AsyncSession, issue_id: int) -> list[str]:
    """``Commits: N`` and the newest linked commits (short SHA, first line, branch, URL); none when unlinked."""
    from specivo.core.constants import REPOSITORY_MCP_COMMIT_LIMIT
    from specivo.services.repository_service import commits_for_issue

    commits, total = await commits_for_issue(session, issue_id, REPOSITORY_MCP_COMMIT_LIMIT)
    if not total:
        return []
    lines = [f"Commits: {total}"]
    for c in commits:
        line = f"- {c.short_sha} {c.first_line}"
        if c.branch:
            line += f" [{c.branch}]"
        if c.url:
            line += f" {c.url}"
        lines.append(line)
    if total > len(commits):
        lines.append(f"- ... and {total - len(commits)} more")
    return lines


async def _pull_request_lines(session: AsyncSession, issue_id: int) -> list[str]:
    """``Pull requests: N`` and each linked request (number, state, title, branch, URL); none when unlinked.

    Then one ``Development:`` line summing up reviews, CI, deployments and
    releases, when there are any (details: ``specivo_issue_development``).
    """
    from specivo.services.repository_service import development_for_issue

    development = await development_for_issue(session, issue_id, limit=1)
    pull_requests = development.pull_requests
    lines = _pull_request_list(pull_requests)
    summary_line = _development_summary_line(development)
    if summary_line:
        lines.append(summary_line)
    return lines


def _development_summary_line(development: Any) -> str | None:
    """``Development: reviews approved; CI failing (3 runs); deployed to production 2026-...; released in v1.2``."""
    summary = development.summary
    if summary is None:
        return None
    parts: list[str] = []
    if summary.reviews:
        parts.append(f"reviews {summary.reviews.replace('_', ' ')}")
    if summary.ci:
        parts.append(f"CI {summary.ci} ({summary.counts.get('ci', 0)} runs)")
    for env in summary.deployments:
        where = env.environment_type or env.environment
        parts.append(f"deployed to {where} {env.deployed_at.isoformat(timespec='minutes')}")
    if summary.released_in:
        parts.append("released in " + ", ".join(r.tag_name for r in summary.released_in[:5]))
    if not parts:
        return None
    return "Development: " + "; ".join(parts) + " (specivo_issue_development for details)"


def _pull_request_list(pull_requests: list) -> list[str]:
    if not pull_requests:
        return []
    lines = [f"Pull requests: {len(pull_requests)}"]
    for pr in pull_requests:
        sigil = "!" if pr.provider == "gitlab" else "#"
        state = f"{pr.state}, closing" if pr.closing else pr.state
        line = f"- {sigil}{pr.number} [{state}] {pr.title}"
        if pr.source_branch:
            line += f" ({pr.source_branch})"
        if pr.url:
            line += f" {pr.url}"
        lines.append(line)
    return lines


async def _issue_development(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    kinds: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """The issue's development activity as JSON, the same document ``GET /api/v1/issues/{ref}/development/`` returns."""
    import json

    from specivo.core.constants import DEVELOPMENT_TIMELINE_MAX_LIMIT, DEVELOPMENT_TIMELINE_MAX_OFFSET
    from specivo.schemas.repository import DEVELOPMENT_KINDS
    from specivo.services.repository_service import development_for_issue

    if kinds is not None:
        unknown = sorted(set(kinds) - set(DEVELOPMENT_KINDS))
        if unknown:
            raise ValidationError(f"Unknown kinds: {', '.join(unknown)}. Use: {', '.join(DEVELOPMENT_KINDS)}")
    if not 1 <= limit <= DEVELOPMENT_TIMELINE_MAX_LIMIT:
        raise ValidationError(f"limit must be between 1 and {DEVELOPMENT_TIMELINE_MAX_LIMIT}")
    if not 0 <= offset <= DEVELOPMENT_TIMELINE_MAX_OFFSET:
        raise ValidationError(f"offset must be between 0 and {DEVELOPMENT_TIMELINE_MAX_OFFSET}")
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "view_issues")
    development = await development_for_issue(session, issue.id, kinds=kinds, offset=offset, limit=limit)
    await _log_tool(
        session, user, AuditEvent.ISSUE_READ, "issue_development", {"issue_ref": issue_ref}, project_id=issue.project_id
    )
    return json.dumps(development.model_dump(mode="json"), ensure_ascii=False)


async def _create_issue(  # noqa: PLR0913
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
    is_private: bool | None = None,
    parent_ref: str | None = None,
    tags: list[str] | None = None,
) -> str:
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "add_issues")
    tag_names = _clean_tag_names(tags)
    parent: Issue | None = None
    if parent_ref:
        # Checked before anything is written. A refusal rolls the tool call back anyway.
        parent = await _resolve_parent_ref(session, user, parent_ref, project.id)
        await _issue_svc.check_parent(session, None, parent)
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
        is_private=is_private,
        parent_id=parent.id if parent is not None else None,
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
    if await _works_on(session, user, issue):
        await record_working_issue(session, user, issue)
    result = (
        f"Created issue {issue.display_key}: {issue.subject}\nStatus: {issue.status_id}, Priority: {issue.priority_id}"
    )
    if parent is not None:
        result += f"\nParent: {parent.display_key}"
    if tag_names:
        applied = await _add_tags(
            session, user, "create_issue", "issue", project, issue.id, issue.display_key, tag_names
        )
        result += "\nTags: " + ", ".join(applied)
    return result


async def _update_issue(  # noqa: PLR0913
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
    parent_ref: str | None = None,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "edit_issues")
    parent: Issue | None = None
    if parent_ref:
        # Checked before the service applies the other fields. A refusal
        # rolls the tool call back anyway.
        parent = await _resolve_parent_ref(session, user, parent_ref, issue.project_id)
        await _issue_svc.check_parent(session, issue, parent)
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
    if parent_ref is not None:
        # The service takes 0 for "move to root".
        supplied["parent_id"] = parent.id if parent is not None else 0
    data = IssueUpdate(**supplied, lock_version=issue.lock_version)
    status_before, assignee_before = issue.status_id, issue.assigned_to_id
    updated = await _issue_svc.update(session, issue, data, user, notes=notes, api_key_id=mcp_api_key_id_var.get())
    await session.flush()
    # The same notifications as an edit over REST.
    await _notification_svc.notify_issue_updated(
        session,
        updated,
        user,
        old_assignee_id=assignee_before,
        assigned_to_id=data.assigned_to_id,
        status_given=data.status_id is not None,
    )
    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_UPDATED,
        "update_issue",
        {"issue_ref": issue_ref},
        project_id=updated.project_id,
    )
    if await _starts_work(session, user, updated, status_before, assignee_before) or await _resumes_work(
        session, user, updated
    ):
        await record_working_issue(session, user, updated)
    return f"Updated issue {updated.display_key}: {updated.subject}\nLock version: {updated.lock_version}"


async def _is_active(session: AsyncSession, issue: Issue) -> bool:
    status = await session.get(IssueStatus, issue.status_id)
    return status is not None and status.category == "active"


async def _works_on(session: AsyncSession, user: User, issue: Issue) -> bool:
    """Whether *user* creating *issue* means working on it: it is assigned to them or in an ``active`` status."""
    return issue.assigned_to_id == user.id or await _is_active(session, issue)


async def _resumes_work(session: AsyncSession, user: User, issue: Issue) -> bool:
    """Whether *issue* is, after an update, in an ``active`` status and assigned to *user*.

    Marks an issue the agent resumes even when the update changed nothing
    (it was already In Progress and theirs).
    """
    return issue.assigned_to_id == user.id and await _is_active(session, issue)


async def _starts_work(
    session: AsyncSession, user: User, issue: Issue, status_before: int, assignee_before: int | None
) -> bool:
    """Whether an update means *user* has started work on *issue*.

    True when it moved the issue into a status of the ``active`` group, or
    assigned it to *user*. Setting a value the issue already had is not a change.
    """
    if issue.assigned_to_id == user.id and assignee_before != user.id:
        return True
    if issue.status_id != status_before:
        return await _is_active(session, issue)
    return False


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
    target = await _get_project(session, user, target_project_key.upper())
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
        raise ValidationError(
            f"search text not found in {issue.display_key} description.\nDescription length: {len(current)} chars.",
            field="search_text",
        )
    new_description = current.replace(search_text, replace_text, 1)
    # Only set ``description`` — omitting every other field keeps
    # ``model_fields_set`` minimal so the service does not touch
    # fixed_version_id / sprint_id. Use ``**`` to avoid mypy insisting on
    # every other optional field being passed explicitly.
    supplied: dict[str, Any] = {"description": new_description}
    data = IssueUpdate(**supplied, lock_version=issue.lock_version)
    updated = await _issue_svc.update(session, issue, data, user, api_key_id=mcp_api_key_id_var.get())
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_UPDATED,
        "edit_description",
        {"issue_ref": issue_ref},
        project_id=updated.project_id,
    )
    return (
        f"Updated description of {updated.display_key}.\n"
        f"Replaced '{search_text}' -> '{replace_text}'.\n"
        f"Lock version: {updated.lock_version}"
    )
