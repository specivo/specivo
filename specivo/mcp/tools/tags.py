"""MCP tools: tags and tagging issues and wiki pages."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import (
    NotFoundError,
    ValidationError,
)
from specivo.mcp.tools._common import (
    _get_project,
    _issue_svc,
    _log_tool,
    _project_svc,
    _require_permission,
    _tag_svc,
    _wiki_svc,
)
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.tag import MAX_TAG_NAMES, TAG_NAME_MAX, TagCreate, TagUpdate, normalize_tag_names
from specivo.services.security_audit_service import AuditEvent


async def _list_tags(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _get_project(session, user, project_key)
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_project")
    tag = await _tag_svc.create(session, project, TagCreate(name=name, color=color))
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.TAG_CREATED,
        "create_tag",
        {"project_key": project_key, "name": tag.name},
        project_id=project.id,
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_project")
    tag = await _tag_svc.get_by_id(session, tag_id)
    if tag.project_id != project.id:
        raise NotFoundError(f"tag {tag_id} not found in project '{project.key}'")
    data = TagUpdate(name=name) if color is None else TagUpdate(name=name, color=color)
    tag = await _tag_svc.update(session, tag, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.TAG_UPDATED,
        "update_tag",
        {"project_key": project_key, "tag_id": tag_id, "name": tag.name},
        project_id=project.id,
    )
    return f"Updated tag '{tag.name}' (ID: {tag.id}) in {project.key}."


async def _delete_tag(session: AsyncSession, user: User, project_key: str, tag_id: int) -> str:
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_project")
    tag = await _tag_svc.get_by_id(session, tag_id)
    if tag.project_id != project.id:
        raise NotFoundError(f"tag {tag_id} not found in project '{project.key}'")
    name = tag.name
    await _tag_svc.delete(session, tag)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.TAG_DELETED,
        "delete_tag",
        {"project_key": project_key, "tag_id": tag_id, "name": name},
        project_id=project.id,
    )
    return f"Deleted tag '{name}' from {project.key}."


def _clean_tag_names(value: Any) -> list[str]:
    """Normalise a tag name or list of names; raise ``ValueError`` when it breaks a limit.

    The same bounds as the REST tag schemas: a capped list of stripped,
    de-duplicated names, none longer than the tag name column. Blank names
    are dropped.
    """
    if value is None:
        names: list[str] = []
    elif isinstance(value, list):
        names = [str(v) for v in value]
    else:
        names = [str(value)]
    if len(names) > MAX_TAG_NAMES:
        raise ValueError(f"at most {MAX_TAG_NAMES} tag names per call")
    names = [n.strip() for n in names]
    if any(len(n) > TAG_NAME_MAX for n in names):
        raise ValueError(f"tag names must be at most {TAG_NAME_MAX} characters")
    return normalize_tag_names(names)


async def _add_tags(
    session: AsyncSession,
    user: User,
    tool_name: str,
    kind: str,
    project: Project,
    entity_id: int,
    target_ref: str,
    names: list[str],
) -> list[str]:
    """Apply *names* (from :func:`_clean_tag_names`) to an issue or wiki page, creating missing tags.

    Each new link is audited as ``TAG_ADDED``. Returns the names of the tags
    newly linked, as stored (an existing tag keeps its own spelling).
    """
    added: list[str] = []
    for name in names:
        if kind == "issue":
            tag, created = await _tag_svc.add_to_issue(session, project, entity_id, name, user)
        else:
            tag, created = await _tag_svc.add_to_wiki_page(session, project, entity_id, name, user)
        if created:
            added.append(tag.name)
            await _log_tool(
                session,
                user,
                AuditEvent.TAG_ADDED,
                tool_name,
                {"target_ref": target_ref, "name": tag.name},
                project_id=project.id,
            )
    await session.flush()
    return added


async def _resolve_tag_target(session: AsyncSession, user: User, target_ref: str, *, write: bool):
    """Resolve a tag target_ref to (kind, project, entity_id, display).

    Accepts an issue ref ('ACME-12' or 'issue:ACME-12') or a wiki ref
    ('wiki:ACME/some-slug'). Raises ``PermissionDeniedError`` unless *user*
    may read the target's tags, or change them when *write* is set.
    """
    check = _tag_svc.require_write if write else _tag_svc.require_read
    if target_ref.startswith("wiki:"):
        ref = target_ref[len("wiki:") :]
        if "/" not in ref:
            raise ValueError("wiki target must be 'wiki:PROJECT_KEY/slug'")
        pkey, slug = ref.split("/", 1)
        project = await _get_project(session, user, pkey.upper())
        await _project_svc.require_project_access(session, project, user)
        # Before the page lookup, so a refusal does not reveal which slugs exist.
        await check(session, user, "wiki", project)
        page, _ = await _wiki_svc.get_page(session, project.id, slug)
        return "wiki", project, page.id, f"wiki {project.key}/{page.slug}"

    ref = target_ref[len("issue:") :] if target_ref.startswith("issue:") else target_ref
    issue = await _issue_svc.get_by_display_key(session, ref, user=user)
    issue_project = await session.get(Project, issue.project_id)
    if issue_project is None:  # pragma: no cover - defensive
        raise NotFoundError(f"Project for issue '{ref}' not found")
    await check(session, user, "issue", issue_project)
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
        raise ValidationError(f"invalid op '{op}'. Must be one of: {', '.join(sorted(valid_ops))}", field="op")

    kind, project, entity_id, display = await _resolve_tag_target(session, user, target_ref, write=op != "get")

    if op == "get":
        tags = (
            await _tag_svc.tags_for_issue(session, entity_id)
            if kind == "issue"
            else await _tag_svc.tags_for_wiki_page(session, entity_id)
        )
        await _log_tool(
            session,
            user,
            AuditEvent.TAGS_LISTED,
            "tag",
            {"target_ref": target_ref, "op": op},
            project_id=project.id,
        )
        if not tags:
            return f"{display}: (no tags)"
        return f"{display}: " + ", ".join(t.name for t in tags)

    names = _clean_tag_names(value)

    if op == "set":
        if kind == "issue":
            diff = await _tag_svc.set_issue_tags(session, project, entity_id, names, user)
        else:
            diff = await _tag_svc.set_wiki_page_tags(session, project, entity_id, names, user)
        await session.flush()
        for n in diff["added"]:
            await _log_tool(
                session,
                user,
                AuditEvent.TAG_ADDED,
                "tag",
                {"target_ref": target_ref, "name": n},
                project_id=project.id,
            )
        for n in diff["removed"]:
            await _log_tool(
                session,
                user,
                AuditEvent.TAG_REMOVED,
                "tag",
                {"target_ref": target_ref, "name": n},
                project_id=project.id,
            )
        return (
            f"Set tags on {display}: +{len(diff['added'])} / -{len(diff['removed'])} "
            f"(added: {', '.join(diff['added']) or 'none'}; removed: {', '.join(diff['removed']) or 'none'})"
        )

    if not names:
        raise ValidationError("value (tag name) is required for add/remove", field="value")

    changed: list[str] = []
    if op == "add":
        changed = await _add_tags(session, user, "tag", kind, project, entity_id, target_ref, names)
        return f"Added tags to {display}: {', '.join(changed) or '(already present)'}"

    # op == "remove"  # noqa: ERA001
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
                session,
                user,
                AuditEvent.TAG_REMOVED,
                "tag",
                {"target_ref": target_ref, "name": existing_tag.name},
                project_id=project.id,
            )
    await session.flush()
    return f"Removed tags from {display}: {', '.join(changed) or '(not present)'}"
