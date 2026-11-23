"""MCP tools: time entries and the per-user timer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import (
    NotFoundError,
    ValidationError,
)
from specivo.core.utils import utcnow
from specivo.mcp.client_context import record_working_issue
from specivo.mcp.tools._common import _get_project, _issue_svc, _log_tool, _time_entry_svc
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.time_entry import TimeEntryCreate
from specivo.services.security_audit_service import AuditEvent


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
    project = await _get_project(session, user, project_key)
    await _time_entry_svc.require_can_log(session, user, project)
    issue_id: int | None = None
    if issue_ref:
        issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
        issue_id = issue.id
        await _time_entry_svc.require_can_log(session, user, project, issue_id)

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


_TIMER_OPS = ("get", "start", "stop", "discard")


def _format_elapsed(seconds: int) -> str:
    """``H:MM:SS`` for a duration in seconds."""
    hours, rest = divmod(max(0, seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"


async def _resolve_activity(session: AsyncSession, activity: str | None) -> int:
    """Return the id of the active activity named or numbered *activity*, or the default one."""
    from specivo.models.time_entry import TimeEntryActivity

    if activity is None or not activity.strip():
        default = await _time_entry_svc.default_activity(session)
        if default is None:
            raise ValidationError(message="No active activities available", field="activity")
        return default.id
    value = activity.strip()
    stmt = select(TimeEntryActivity.id).where(TimeEntryActivity.active.is_(True))
    if value.isdigit():
        stmt = stmt.where(TimeEntryActivity.id == int(value))
    else:
        stmt = stmt.where(func.lower(TimeEntryActivity.name) == value.lower())
    activity_id = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if activity_id is None:
        raise ValidationError(
            message=f"Unknown activity '{value}'. Call specivo_list_lookups for the active activities.",
            field="activity",
        )
    return activity_id


async def _timer(
    session: AsyncSession,
    user: User,
    op: str,
    issue_ref: str | None = None,
    activity: str | None = None,
    comments: str | None = None,
) -> str:
    """Read or drive the caller's own timer.

    Ops: get; start (on *issue_ref*, stopping and logging a running timer
    first, as the REST start does, and saying whether it was logged or, when
    it could no longer be logged, discarded); stop (logs the time with *activity*, a
    name or id, else the default activity, and *comments*); discard (drops
    the timer without logging time). Starting and stopping need ``log_time``
    where the timer runs; stopping re-checks and discards a timer that can no
    longer be logged.
    """
    if op not in _TIMER_OPS:
        raise ValidationError(message=f"Invalid op '{op}'. Must be one of: {', '.join(_TIMER_OPS)}", field="op")

    if op == "get":
        summary = await _time_entry_svc.timer_summary(session, user)
        await _log_tool(session, user, AuditEvent.RESOURCE_ACCESS, "timer", {"op": op})
        if summary is None:
            return "No timer is running."
        target = summary.issue_key or summary.project_key
        lines = [f"Timer running on {target}" + (f": {summary.subject}" if summary.subject else "")]
        lines.append(f"Project: {summary.project_key}")
        lines.append(f"Started: {summary.started_at.isoformat()}")
        lines.append(f"Elapsed: {_format_elapsed(summary.elapsed_seconds)}")
        return "\n".join(lines)

    if op == "start":
        if not issue_ref:
            raise ValidationError(message="op='start' needs issue_ref", field="issue_ref")
        issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
        timer, replaced = await _time_entry_svc.start_timer_replacing(
            session, user, issue.project_id, issue.id, comments
        )
        await record_working_issue(session, user, issue)
        await _log_tool(
            session,
            user,
            AuditEvent.TIMER_STARTED,
            "timer",
            {"op": op, "issue_ref": issue.display_key},
            project_id=timer.project_id,
        )
        result = f"Started a timer on {issue.display_key}."
        if replaced is not None:
            target = replaced.issue_key or replaced.project_key
            if replaced.entry is not None:
                result += (
                    f"\nThe timer that was running was stopped first. Logged {replaced.entry.hours}h on {target} "
                    f"with the default activity.\nEntry ID: {replaced.entry.id}"
                )
            else:
                result += (
                    f"\nThe timer that was running on {target} was discarded: you may no longer log time there, "
                    "so no time was logged."
                )
        return result

    if op == "stop":
        if await _time_entry_svc.get_current_timer(session, user) is None:
            raise NotFoundError("No active timer")
        activity_id = await _resolve_activity(session, activity)
        entry = await _time_entry_svc.stop_timer(session, user, activity_id, comments)
        issue_label = ""
        if entry.issue_id is not None:
            issue_label = f" on {(await _issue_svc.get_by_id(session, entry.issue_id)).display_key}"
        project = await session.get(Project, entry.project_id)
        project_key = project.key if project is not None else str(entry.project_id)
        await _log_tool(
            session,
            user,
            AuditEvent.TIME_LOGGED,
            "timer",
            {"op": op, "project_key": project_key, "hours": str(entry.hours)},
            project_id=entry.project_id,
        )
        return f"Logged {entry.hours}h{issue_label} in {project_key}.\nEntry ID: {entry.id}"

    timer = await _time_entry_svc.discard_timer(session, user)
    elapsed = int((utcnow() - timer.started_at).total_seconds())
    await _log_tool(
        session,
        user,
        AuditEvent.TIMER_DISCARDED,
        "timer",
        {"op": op, "issue_id": timer.issue_id, "elapsed_seconds": max(0, elapsed)},
        project_id=timer.project_id,
    )
    return f"Discarded the timer after {_format_elapsed(elapsed)}; no time was logged."
