"""MCP tools: issue relations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.mcp.tools._common import _issue_svc, _log_tool, _relation_svc, _require_permission
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent


async def _list_relations(
    session: AsyncSession,
    user: User,
    issue_ref: str,
) -> str:
    issue = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue.project_id, "view_issues")
    rows = await _relation_svc.list_for_issue(session, issue, user)
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
        raise ValidationError(
            f"invalid relation_type '{relation_type}'. Must be one of: {valid}", field="relation_type"
        )

    issue_from = await _issue_svc.get_by_display_key(session, issue_ref, user=user)
    await _require_permission(session, user, issue_from.project_id, "manage_issue_relations")
    issue_to = await _issue_svc.get_by_display_key(session, issue_to_key, user=user)

    relation = await _relation_svc.create(
        session=session,
        issue_from=issue_from,
        issue_to=issue_to,
        relation_type=relation_type,
        delay=delay,
        actor=user,
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
    await _relation_svc.delete(session, relation_id, user, via_issue=issue)
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
