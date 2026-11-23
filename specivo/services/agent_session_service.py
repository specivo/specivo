"""AgentSessionService — manage agent work sessions: one per MCP connection, else per API key.

A session can also carry the client's own session id and its parent's, so AI
usage and client events link to it by id rather than by time overlap.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError, ValidationError
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


#: How an agent session was opened: an MCP tool call, a plain REST call, an event receiver.
CLIENT_TYPE_MCP = "mcp"
CLIENT_TYPE_REST = "rest"
CLIENT_TYPE_WEBHOOK = "webhook"
CLIENT_TYPES = (CLIENT_TYPE_MCP, CLIENT_TYPE_REST, CLIENT_TYPE_WEBHOOK)

#: A client's own session id: opaque, never parsed, only checked against this shape.
EXTERNAL_SESSION_ID_MAX = 200
_EXTERNAL_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")

#: MCP tools that act in no project: their project-less audit rows name nothing of any project,
#: so a project's session timeline shows them. Project-less rows of every other tool are left out,
#: because they can name issues or pages in projects the reader cannot see.
PROJECT_FREE_TOOLS = ("whoami", "list_projects", "list_lookups")

#: How many generations ``lineage`` walks up and down from a session.
LINEAGE_MAX_DEPTH = 10

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


def check_external_session_id(value: str, field: str = "external_session_id") -> str:
    """Return *value* when it is a valid client session id, else raise ``ValidationError``.

    The id is opaque: 1 to 200 characters, letters, digits and ``. _ : -``,
    starting with a letter or digit. Nothing else about it is interpreted.
    """
    if not isinstance(value, str) or _EXTERNAL_SESSION_ID.fullmatch(value) is None:
        raise ValidationError(
            f"{field} must be 1 to {EXTERNAL_SESSION_ID_MAX} characters: letters, digits and . _ : -, "
            "starting with a letter or digit"
        )
    return value


def connection_hash(connection: str) -> str:
    """Return the stored form of an MCP transport session id: its SHA-256 hex digest.

    The id is only ever compared, so the raw value, which a client presents to
    resume its connection, is never written to the database.
    """
    return hashlib.sha256(connection.encode()).hexdigest()


@dataclass(frozen=True)
class AgentSessionFilters:
    """Narrows a project's agent session list; ``None`` leaves a field out.

    *active* ``True`` keeps the open sessions (not ended, last active within
    ``agent_session_timeout``), ``False`` the others.
    """

    user_id: int | None = None
    issue_id: int | None = None
    active: bool | None = None
    external_session_id: str | None = None


class AgentSessionService:
    """Stateless service for agent session management."""

    async def get_or_create_session(
        self,
        session: AsyncSession,
        api_key_id: int,
        user_id: int,
        user_agent: str | None = None,
        client: ClientInfo | None = None,
        connection: str | None = None,
        client_type: str | None = None,
    ) -> AgentSession:
        """Find the active session of this connection (or API key) or start a new one.

        *connection* is the MCP transport session id of the call
        (``Mcp-Session-Id``, or the session id of a legacy SSE stream). Each
        connection has sessions of its own, so a new client connection starts
        a new session with no issue even when it uses the same API key as
        another one; a connection resumed with the same id (after a network
        drop or an api restart) keeps its session. Calls without a connection
        (plain REST) share one session per API key and never join or extend
        a connection's session.

        A session is "active" while its last_activity_at is within the
        timeout window; after that a new session is started.

        *client* (the MCP ``clientInfo``) names the client; without it the
        first product of *user_agent* does. A session that has no client yet
        takes the one this request brings; a known client is never replaced.

        *client_type* is recorded on a new session (``mcp`` for an MCP tool
        call, ``rest`` for a plain REST call); without it a session with a
        connection is ``mcp``, one without is ``rest``.
        """
        now = utcnow()
        client = client or parse_user_agent(user_agent)
        conn_hash = connection_hash(connection) if connection else None

        # The most recent session of this connection, or of the key's plain REST calls.
        stmt = (
            select(AgentSession)
            .where(
                AgentSession.api_key_id == api_key_id,
                AgentSession.connection_hash == conn_hash
                if conn_hash is not None
                else AgentSession.connection_hash.is_(None),
            )
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
            connection_hash=conn_hash,
            client_type=client_type or (CLIENT_TYPE_MCP if conn_hash is not None else CLIENT_TYPE_REST),
            client_name=client.name if client else None,
            client_version=client.version if client else None,
            started_at=now,
            last_activity_at=now,
        )
        session.add(agent_session)
        await session.flush()

        logger.debug(
            "Created agent session %d for api_key_id=%d client=%s connection=%s",
            agent_session.id,
            api_key_id,
            client,
            "yes" if conn_hash else "no",
        )
        return agent_session

    async def record_issue(self, session: AsyncSession, agent_session_id: int, user: User, issue: Issue) -> bool:
        """Make *issue* the issue agent session *agent_session_id* is working on.

        Called when, through MCP, the session's account creates *issue* in an
        ``active`` status or assigned to itself, moves it into an ``active``
        status, assigns it to itself, updates it while it is ``active`` and
        its own (resuming it), or starts a timer on it; the latest such issue
        wins. Nothing is recorded (and ``False`` returned)
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

    # ------------------------------------------------------------------
    # The client's own session id and its parent
    # ------------------------------------------------------------------

    async def set_external(
        self,
        session: AsyncSession,
        agent_session_id: int,
        user: User,
        external_id: str | None,
        parent_external_id: str | None,
    ) -> bool:
        """Record the client's ids on agent session *agent_session_id*, which must be *user*'s.

        *external_id* is the client's id of this session, *parent_external_id*
        its parent's; ``None`` leaves a value as it is. The parent row is
        resolved among *user*'s sessions only (the newest with that id), and
        *user*'s sessions that named this id as their parent before it was
        known are linked to this row. Another account's sessions are never
        read or changed, whatever ids they carry.

        Returns whether anything changed; the same values again are a no-op.
        Raises ``ValidationError`` for an id of the wrong shape or a session
        named as its own parent, ``NotFoundError`` when the session is not
        *user*'s.
        """
        if external_id is not None:
            check_external_session_id(external_id)
        if parent_external_id is not None:
            check_external_session_id(parent_external_id, "parent_session_id")
        agent_session = await session.get(AgentSession, agent_session_id)
        if agent_session is None or agent_session.user_id != user.id:
            raise NotFoundError("Agent session not found")

        new_external = external_id if external_id is not None else agent_session.external_session_id
        new_parent = parent_external_id if parent_external_id is not None else agent_session.parent_external_session_id
        if new_external is not None and new_external == new_parent:
            raise ValidationError("A session cannot be its own parent")

        old_external = agent_session.external_session_id
        changed = False
        if new_external != old_external:
            agent_session.external_session_id = new_external
            changed = True
        if new_parent != agent_session.parent_external_session_id:
            agent_session.parent_external_session_id = new_parent
            changed = True

        parent_row_id: int | None = None
        if new_parent is not None:
            parent_row_id = await self._newest_id(session, user.id, new_parent, exclude=agent_session.id)
        if parent_row_id != agent_session.parent_session_id:
            agent_session.parent_session_id = parent_row_id
            changed = True
        await session.flush()

        if old_external is not None and old_external != new_external:
            # Children linked through the old id go to the newest other row that still has it.
            replacement = await self._newest_id(session, user.id, old_external, exclude=agent_session.id)
            await session.execute(
                update(AgentSession)
                .where(
                    AgentSession.user_id == user.id,
                    AgentSession.parent_session_id == agent_session.id,
                    AgentSession.parent_external_session_id == old_external,
                )
                .values(parent_session_id=replacement)
                .execution_options(synchronize_session="fetch")
            )
        if new_external is not None:
            # Back-fill: the user's sessions naming this id as their parent link to its newest row.
            newest = await self._newest_id(session, user.id, new_external)
            await session.execute(
                update(AgentSession)
                .where(
                    AgentSession.user_id == user.id,
                    AgentSession.parent_external_session_id == new_external,
                    AgentSession.id != newest,
                    AgentSession.parent_session_id.is_distinct_from(newest),
                )
                .values(parent_session_id=newest)
                .execution_options(synchronize_session="fetch")
            )
        await session.flush()
        return changed

    async def _newest_id(
        self, session: AsyncSession, user_id: int, external_id: str, *, exclude: int | None = None
    ) -> int | None:
        """Return the id of *user_id*'s most recently active session with *external_id*, except *exclude*."""
        stmt = (
            select(AgentSession.id)
            .where(AgentSession.user_id == user_id, AgentSession.external_session_id == external_id)
            .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            .limit(1)
        )
        if exclude is not None:
            stmt = stmt.where(AgentSession.id != exclude)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def find_by_external(self, session: AsyncSession, user_id: int, external_id: str) -> list[AgentSession]:
        """Return *user_id*'s agent sessions with the client session id *external_id*, newest first."""
        stmt = (
            select(AgentSession)
            .where(AgentSession.user_id == user_id, AgentSession.external_session_id == external_id)
            .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def lineage(self, session: AsyncSession, agent_session: AgentSession) -> list[AgentSession]:
        """Return the ancestors (nearest first) and then the descendants (nearest first) of *agent_session*.

        Follows ``parent_session_id`` up to ``LINEAGE_MAX_DEPTH`` generations
        each way and stays within the session's user. A row is listed once,
        and never the session itself, so a cycle in the links ends the walk.
        """
        seen: set[int] = {agent_session.id}
        found: list[AgentSession] = []

        current = agent_session
        for _ in range(LINEAGE_MAX_DEPTH):
            parent_id = current.parent_session_id
            if parent_id is None or parent_id in seen:
                break
            parent = await session.get(AgentSession, parent_id)
            if parent is None or parent.user_id != agent_session.user_id:
                break
            seen.add(parent.id)
            found.append(parent)
            current = parent

        frontier = [agent_session.id]
        for _ in range(LINEAGE_MAX_DEPTH):
            if not frontier:
                break
            stmt = (
                select(AgentSession)
                .where(
                    AgentSession.user_id == agent_session.user_id,
                    AgentSession.parent_session_id.in_(frontier),
                )
                .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            )
            children = [row for row in (await session.execute(stmt)).scalars().all() if row.id not in seen]
            for child in children:
                seen.add(child.id)
            found.extend(children)
            frontier = [child.id for child in children]
        return found

    async def issue_for_external(self, session: AsyncSession, user_id: int, external_id: str) -> int | None:
        """Return the issue of *user_id*'s sessions with client session id *external_id*.

        The newest such session naming an issue wins; when none does, the
        nearest session of the newest one's lineage that names one (its
        ancestors first, then its descendants). Only *user_id*'s sessions
        count. ``None`` when nothing names an issue. Like ``issue_for``, this
        is an attribution hint: callers still check they may log there.
        """
        rows = await self.find_by_external(session, user_id, external_id)
        if not rows:
            return None
        for row in rows:
            if row.issue_id is not None:
                return row.issue_id
        for row in await self.lineage(session, rows[0]):
            if row.issue_id is not None:
                return row.issue_id
        return None

    # ------------------------------------------------------------------
    # Sessions opened by an event receiver
    # ------------------------------------------------------------------

    async def open_webhook_session(
        self,
        session: AsyncSession,
        user: User,
        external_id: str,
        *,
        receiver_id: int | None,
        client: ClientInfo | None = None,
        parent_external_id: str | None = None,
    ) -> AgentSession:
        """Find *user*'s open webhook session with client session id *external_id*, or start one.

        Open means not ended and active within the session timeout, as for
        ``get_or_create_session``; the newest such row is extended. Only
        *user*'s webhook rows are read, so an id another account uses (or an
        MCP connection of the same account) never joins this one. A new row
        has no API key, ``client_type`` ``webhook`` and the receiver's id; its
        parent (*parent_external_id*) is resolved and earlier children are
        linked by ``set_external``, within *user*'s sessions only. When some
        row of *user* with that id (or its lineage) names an issue, a session
        without one takes it, so its activity shows on that issue's project;
        on every event the issue is kept only while *user* may still log on
        it (``can_log_on``), else the session names none.

        Raises ``ValidationError`` for an id of the wrong shape.
        """
        check_external_session_id(external_id)
        now = utcnow()
        since = now - timedelta(seconds=_get_session_timeout())
        stmt = (
            select(AgentSession)
            .where(
                AgentSession.user_id == user.id,
                AgentSession.client_type == CLIENT_TYPE_WEBHOOK,
                AgentSession.external_session_id == external_id,
                AgentSession.ended_at.is_(None),
                AgentSession.last_activity_at >= since,
            )
            .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            .limit(1)
        )
        agent_session = (await session.execute(stmt)).scalar_one_or_none()
        if agent_session is None:
            agent_session = AgentSession(
                api_key_id=None,
                user_id=user.id,
                receiver_id=receiver_id,
                client_type=CLIENT_TYPE_WEBHOOK,
                client_name=client.name if client else None,
                client_version=client.version if client else None,
                started_at=now,
                last_activity_at=now,
            )
            session.add(agent_session)
            await session.flush()
            await self.set_external(session, agent_session.id, user, external_id, parent_external_id)
        else:
            agent_session.last_activity_at = now
            if parent_external_id is not None and agent_session.parent_external_session_id is None:
                await self.set_external(session, agent_session.id, user, None, parent_external_id)
        candidate = agent_session.issue_id or await self.issue_for_external(session, user.id, external_id)
        usable = candidate if candidate is not None and await self.can_log_on(session, user, candidate) else None
        if agent_session.issue_id != usable:
            agent_session.issue_id = usable
        await session.flush()
        return agent_session

    async def can_log_on(self, session: AsyncSession, user: User, issue_id: int) -> bool:
        """Whether *user* may still log AI usage on issue *issue_id*: the check MCP and the ingest use.

        An attribution hint from a session is only followed into a project the
        account can still log in (``AiUsageService.require_can_log``), so an
        account that lost access does not keep writing into that project.
        """
        from specivo.core.exceptions import AppError
        from specivo.models.issue import Issue
        from specivo.models.project import Project
        from specivo.services.ai_usage_service import AiUsageService

        issue = await session.get(Issue, issue_id)
        if issue is None or issue.project_id is None:
            return False
        project = await session.get(Project, issue.project_id)
        if project is None:
            return False
        try:
            await AiUsageService().require_can_log(session, user, user, project, issue.id)
        except AppError:
            return False
        return True

    async def end_session(self, session: AsyncSession, agent_session: AgentSession) -> None:
        """Mark *agent_session* ended now (the client said so); an ended session stays ended."""
        now = utcnow()
        if agent_session.ended_at is None:
            agent_session.ended_at = now
        agent_session.last_activity_at = now
        await session.flush()

    async def update_activity(self, session: AsyncSession, session_id: int) -> None:
        """Update last_activity_at timestamp for a session."""
        stmt = select(AgentSession).where(AgentSession.id == session_id)
        result = await session.execute(stmt)
        agent_session = result.scalar_one_or_none()
        if agent_session is not None:
            agent_session.last_activity_at = utcnow()
            await session.flush()

    def is_active(self, agent_session: AgentSession, now: datetime | None = None) -> bool:
        """Whether *agent_session* is still open: not ended, and active within the session timeout."""
        if agent_session.ended_at is not None:
            return False
        last = agent_session.last_activity_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return ((now or utcnow()) - last).total_seconds() < _get_session_timeout()

    async def _project_conditions(
        self, session: AsyncSession, project_id: int, user: User
    ) -> list[ColumnElement[bool]]:
        """The WHERE conditions that make a session one of *project_id*'s that *user* may see.

        A session is the project's when its account is a member of the
        project (directly or through a group). A session tied to an issue is
        the project's only when that issue is in the project and *user* may
        see it, as in the issue list, so nothing about other projects or
        hidden issues is revealed.
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
        return [
            account_is_member,
            or_(AgentSession.issue_id.is_(None), AgentSession.issue_id.in_(visible_issues)),
        ]

    async def list_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        user: User,
        filters: AgentSessionFilters | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[AgentSession], int]:
        """List the agent sessions of a project that *user* may see, most recently active first.

        Returns ``(page, total)``. *filters* narrows the list (see
        :class:`AgentSessionFilters`); which sessions count as the project's
        is decided by ``_project_conditions``.
        """
        conditions = await self._project_conditions(session, project_id, user)
        filters = filters or AgentSessionFilters()
        if filters.user_id is not None:
            conditions.append(AgentSession.user_id == filters.user_id)
        if filters.issue_id is not None:
            conditions.append(AgentSession.issue_id == filters.issue_id)
        if filters.external_session_id is not None:
            conditions.append(AgentSession.external_session_id == filters.external_session_id)
        if filters.active is not None:
            since = utcnow() - timedelta(seconds=_get_session_timeout())
            open_now = and_(AgentSession.ended_at.is_(None), AgentSession.last_activity_at >= since)
            conditions.append(open_now if filters.active else not_(open_now))

        total = await session.scalar(select(func.count()).select_from(AgentSession).where(*conditions)) or 0
        stmt = (
            select(AgentSession)
            .where(*conditions)
            .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all()), int(total)

    async def get_for_project(
        self, session: AsyncSession, project_id: int, agent_session_id: int, user: User
    ) -> AgentSession:
        """Return agent session *agent_session_id* if it is one of *project_id*'s that *user* may see.

        Raises ``NotFoundError`` otherwise: a session of another project and
        one tied to a hidden issue look the same as a missing one.
        """
        conditions = await self._project_conditions(session, project_id, user)
        stmt = select(AgentSession).where(AgentSession.id == agent_session_id, *conditions)
        agent_session = (await session.execute(stmt)).scalar_one_or_none()
        if agent_session is None:
            raise NotFoundError("Agent session not found")
        return agent_session

    async def filters_for(
        self,
        session: AsyncSession,
        project_id: int,
        user: User,
        *,
        account: str | None = None,
        issue_ref: str | None = None,
        active: bool | None = None,
        external_session_id: str | None = None,
    ) -> AgentSessionFilters | None:
        """Resolve the filters of a session list request, or ``None`` when nothing can match.

        *account* is a login (case-insensitive) and *issue_ref* an issue key
        such as ``ACME-12``; an unknown login, an issue outside the project
        and one *user* cannot see all give ``None``, so the list is empty and
        tells nothing about the issue.
        """
        from specivo.models.user import User as UserModel
        from specivo.services.issue_service import IssueService

        user_id: int | None = None
        if account and account.strip():
            user_id = await session.scalar(
                select(UserModel.id).where(func.lower(UserModel.login) == account.strip().lower())
            )
            if user_id is None:
                return None
        issue_id: int | None = None
        if issue_ref and issue_ref.strip():
            try:
                issue = await IssueService().get_by_display_key(session, issue_ref.strip().upper(), user=user)
            except NotFoundError:
                return None
            if issue.project_id != project_id:
                return None
            issue_id = issue.id
        external = external_session_id.strip() if external_session_id and external_session_id.strip() else None
        return AgentSessionFilters(user_id=user_id, issue_id=issue_id, active=active, external_session_id=external)

    async def visible_ids(self, session: AsyncSession, project_id: int, user: User, ids: set[int]) -> set[int]:
        """The ids among *ids* of *project_id*'s sessions that *user* may see (``get_for_project``'s rule)."""
        if not ids:
            return set()
        conditions = await self._project_conditions(session, project_id, user)
        stmt = select(AgentSession.id).where(AgentSession.id.in_(ids), *conditions)
        return set((await session.execute(stmt)).scalars().all())

    async def visible_lineage(
        self, session: AsyncSession, project_id: int, agent_session: AgentSession, user: User
    ) -> list[AgentSession]:
        """``lineage`` of *agent_session*, keeping only the sessions *user* may see in *project_id*."""
        lineage = await self.lineage(session, agent_session)
        visible = await self.visible_ids(session, project_id, user, {s.id for s in lineage})
        return [s for s in lineage if s.id in visible]

    async def hidden_parents(
        self, session: AsyncSession, project_id: int, user: User, sessions: list[AgentSession]
    ) -> set[int]:
        """The ids of the *sessions* whose linked parent *user* may not see in *project_id*.

        Their ``parent_session_id`` and ``parent_external_session_id`` are
        left out of what *user* is shown: the parent's id names a session
        working in another project or on a hidden issue. A parent id the
        client reported but no session row matches names nothing and stays.
        """
        parents = {s.parent_session_id for s in sessions if s.parent_session_id is not None}
        visible = await self.visible_ids(session, project_id, user, parents)
        return {s.id for s in sessions if s.parent_session_id is not None and s.parent_session_id not in visible}
