"""AgentSessionService — manage agent work sessions tied to API key usage."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession

if TYPE_CHECKING:
    from specivo.models.issue import Issue
    from specivo.models.user import User

logger = logging.getLogger(__name__)

# Session inactivity threshold: if last activity was more than this many seconds
# ago, start a new session instead of reusing the old one.
# Resolved lazily to avoid import-time get_settings() (breaks CI/test imports).
_SESSION_TIMEOUT_SECONDS: int | None = None


def _get_session_timeout() -> int:
    global _SESSION_TIMEOUT_SECONDS
    if _SESSION_TIMEOUT_SECONDS is None:
        _SESSION_TIMEOUT_SECONDS = get_settings().agent_session_timeout
    return _SESSION_TIMEOUT_SECONDS


_CLIENT_NAME_MAX = 100
_CLIENT_VERSION_MAX = 50

# The first product token of a User-Agent: ``name`` or ``name/version``.
# Examples:
#   "claude-code/2.1.0 (external, cli)" -> ("claude-code", "2.1.0")
#   "python-httpx/0.28.1"               -> ("python-httpx", "0.28.1")
#   "curl"                              -> ("curl", None)
_UA_PRODUCT = re.compile(r"^\s*([^\s/()]+)(?:/([^\s()]+))?")


@dataclass(frozen=True)
class ClientInfo:
    """The client that opened an agent session: a name and an optional version."""

    name: str
    version: str | None = None

    @classmethod
    def of(cls, name: str | None, version: str | None = None) -> ClientInfo | None:
        """Return a trimmed ``ClientInfo`` that fits the columns, or ``None`` for a blank name."""
        name = (name or "").strip()[:_CLIENT_NAME_MAX]
        if not name:
            return None
        version = (version or "").strip()[:_CLIENT_VERSION_MAX] or None
        return cls(name=name, version=version)


def parse_user_agent(user_agent: str | None) -> ClientInfo | None:
    """Return the first product of a User-Agent header as a ``ClientInfo``.

    Returns ``None`` when the header is missing or does not start with a product.
    """
    if not user_agent:
        return None
    match = _UA_PRODUCT.match(user_agent)
    if match is None:
        return None
    return ClientInfo.of(match.group(1), match.group(2))


class AgentSessionService:
    """Stateless service for agent session management."""

    async def get_or_create_session(
        self,
        session: AsyncSession,
        api_key_id: int,
        user_id: int,
        user_agent: str | None = None,
        client: ClientInfo | None = None,
    ) -> AgentSession:
        """Find an active session for this API key or create a new one.

        A session is considered "active" if its last_activity_at is within
        the timeout window. Otherwise a new session is started.

        *client* (the MCP ``clientInfo``) names the client; without it the
        first product of *user_agent* does. A session that has no client yet
        takes the one this request brings; a known client is never replaced.
        """
        now = utcnow()
        client = client or parse_user_agent(user_agent)

        # Look for the most recent session for this API key
        stmt = (
            select(AgentSession)
            .where(AgentSession.api_key_id == api_key_id)
            .order_by(AgentSession.last_activity_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            elapsed = (now - existing.last_activity_at.replace(tzinfo=UTC)).total_seconds()
            if elapsed < _get_session_timeout():
                existing.last_activity_at = now
                if existing.client_name is None and client is not None:
                    existing.client_name = client.name
                    existing.client_version = client.version
                await session.flush()
                return existing

        agent_session = AgentSession(
            api_key_id=api_key_id,
            user_id=user_id,
            client_name=client.name if client else None,
            client_version=client.version if client else None,
            started_at=now,
            last_activity_at=now,
        )
        session.add(agent_session)
        await session.flush()

        logger.debug(
            "Created agent session %d for api_key_id=%d client=%s",
            agent_session.id,
            api_key_id,
            client,
        )
        return agent_session

    async def record_issue(self, session: AsyncSession, agent_session_id: int, user: User, issue: Issue) -> bool:
        """Make *issue* the issue agent session *agent_session_id* is working on.

        Called when, through MCP, the session's account moves *issue* into an
        ``active`` status, assigns it to itself or starts a timer on it; the
        latest such issue wins. Nothing is recorded (and ``False`` returned)
        when the session is not *user*'s or *user* may not see *issue*, so a
        session never names an issue its account cannot see.
        """
        from specivo.services.issue_service import IssueService

        agent_session = await session.get(AgentSession, agent_session_id)
        if agent_session is None or agent_session.user_id != user.id:
            return False
        if not await IssueService().is_visible(session, issue, user):
            return False
        if agent_session.issue_id != issue.id:
            agent_session.issue_id = issue.id
            await session.flush()
        return True

    async def issue_for(
        self,
        session: AsyncSession,
        user_id: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> int | None:
        """Return the issue of *user_id*'s agent session overlapping ``[started_at, ended_at]``.

        Only sessions that name an issue count. When several overlap the
        window, the one sharing the longest stretch of it wins, then the most
        recently active. A session's window is ``[started_at, last_activity_at]``;
        touching at a single instant counts as overlapping. ``None`` when no
        session overlaps. The caller still checks that the user may log to
        the issue: this is an attribution hint, not a permission.
        """
        if ended_at < started_at:
            started_at, ended_at = ended_at, started_at
        overlap = func.least(AgentSession.last_activity_at, ended_at) - func.greatest(
            AgentSession.started_at, started_at
        )
        stmt = (
            select(AgentSession.issue_id)
            .where(
                AgentSession.user_id == user_id,
                AgentSession.issue_id.is_not(None),
                AgentSession.started_at <= ended_at,
                AgentSession.last_activity_at >= started_at,
            )
            .order_by(overlap.desc(), AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def update_activity(self, session: AsyncSession, session_id: int) -> None:
        """Update last_activity_at timestamp for a session."""
        stmt = select(AgentSession).where(AgentSession.id == session_id)
        result = await session.execute(stmt)
        agent_session = result.scalar_one_or_none()
        if agent_session is not None:
            agent_session.last_activity_at = utcnow()
            await session.flush()

    async def list_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        user: User,
        limit: int = 25,
    ) -> list[AgentSession]:
        """List the recent agent sessions of a project that *user* may see.

        A session is the project's when its account is a member of the
        project (directly or through a group). A session tied to an issue is
        listed only when that issue is in the project and *user* may see it,
        as in the issue list, so the list reveals nothing about other
        projects or hidden issues.
        """
        from specivo.models.issue import Issue
        from specivo.models.member import Member
        from specivo.models.user_group import UserGroupMember
        from specivo.services.issue_service import IssueService

        account_groups = select(UserGroupMember.group_id).where(UserGroupMember.user_id == AgentSession.user_id)
        account_is_member = (
            select(Member.id)
            .where(
                Member.project_id == project_id,
                or_(Member.user_id == AgentSession.user_id, Member.group_id.in_(account_groups)),
            )
            .exists()
        )
        visible_issues = select(Issue.id).where(Issue.project_id == project_id)
        visibility = await IssueService().visible_issues_clause(session, user)
        if visibility is not None:
            visible_issues = visible_issues.where(visibility)

        stmt = (
            select(AgentSession)
            .where(
                account_is_member,
                or_(AgentSession.issue_id.is_(None), AgentSession.issue_id.in_(visible_issues)),
            )
            .order_by(AgentSession.last_activity_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
