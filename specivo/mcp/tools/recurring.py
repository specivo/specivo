"""MCP tools: recurring issue patterns and their occurrences."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import (
    NotFoundError,
    ValidationError,
)
from specivo.core.utils import utcnow
from specivo.mcp.tools._common import _get_project, _log_tool, _recurring_pattern_svc, _require_permission
from specivo.models.user import User
from specivo.schemas.recurring_pattern import RecurringPatternCreate, RecurringPatternUpdate
from specivo.services.recurrence import expand_occurrences
from specivo.services.security_audit_service import AuditEvent


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
    which reaches the agent as a tool error.
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
    project = await _get_project(session, user, project_key)
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
        except Exception:  # noqa: BLE001
            next_hint = ""
        lines.append(f"  [{p.id}] {p.name} ({rule}, {p.anchor_mode}, {state}){next_hint}")
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


async def _create_recurring_pattern(  # noqa: PLR0913
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_recurring_tasks")
    try:
        data = RecurringPatternCreate(
            name=name,
            template_subject=template_subject,
            template_tracker_id=template_tracker_id,
            template_description=template_description,
            template_priority_id=template_priority_id,
            template_assigned_to_id=template_assigned_to_id,
            freq=freq,
            rrule_interval=rrule_interval,
            byday=_parse_byday(byday),
            rrule_count=rrule_count,
            rrule_raw=rrule_raw,
            anchor_mode=anchor_mode,
            base_date_strategy=base_date_strategy,
            dtstart=_parse_occurrence_at(dtstart),
            timezone=timezone,
            creation_lead_time_days=creation_lead_time_days,
            enabled=enabled,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
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


async def _update_recurring_pattern(  # noqa: C901, PLR0913
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
    project = await _get_project(session, user, project_key)
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
        raise ValidationError(str(exc)) from exc
    pattern = await _recurring_pattern_svc.update(session, pattern, data, user)
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
    project = await _get_project(session, user, project_key)
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
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_recurring_tasks")
    pattern = await _resolve_pattern_in_project(session, project.id, pattern_id)
    try:
        occurrence = _parse_occurrence_at(occurrence_at)
    except ValueError as exc:
        raise ValidationError(f"invalid occurrence_at: {exc}", field="occurrence_at") from exc
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
    return f"Skipped occurrence {occurrence.isoformat()} for recurring pattern '{pattern.name}' (ID: {pattern_id})."


async def _list_recurrence_occurrences(
    session: AsyncSession,
    user: User,
    project_key: str,
    pattern_id: int,
    days: int | None = None,
) -> str:
    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")
    pattern = await _resolve_pattern_in_project(session, project.id, pattern_id)

    settings = get_settings()
    window_days = days if days is not None else pattern.creation_lead_time_days
    window_days = min(window_days, settings.recurring_tasks_max_lead_time_days)

    now = utcnow()
    window_end = now + timedelta(days=window_days)

    # Skip exceptions (EXDATEs) so the preview matches generation reality.
    exdates, _overrides = await _recurring_pattern_svc._load_exceptions(session, pattern.id)
    occurrences = expand_occurrences(_recurring_pattern_svc.build_spec(pattern), now, window_end, exdates)

    lines = [
        f"Upcoming occurrences for '{pattern.name}' (ID: {pattern_id}), "
        f"next {window_days} days ({len(occurrences)} total):",
        "",
    ]
    for occ in occurrences:
        lines.append(f"  {occ.isoformat()}")  # noqa: PERF401
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
