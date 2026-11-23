"""MCP tool: the calling connection's own agent session.

A client names its session with its own id (for example a Hermes
``session_id``) and its parent's, so AI usage and client events link to the
session by id rather than by time overlap. The tool only ever touches the
agent session of the connection it is called on (``agent_session_id_var``),
and parents are resolved among the same account's sessions only.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.mcp.client_context import agent_session_id_var, record_working_issue
from specivo.mcp.tools._common import _issue_svc, _log_tool
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.user import User
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.security_audit_service import AuditEvent

AGENT_SESSION_OPS = ("set", "get")

_agent_session_svc = AgentSessionService()


async def _current(session: AsyncSession, user: User) -> AgentSession:
    agent_session_id = agent_session_id_var.get()
    agent_session = await session.get(AgentSession, agent_session_id) if agent_session_id is not None else None
    if agent_session is None or agent_session.user_id != user.id:
        raise NotFoundError("No agent session is tracked for this call")
    return agent_session


async def _issue_key(session: AsyncSession, issue_id: int | None) -> str:
    if issue_id is None:
        return "(none)"
    issue = await session.get(Issue, issue_id)
    return issue.display_key if issue is not None else "(none)"


async def _describe(session: AsyncSession, agent_session: AgentSession) -> str:
    client = " ".join(part for part in (agent_session.client_name, agent_session.client_version) if part)
    lines = [
        f"id: {agent_session.id}",
        f"client: {client or '(unknown)'}",
        f"client_type: {agent_session.client_type or '(unknown)'}",
        f"external_session_id: {agent_session.external_session_id or '(none)'}",
        f"parent_external_session_id: {agent_session.parent_external_session_id or '(none)'}",
        f"parent_session_id: {agent_session.parent_session_id or '(not linked)'}",
        f"issue: {await _issue_key(session, agent_session.issue_id)}",
        f"started_at: {agent_session.started_at.isoformat()}",
        f"last_activity_at: {agent_session.last_activity_at.isoformat()}",
    ]
    return "\n".join(lines)


async def _agent_session(
    session: AsyncSession,
    user: User,
    op: str,
    external_session_id: str | None = None,
    parent_session_id: str | None = None,
    issue_ref: str | None = None,
) -> str:
    """Read or set the ids of the calling connection's agent session.

    ``set`` records the client's session id and its parent's (``None`` keeps
    a value) and, with *issue_ref*, the issue the session works on (an issue
    *user* cannot see is "not found"). It is audited when something changed.
    ``get`` returns the session; it is not audited, the data is the caller's own.
    """
    if op not in AGENT_SESSION_OPS:
        raise ValidationError(f"Unknown op '{op}': use one of {', '.join(AGENT_SESSION_OPS)}")
    agent_session = await _current(session, user)
    if op == "get":
        return await _describe(session, agent_session)

    if external_session_id is None and parent_session_id is None and issue_ref is None:
        raise ValidationError("Nothing to set: pass external_session_id, parent_session_id or issue_ref")
    issue: Issue | None = None
    if issue_ref is not None:
        # Resolved first, so a hidden issue refuses the whole call before anything is written.
        issue = await _issue_svc.get_by_display_key(session, issue_ref, user)

    changed = await _agent_session_svc.set_external(
        session, agent_session.id, user, external_session_id, parent_session_id
    )
    if issue is not None and agent_session.issue_id != issue.id:
        changed = await record_working_issue(session, user, issue) or changed

    await session.refresh(agent_session)
    if not changed:
        return "Agent session unchanged.\n" + await _describe(session, agent_session)
    await _log_tool(
        session,
        user,
        AuditEvent.AGENT_SESSION_LINKED,
        "agent_session",
        {
            "external_session_id": agent_session.external_session_id,
            "parent_external_session_id": agent_session.parent_external_session_id,
            "issue": issue.display_key if issue is not None else None,
        },
        project_id=issue.project_id if issue is not None else None,
    )
    return "Agent session updated.\n" + await _describe(session, agent_session)
