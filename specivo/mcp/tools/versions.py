"""MCP tools: versions and the issues assigned to them."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import (
    NotFoundError,
    ValidationError,
)
from specivo.mcp.tools._common import _get_project, _issue_svc, _log_tool, _require_permission, _version_svc
from specivo.models.user import User
from specivo.schemas.version import VersionCreate, VersionUpdate
from specivo.services.computed_metadata_service import load_project_settings, merge_computed
from specivo.services.security_audit_service import AuditEvent


async def _list_versions(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _get_project(session, user, project_key)
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
    project = await _get_project(session, user, project_key)
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
    project = await _get_project(session, user, project_key)  # validate project
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
    project = await _get_project(session, user, project_key)
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

    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")

    if fields not in ("minimal", "default", "full"):
        raise ValidationError("fields must be one of: minimal, default, full", field="fields")

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
    # Only the issues the caller may see, as in the issue list
    visibility = await _issue_svc.project_visible_issues_clause(session, user, project)
    if visibility is not None:
        base_where.append(visibility)

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
    settings_by_project = {pid: await load_project_settings(session, pid) for pid in {i.project_id for i in issues}}

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
