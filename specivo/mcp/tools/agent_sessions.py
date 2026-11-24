"""MCP tool: the calling connection's own agent session.

A client names its session with its own id (for example a Hermes
``session_id``) and its parent's, so AI usage and client events link to the
session by id rather than by time overlap. The tool only ever touches the
agent sessions of the connection it is called on (``agent_session_id_var``),
and parents are resolved among the same account's sessions only.

A client that runs several sessions over one connection (subagents, gateway
chats) passes each one's id on ``set``: every client session gets a row of
its own on the connection, and the last one selected is the one later tool
calls on the connection are attributed to.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.mcp.client_context import agent_session_id_var, record_working_issue
from specivo.mcp.tools._common import _issue_svc, _log_tool
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.user import User
from specivo.services.agent_session_service import AgentSessionService, check_external_session_id
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
    """Read or set the calling connection's agent session.

    ``set`` with *external_session_id* first selects the connection's row of
    that client session, creating it when the connection has none
    (``AgentSessionService.select_for_client``), and makes it current for
    the rest of the call and for later calls on the connection. It then
    records the client's session id and its parent's (``None`` keeps a
    value) and, with *issue_ref*, the issue the session works on (an issue
    *user* cannot see is "not found"); an empty *issue_ref* clears the
    issue. It is audited when something changed.

    ``get`` returns the current session, or with *external_session_id* the
    connection's row of that client session; it is not audited, the data is
    the caller's own.
    """
    if op not in AGENT_SESSION_OPS:
        raise ValidationError(f"Unknown op '{op}': use one of {', '.join(AGENT_SESSION_OPS)}")
    agent_session = await _current(session, user)
    if op == "get":
        if external_session_id is not None:
            check_external_session_id(external_session_id)
            found = await _agent_session_svc.find_on_connection(session, agent_session, external_session_id)
            if found is None:
                raise NotFoundError(f"No agent session with external_session_id '{external_session_id}' here")
            agent_session = found
        return await _describe(session, agent_session)
    return await _set(session, user, agent_session, external_session_id, parent_session_id, issue_ref)


async def _set(
    session: AsyncSession,
    user: User,
    agent_session: AgentSession,
    external_session_id: str | None,
    parent_session_id: str | None,
    issue_ref: str | None,
) -> str:
    if external_session_id is None and parent_session_id is None and issue_ref is None:
        raise ValidationError("Nothing to set: pass external_session_id, parent_session_id or issue_ref")
    if external_session_id is not None:
        check_external_session_id(external_session_id)
    if parent_session_id is not None:
        check_external_session_id(parent_session_id, "parent_session_id")
    clear_issue = issue_ref is not None and not issue_ref.strip()
    issue: Issue | None = None
    if issue_ref is not None and not clear_issue:
        # Resolved first, so a hidden issue refuses the whole call before anything is written.
        issue = await _issue_svc.get_by_display_key(session, issue_ref, user)

    target, changed = agent_session, False
    if external_session_id is not None:
        target, changed = await _agent_session_svc.select_for_client(
            session, agent_session.id, user, external_session_id
        )
    switched = target.id != agent_session.id
    # The rest of this call (the working issue, the audit row) belongs to the selected session.
    token = agent_session_id_var.set(target.id) if switched else None
    try:
        changed = (
            await _agent_session_svc.set_external(session, target.id, user, external_session_id, parent_session_id)
            or changed
        )
        if issue is not None and target.issue_id != issue.id:
            changed = await record_working_issue(session, user, issue) or changed
        cleared: Issue | None = None
        if clear_issue:
            old_issue_id = await _agent_session_svc.clear_issue(session, target.id, user)
            if old_issue_id is not None:
                cleared = await session.get(Issue, old_issue_id)
                changed = True

        await session.refresh(target)
        if not changed:
            head = "Agent session selected." if switched else "Agent session unchanged."
            return head + "\n" + await _describe(session, target)
        await _audit(session, user, target, issue, cleared, cleared=clear_issue)
        return "Agent session updated.\n" + await _describe(session, target)
    finally:
        if token is not None:
            agent_session_id_var.reset(token)


async def _audit(
    session: AsyncSession,
    user: User,
    target: AgentSession,
    issue: Issue | None,
    cleared_issue: Issue | None,
    *,
    cleared: bool,
) -> None:
    """Write the ``AGENT_SESSION_LINKED`` row of a change, in the project of the issue set or cleared."""
    details: dict[str, str | None] = {
        "external_session_id": target.external_session_id,
        "parent_external_session_id": target.parent_external_session_id,
        "issue": issue.display_key if issue is not None else None,
    }
    if cleared:
        details["issue_cleared"] = cleared_issue.display_key if cleared_issue is not None else None
    changed_in = issue if issue is not None else cleared_issue
    await _log_tool(
        session,
        user,
        AuditEvent.AGENT_SESSION_LINKED,
        "agent_session",
        details,
        project_id=changed_in.project_id if changed_in is not None else None,
    )
