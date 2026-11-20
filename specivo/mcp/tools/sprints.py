"""MCP tools: sprints and their issues."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import (
    NotFoundError,
    ValidationError,
)
from specivo.mcp.tools._common import _get_project, _issue_svc, _log_tool, _require_permission, _sprint_svc
from specivo.models.user import User
from specivo.schemas.sprint import SprintCreate, SprintUpdate
from specivo.services.computed_metadata_service import load_project_settings, merge_computed
from specivo.services.permission_service import Permission
from specivo.services.security_audit_service import AuditEvent


async def _list_sprints(session: AsyncSession, user: User, project_key: str) -> str:
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    sprints = await _sprint_svc.list_for_project(session, project.id)
    velocity = await _sprint_svc.velocity_for(session, sprints, user)
    lines = [f"Sprints for {project.key} ({len(sprints)} total):", ""]
    for s in sprints:
        dates = ""
        if s.start_date and s.end_date:
            dates = f"  {s.start_date} → {s.end_date}"
        elif s.start_date:
            dates = f"  {s.start_date} → ?"
        vel = ""
        snap = velocity.get(s.id)
        if snap:
            vel = f" | velocity: {snap['completed_issues']} completed"
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    data = SprintCreate(name=name, goal=goal, start_date=start_date, end_date=end_date)
    sprint = await _sprint_svc.create(session, project, data)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.SPRINT_CREATED,
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
    project = await _get_project(session, user, project_key)
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
        AuditEvent.SPRINT_UPDATED,
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    sprint = await _sprint_svc.get_by_id(session, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(message="Sprint not found in this project")
    sprint = await _sprint_svc.start_sprint(session, sprint, user)
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, Permission.MANAGE_SPRINTS)
    sprint = await _sprint_svc.get_by_id(session, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(message="Sprint not found in this project")
    sprint = await _sprint_svc.complete_sprint(session, sprint, user, move_to_sprint_id=move_incomplete_to_sprint_id)
    await session.flush()
    vel = (await _sprint_svc.velocity_for(session, [sprint], user))[sprint.id] or {}
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

    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")

    if fields not in ("minimal", "default", "full"):
        raise ValidationError("fields must be one of: minimal, default, full", field="fields")

    limit = max(1, min(limit, 100))

    is_backlog = sprint_id == 0

    if is_backlog:
        issues, total = await _sprint_svc.backlog_issues(
            session,
            project.id,
            user,
            offset=offset,
            limit=limit,
        )
        header = f"Backlog issues for {project.key} ({total} total, showing {offset}..{offset + len(issues)}):"
    else:
        # Validate sprint belongs to project
        sprint = await _sprint_svc.get_by_id(session, sprint_id)
        if sprint.project_id != project.id:
            raise NotFoundError(message="Sprint not found in this project")

        # Query the issues of this sprint the caller may see
        base_where = [Issue.project_id == project.id, Issue.sprint_id == sprint_id]
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
        header = (
            f'Issues in Sprint {sprint.id} "{sprint.name}" ({total} total, showing {offset}..{offset + len(issues)}):'
        )

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
        "list_sprint_issues",
        {"project_key": project_key, "sprint_id": sprint_id, "fields": fields},
        project_id=project.id,
    )
    return "\n".join(lines)
