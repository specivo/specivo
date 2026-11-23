"""MCP tools: issue comments and their change history."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.mcp.client_context import mcp_api_key_id_var
from specivo.mcp.tools._common import _issue_svc, _journal_svc, _log_tool, _require_permission
from specivo.models.journal import ATTACHMENT_DETAIL, SPRINT_DETAIL, JournalDetail
from specivo.models.user import User
from specivo.services.activity_lookups import ActivityLookups, activity_lookup_maps
from specivo.services.security_audit_service import AuditEvent


async def _add_comment(
    session: AsyncSession,
    user: User,
    issue_ref: str,
    notes: str,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "add_issue_notes")
    journal = await _journal_svc.add_comment(session, issue, user, notes, api_key_id=mcp_api_key_id_var.get())
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
    include_changes: bool = False,
) -> str:
    """Return a paginated, formatted list of comments for an issue.

    Only journals with a non-empty ``notes`` body are returned; pure
    field-change journals are excluded, unless *include_changes*: then every
    entry is listed with its field changes, named and left out as the issue
    page does for the same user (see :mod:`specivo.services.activity_lookups`).
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

    comments, total = await _journal_svc.list_comments(
        session, issue.id, limit=limit, offset=offset, order=order, include_changes=include_changes
    )
    names = await activity_lookup_maps(session, user, comments, people_by_login=False) if include_changes else None

    end = offset + len(comments)
    what = "History" if include_changes else "Comments"
    header = f"{what} for {issue.display_key} ({total} total, showing {offset}..{end}):"
    lines: list[str] = [header, ""]

    if not comments:
        lines.append("  (none)")
    else:
        for j in comments:
            author = j.user.display_name if j.user else "(unknown)"
            lines.append(f"[#{j.id}] {author}  {j.created_at}")
            if names is not None:
                shown = [d for d in j.details if not names.hides(d, anonymous=user.is_anonymous)]
                lines.extend(f"  * {_change_line(d, names)}" for d in shown)
            body = j.notes or ""
            if body or names is None:
                for body_line in body.splitlines() or [""]:
                    lines.append(f"  {body_line}")  # noqa: PERF401
            lines.append("")

    if end < total:
        lines.append(f"(more: use offset={end})")

    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_READ,
        "list_comments",
        {"issue_ref": issue_ref, "limit": limit, "offset": offset, "order": order},
        project_id=issue.project_id,
    )
    return "\n".join(lines).rstrip() + "\n"


def _change_line(detail: JournalDetail, names: ActivityLookups) -> str:
    """One field change as text: a value naming a record shows its name and id, when the user may see it."""
    if detail.property == "relation":
        verb = "added" if detail.new_value and not detail.old_value else "removed"
        return f"relation {detail.prop_key} {verb}: {detail.new_value or detail.old_value}"
    if detail.property == SPRINT_DETAIL:
        return f"sprint {names.sprint_name(detail)} (id {detail.old_value}) {detail.prop_key}"
    if detail.property == ATTACHMENT_DETAIL:
        removed = detail.old_value is not None and detail.new_value is None
        name = names.attachment_name(detail)
        verb = "deleted" if removed else "added"
        return f"file {name} {verb}" if name else f"file {verb}"
    if detail.prop_key in ("description", "issue_metadata"):
        return f"{detail.prop_key} updated"

    def shown(value: str | None) -> str:
        if value is None or value == "":
            return "(none)"
        name = names.name(detail, value)
        return f"{name} (id {value})" if name is not None else value

    field_name = names.field_label(detail)
    label = detail.prop_key if field_name is None else f"{field_name} ({detail.prop_key}, custom field)"
    return f"{label}: {shown(detail.old_value)} -> {shown(detail.new_value)}"
