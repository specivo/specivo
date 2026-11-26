"""The agent session world (``agent_session_world``) plus a Hermes run, for the issue agent timeline tests.

Not a test module. On top of :func:`build_session_world` (block of ``pub``:
root ``old`` with its child ``live``):

- ``hermes``: the webhook half of ``live``'s run (same account and client
  session id, no parent of its own), with project-less Hermes audit rows: a
  tool call, an approval asked and denied, a subagent start; and one tool call
  in project Q, which P's pages must not show;
- ``sub``: the subagent, a webhook child of ``hermes`` that ended interrupted,
  with usage of 0.25 USD (profile ``night``, unattended);
- ``child_issue``: a private sub-issue of ``pub`` with an MCP session ``child``
  on it, which only readers who may see private issues see;
- ``dev``: a member of P with ``manage_project`` and ``view_ai_usage`` whose
  AI usage visibility is ``own`` and who may not see private issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import bounded_log
from tests.factories.user import UserFactory
from tests.integration.agent_session_world import SessionWorld, _issue, _usage, build_session_world


@dataclass
class TimelineWorld:
    w: SessionWorld
    hermes: AgentSession
    sub: AgentSession
    child_issue: Issue
    child: AgentSession
    dev: User


async def build_timeline_world(db: AsyncSession, prefix: str) -> TimelineWorld:
    w = await build_session_world(db, prefix)
    low = prefix.lower()
    now = utcnow()

    role = Role(
        name=f"{prefix} Lead",
        permissions=["view_issues", "manage_project", "view_ai_usage"],
        builtin=0,
        issues_visibility="default",
        ai_usage_visibility="own",
    )
    dev = UserFactory.build(login=f"{low}_dev", status="active")
    db.add_all([role, dev])
    await db.flush()
    member = Member(user_id=dev.id, project_id=w.p.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))

    hermes = AgentSession(
        api_key_id=None,
        user_id=w.agent.id,
        client_type="webhook",
        client_name="hermes-agent",
        external_session_id=f"{low}-live",
        started_at=now - timedelta(minutes=55),
        last_activity_at=now - timedelta(minutes=2),
    )
    db.add(hermes)
    await db.flush()
    sub = AgentSession(
        api_key_id=None,
        user_id=w.agent.id,
        client_type="webhook",
        client_name="hermes-agent",
        external_session_id=f"{low}-sub",
        parent_external_session_id=f"{low}-live",
        parent_session_id=hermes.id,
        started_at=now - timedelta(minutes=45),
        last_activity_at=now - timedelta(minutes=35),
        ended_at=now - timedelta(minutes=35),
    )
    db.add(sub)

    lookups = tuple(SimpleNamespace(id=i) for i in (w.pub.tracker_id, w.pub.status_id, w.pub.priority_id))
    child_issue = await _issue(
        db, w.p, lookups, w.manager, f"{prefix} hidden kitten", is_private=True, parent_id=w.pub.id
    )
    child = AgentSession(
        api_key_id=w.live.api_key_id,
        user_id=w.agent.id,
        issue_id=child_issue.id,
        client_type="mcp",
        client_name="claude-code",
        connection_hash="c" * 64,
        started_at=now - timedelta(hours=3),
        last_activity_at=now - timedelta(hours=2),
    )
    db.add(child)
    await db.flush()

    def hermes_row(event_type: str, row: AgentSession, minutes: int, project_id: int | None, **details: Any) -> None:
        db.add(
            bounded_log(
                {
                    "event_type": event_type,
                    "user_id": w.agent.id,
                    "project_id": project_id,
                    "agent_session_id": row.id,
                    "details": {"source": "hermes", "event": "x", "delivery_id": f"d-{minutes}", **details},
                    "created_at": now - timedelta(minutes=minutes),
                }
            )
        )

    hermes_row("agent_tool_called", hermes, 54, None, tool="terminal", status="ok", duration_ms=120)
    hermes_row("agent_approval_requested", hermes, 53, None, pattern_key="rm_rf")
    hermes_row("agent_approval_resolved", hermes, 52, None, pattern_key="rm_rf", choice="deny")
    hermes_row("agent_subagent_started", hermes, 46, None, child_role="researcher", child_agent_session_id=sub.id)
    hermes_row("agent_tool_called", hermes, 44, w.q.id, tool=f"{low}_far_tool", status="ok")
    hermes_row("agent_session_ended", sub, 35, None, interrupted=True, completed=False)

    db.add(
        _usage(
            w.agent,
            w.p,
            w.pub,
            "0.25",
            external_session_id=f"{low}-sub",
            profile="night",
            unattended=True,
        )
    )
    w.old.ended_at = w.old.last_activity_at
    await db.commit()
    clear_role_cache(db)
    return TimelineWorld(w=w, hermes=hermes, sub=sub, child_issue=child_issue, child=child, dev=dev)
