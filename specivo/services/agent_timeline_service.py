"""AgentTimelineService — what agents did for an issue: one block per agent session, with steps and cost.

A read-only composition of three existing sources, no storage of its own:

- ``agent_sessions``: which sessions worked on the issue, grouped by lineage;
- ``security_audit_logs``: their steps (MCP tool calls, Hermes tool calls,
  approvals, subagents, the end of the session);
- ``ai_usage_entries``: what the work cost, from the usage the reporter sent.

Which sessions belong to an issue: the sessions on the issue (or on a
sub-issue the reader can see, when sub-issues are included), and the sessions
of the same account whose client session id is on a usage entry of those
issues. Each is grouped under its lineage root (``parent_session_id``,
walked up within the account while the parent is one the reader may see in
the project), and the root's descendants join the block as its children,
flattened to one level. Rows of one account sharing a client session id (the
MCP connection and the webhook session of one Hermes run) are one node.

Only sessions the reader may see in the project count
(``AgentSessionService.visible_conditions``): the account is a member, and a
session on an issue is kept only when that issue is visible. Steps are this
project's audit rows of the block, the project-less rows of
``PROJECT_FREE_TOOLS`` and the project-less rows of event receivers, which
hold metadata only. Cost follows the AI usage visibility rules of the project.

Bounded: at most :data:`MAX_SESSIONS` sessions are read for one issue, and
the steps, counters and cost are read for one page of blocks at a time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from specivo.core.constants import MAX_HIERARCHY_DEPTH
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.agent_session_service import LINEAGE_MAX_DEPTH, PROJECT_FREE_TOOLS, AgentSessionService
from specivo.services.agent_session_usage import session_match
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService, project_rows_clause

if TYPE_CHECKING:
    from specivo.core.exchange import ConversionSummary
    from specivo.models.project import Project
    from specivo.models.user import User

#: Sessions read for one issue (most recently active first); older ones are not shown.
MAX_SESSIONS = 500
#: Rows read while walking lineages up and down, on top of the sessions of the issue.
MAX_LINEAGE_ROWS = 1000
#: Blocks per page of the issue's agent tab and REST list.
BLOCKS_PER_PAGE = 20
#: Steps shown in a block; the session's own page has them all.
STEPS_SHOWN = 30
#: Usage entries priced for one page of blocks.
MAX_COST_ENTRIES = 5000
#: Longest span of usage days one pricing covers (the spend report's own limit).
_MAX_COST_DAYS = 1100
#: Event sources whose project-less audit rows are metadata only, so a project's timeline may show them.
PROJECTLESS_SOURCES = ("hermes",)

AgentState = Literal["running", "completed", "interrupted", "idle"]
STATE_RUNNING: AgentState = "running"
STATE_COMPLETED: AgentState = "completed"
STATE_INTERRUPTED: AgentState = "interrupted"
STATE_IDLE: AgentState = "idle"

#: A node's key: ``("x", user id, client session id)`` or ``("s", session id)`` without a client id.
NodeKey = tuple[Any, ...]


def node_key(agent_session: AgentSession) -> NodeKey:
    """Rows of one account with the same client session id are one node; others stand alone."""
    if agent_session.external_session_id:
        return ("x", agent_session.user_id, agent_session.external_session_id)
    return ("s", agent_session.id)


@dataclass
class SessionNode:
    """One agent session as the timeline shows it: one or more rows sharing a client session id."""

    key: NodeKey
    rows: list[AgentSession] = field(default_factory=list)

    @property
    def primary(self) -> AgentSession:
        """The row that names the node: the first one opened."""
        return min(self.rows, key=lambda s: (s.started_at, s.id))

    @property
    def ids(self) -> list[int]:
        return [s.id for s in self.rows]

    @property
    def started_at(self) -> datetime:
        return min(s.started_at for s in self.rows)

    @property
    def last_activity_at(self) -> datetime:
        return max(s.last_activity_at for s in self.rows)

    @property
    def ended_at(self) -> datetime | None:
        """When the last row ended, or ``None`` while any row has not."""
        if any(s.ended_at is None for s in self.rows):
            return None
        return max(s.ended_at for s in self.rows if s.ended_at is not None)

    @property
    def tool_call_count(self) -> int:
        return sum(s.tool_call_count for s in self.rows)

    def add(self, agent_session: AgentSession) -> None:
        if all(row.id != agent_session.id for row in self.rows):
            self.rows.append(agent_session)


@dataclass
class AgentBlock:
    """A lineage root and its descendants (flattened to one level of children)."""

    root: SessionNode
    children: dict[NodeKey, SessionNode] = field(default_factory=dict)

    @property
    def nodes(self) -> list[SessionNode]:
        return [self.root, *self.children.values()]

    @property
    def session_ids(self) -> list[int]:
        return [sid for node in self.nodes for sid in node.ids]

    @property
    def last_activity_at(self) -> datetime:
        return max(node.last_activity_at for node in self.nodes)

    def child_nodes(self) -> list[SessionNode]:
        """The children, the first started first."""
        return sorted(self.children.values(), key=lambda n: (n.started_at, n.primary.id))


@dataclass
class BlockCounters:
    """What a block's audit rows count: tool calls, approvals and how they were resolved, the end."""

    tool_calls: int = 0
    approvals_requested: int = 0
    #: Resolved approvals per choice (``approve``, ``deny``, ...; ``""`` when none was given).
    approvals_resolved: dict[str, int] = field(default_factory=dict)
    #: Session ids whose end event said the run was interrupted.
    interrupted_ids: set[int] = field(default_factory=set)


@dataclass
class BlockCost:
    """The usage of a block the reader may read, priced in the project's reporting currency."""

    entries: int
    tokens: int
    currency: str
    marginal: ConversionSummary | None
    api_equivalent: ConversionSummary | None
    profiles: list[str] = field(default_factory=list)
    unattended: bool = False


def _issue_tree_ids(issue_id: int) -> Any:
    """A select of *issue_id* and every issue below it (one recursive CTE, bounded like the AI usage one)."""
    child = aliased(Issue)
    tree = (
        select(Issue.id.label("id"), literal(0).label("level"))
        .where(Issue.id == issue_id)
        .cte("agent_timeline_issue_tree", recursive=True)
    )
    tree = tree.union_all(
        select(child.id, tree.c.level + 1).where(child.parent_id == tree.c.id, tree.c.level < MAX_HIERARCHY_DEPTH)
    )
    return select(tree.c.id)


@dataclass
class AgentPage:
    """One page of an issue's blocks with what the page shows of them."""

    blocks: list[AgentBlock]
    total: int
    offset: int
    limit: int
    counters: dict[NodeKey, BlockCounters]
    #: ``None`` when the reader may not see AI usage cost in the project.
    costs: dict[NodeKey, BlockCost] | None
    #: The accounts of the page's sessions, by id.
    users: dict[int, User]


class _BlockBuilder:
    """Groups the sessions of an issue into blocks by lineage root (``AgentTimelineService.sessions_for_issue``).

    Works on nodes (``node_key``): the rows of one account sharing a client
    session id are one node, and a node's parent is the parent of any of its
    rows, so the MCP half of a run that names its parent and the webhook half
    that does not still land in the same block.
    """

    def __init__(self, session: AsyncSession, visible: list[Any], seeds: list[AgentSession]) -> None:
        self.session = session
        self.visible = visible
        self.seeds = seeds
        self.loaded: dict[int, AgentSession] = {s.id: s for s in seeds}
        self.by_key: dict[NodeKey, dict[int, AgentSession]] = defaultdict(dict)
        for row in seeds:
            self.by_key[node_key(row)][row.id] = row
        self.siblings_fetched: set[NodeKey] = set()
        self.blocks: dict[NodeKey, AgentBlock] = {}
        self.block_of_key: dict[NodeKey, NodeKey] = {}
        self.budget = MAX_LINEAGE_ROWS

    def _remember(self, rows: list[AgentSession]) -> list[AgentSession]:
        new = [row for row in rows if row.id not in self.loaded]
        for row in new:
            self.loaded[row.id] = row
            self.by_key[node_key(row)][row.id] = row
        return new

    async def _fetch(self, *conditions: Any) -> list[AgentSession]:
        if self.budget <= 0:
            return []
        stmt = select(AgentSession).where(*conditions, *self.visible).order_by(AgentSession.id).limit(self.budget)
        rows = list((await self.session.execute(stmt)).scalars().all())
        self.budget -= len(rows)
        return self._remember(rows)

    async def _fetch_siblings(self) -> list[AgentSession]:
        """Rows sharing a loaded node's client session id that are not loaded yet."""
        keys = [k for k in self.by_key if k[0] == "x" and k not in self.siblings_fetched]
        if not keys:
            return []
        self.siblings_fetched.update(keys)
        return await self._fetch(
            tuple_(AgentSession.user_id, AgentSession.external_session_id).in_([(k[1], k[2]) for k in keys]),
            AgentSession.id.not_in(list(self.loaded)),
        )

    async def load_ancestors(self) -> None:
        """Load the seeds' nodes and their ancestors the reader may see, a generation at a time."""
        for _ in range(LINEAGE_MAX_DEPTH + 1):
            new = await self._fetch_siblings()
            parents = {
                s.parent_session_id for s in self.loaded.values() if s.parent_session_id is not None
            } - self.loaded.keys()
            if parents:
                new += await self._fetch(AgentSession.id.in_(parents))
            if not new:
                break

    def _parent_key(self, key: NodeKey) -> NodeKey | None:
        """The node of the loaded parent of any of *key*'s rows within the account (the newest row's first)."""
        rows = sorted(self.by_key[key].values(), key=lambda s: (s.last_activity_at, s.id), reverse=True)
        for row in rows:
            parent = self.loaded.get(row.parent_session_id) if row.parent_session_id is not None else None
            if parent is not None and parent.user_id == row.user_id and node_key(parent) != key:
                return node_key(parent)
        return None

    def _path(self, key: NodeKey) -> list[NodeKey]:
        """*key* and its ancestors' nodes, the root last; a cycle ends the walk."""
        path = [key]
        for _ in range(LINEAGE_MAX_DEPTH):
            parent = self._parent_key(path[-1])
            if parent is None or parent in path:
                break
            path.append(parent)
        return path

    def _place(self, key: NodeKey, block_key: NodeKey) -> None:
        block = self.blocks.setdefault(block_key, AgentBlock(root=SessionNode(key=block_key)))
        node = block.root if key == block_key else block.children.setdefault(key, SessionNode(key=key))
        for row in self.by_key[key].values():
            node.add(row)
        self.block_of_key[key] = block_key

    def place_seeds(self) -> None:
        """Put each seed's node under its root, with the nodes between them as the block's children."""
        for seed in self.seeds:
            path = self._path(node_key(seed))
            for key in path:
                if key not in self.block_of_key:
                    self._place(key, path[-1])

    async def load_descendants(self) -> None:
        """The descendants of every block, flattened under it as children."""
        expanded: set[int] = set()
        for _ in range(LINEAGE_MAX_DEPTH):
            frontier = {sid for key in self.block_of_key for sid in self.by_key[key]} - expanded
            if not frontier or self.budget <= 0:
                break
            expanded |= frontier
            children = await self._fetch(AgentSession.parent_session_id.in_(frontier))
            if not children:
                break
            await self._fetch_siblings()
            for child in children:
                key = node_key(child)
                if key in self.block_of_key:
                    continue
                parent_key = self._parent_key(key)
                if parent_key is None or parent_key not in self.block_of_key:
                    continue
                self._place(key, self.block_of_key[parent_key])
        # Rows that joined a placed node late (siblings fetched after placing) belong to it too.
        for key, block_key in self.block_of_key.items():
            self._place(key, block_key)


class AgentTimelineService:
    """Stateless composition of agent sessions, their audit rows and their AI usage for one issue."""

    def __init__(self) -> None:
        self._sessions = AgentSessionService()
        self._audit = SecurityAuditService()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def _issue_ids(self, session: AsyncSession, user: User, issue: Issue, include_subtree: bool) -> list[int]:
        """The issue and, with *include_subtree*, the sub-issues below it that *user* may see."""
        if not include_subtree:
            return [issue.id]
        from specivo.services.issue_service import IssueService

        stmt = select(Issue.id).where(Issue.id.in_(_issue_tree_ids(issue.id)))
        visibility = await IssueService().visible_issues_clause(session, user)
        if visibility is not None:
            stmt = stmt.where(or_(Issue.id == issue.id, visibility))
        return list((await session.execute(stmt)).scalars().all())

    async def sessions_for_issue(
        self,
        session: AsyncSession,
        user: User,
        project: Project,
        issue: Issue,
        include_subtree: bool = False,
    ) -> list[AgentBlock]:
        """Every block of *issue* that *user* may see, most recently active first (see the module docstring)."""
        visible = await self._sessions.visible_conditions(session, project.id, user)
        issue_ids = await self._issue_ids(session, user, issue, include_subtree)
        usage_keys = (
            select(AiUsageEntry.user_id, AiUsageEntry.external_session_id)
            .where(AiUsageEntry.issue_id.in_(issue_ids), AiUsageEntry.external_session_id.is_not(None))
            .distinct()
        )
        seeds_stmt = (
            select(AgentSession)
            .where(
                *visible,
                or_(
                    AgentSession.issue_id.in_(issue_ids),
                    tuple_(AgentSession.user_id, AgentSession.external_session_id).in_(usage_keys),
                ),
            )
            .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            .limit(MAX_SESSIONS)
        )
        seeds = list((await session.execute(seeds_stmt)).scalars().all())
        if not seeds:
            return []
        builder = _BlockBuilder(session, visible, seeds)
        await builder.load_ancestors()
        builder.place_seeds()
        await builder.load_descendants()
        return sorted(builder.blocks.values(), key=lambda b: (b.last_activity_at, b.root.primary.id), reverse=True)

    async def count_for_issue(
        self, session: AsyncSession, user: User, project: Project, issue: Issue, include_subtree: bool = False
    ) -> int:
        """How many blocks the issue's agent tab lists."""
        return len(await self.sessions_for_issue(session, user, project, issue, include_subtree))

    async def page(
        self,
        session: AsyncSession,
        user: User,
        project: Project,
        issue: Issue,
        *,
        include_subtree: bool = False,
        offset: int = 0,
        limit: int = BLOCKS_PER_PAGE,
        with_cost: bool = True,
    ) -> AgentPage:
        """A page of the issue's blocks with their counters, accounts and (with *with_cost*) cost."""
        from specivo.models.user import User as UserModel

        blocks = await self.sessions_for_issue(session, user, project, issue, include_subtree)
        total = len(blocks)
        if offset >= total > 0:
            offset = (total - 1) // limit * limit
        shown = blocks[offset : offset + limit]
        user_ids = {row.user_id for block in shown for node in block.nodes for row in node.rows}
        users: dict[int, User] = {}
        if user_ids:
            result = await session.execute(select(UserModel).where(UserModel.id.in_(user_ids)))
            users = {u.id: u for u in result.scalars().all()}
        return AgentPage(
            blocks=shown,
            total=total,
            offset=offset,
            limit=limit,
            counters=await self.counters_for(session, project, shown),
            costs=await self.cost_for(session, user, project, shown) if with_cost else None,
            users=users,
        )

    # ------------------------------------------------------------------
    # Steps and counters
    # ------------------------------------------------------------------

    async def steps_for(
        self, session: AsyncSession, project: Project, block: AgentBlock, limit: int = STEPS_SHOWN
    ) -> tuple[list[SecurityAuditLog], int]:
        """The newest *limit* audit rows of the block's sessions, merged by time, and how many there are."""
        return await self._audit.list_events(
            session,
            project_id=project.id,
            agent_session_ids=block.session_ids,
            projectless_tools=PROJECT_FREE_TOOLS,
            projectless_sources=PROJECTLESS_SOURCES,
            limit=limit,
        )

    async def counters_for(
        self, session: AsyncSession, project: Project, blocks: Sequence[AgentBlock]
    ) -> dict[NodeKey, BlockCounters]:
        """The counters of each block (by root key), from two grouped queries over the page's audit rows."""
        block_of = {sid: block.root.key for block in blocks for sid in block.session_ids}
        out: dict[NodeKey, BlockCounters] = {block.root.key: BlockCounters() for block in blocks}
        if not block_of:
            return out
        rows = project_rows_clause(project.id, PROJECT_FREE_TOOLS, PROJECTLESS_SOURCES)
        in_page = SecurityAuditLog.agent_session_id.in_(list(block_of))
        is_tool = SecurityAuditLog.details["tool"].astext.is_not(None)
        requested = SecurityAuditLog.event_type == AuditEvent.AGENT_APPROVAL_REQUESTED.value
        counts = await session.execute(
            select(
                SecurityAuditLog.agent_session_id,
                func.count().filter(is_tool),
                func.count().filter(requested),
            )
            .where(in_page, rows)
            .group_by(SecurityAuditLog.agent_session_id)
        )
        for session_id, tools, approvals in counts.all():
            if session_id is None:  # pragma: no cover - the filter names sessions
                continue
            counters = out[block_of[session_id]]
            counters.tool_calls += int(tools or 0)
            counters.approvals_requested += int(approvals or 0)

        choice = func.coalesce(SecurityAuditLog.details["choice"].astext, "")
        interrupted = SecurityAuditLog.details["interrupted"].astext == "true"
        resolved = await session.execute(
            select(
                SecurityAuditLog.agent_session_id,
                SecurityAuditLog.event_type,
                choice,
                func.bool_or(interrupted),
                func.count(),
            )
            .where(
                in_page,
                rows,
                SecurityAuditLog.event_type.in_(
                    [AuditEvent.AGENT_APPROVAL_RESOLVED.value, AuditEvent.AGENT_SESSION_ENDED.value]
                ),
            )
            .group_by(SecurityAuditLog.agent_session_id, SecurityAuditLog.event_type, choice)
        )
        for session_id, event_type, choice_value, was_interrupted, count in resolved.all():
            counters = out[block_of[session_id]]
            if event_type == AuditEvent.AGENT_SESSION_ENDED.value:
                if was_interrupted:
                    counters.interrupted_ids.add(session_id)
                continue
            counters.approvals_resolved[choice_value] = counters.approvals_resolved.get(choice_value, 0) + int(count)
        return out

    def state_of(self, node: SessionNode, counters: BlockCounters, now: datetime | None = None) -> AgentState:
        """``running`` (a row active within the session timeout), ``interrupted``, ``completed`` or ``idle``."""
        now = now or utcnow()
        if any(self._sessions.is_active(row, now) for row in node.rows):
            return STATE_RUNNING
        if node.ended_at is not None:
            if any(sid in counters.interrupted_ids for sid in node.ids):
                return STATE_INTERRUPTED
            return STATE_COMPLETED
        return STATE_IDLE

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    async def cost_for(
        self, session: AsyncSession, user: User, project: Project, blocks: Sequence[AgentBlock]
    ) -> dict[NodeKey, BlockCost]:
        """The priced usage of each block (by root key) that *user* may read in *project*.

        An entry belongs to a session as on the session's page
        (``agent_session_usage.session_match``: by client session id, else by
        time). Excluded entries are left out. Marginal spend and the
        API-equivalent value come from ``AiSpendReportService.entry_spend``
        in the project's reporting currency, without seat cost. Blocks
        without usage are missing from the map.
        """
        from specivo.services.ai_spend_report_service import AiSpendReportService, fold_rows
        from specivo.services.ai_usage_service import AiUsageService
        from specivo.services.currency_service import reporting_currency_for

        block_of = {sid: block.root.key for block in blocks for sid in block.session_ids}
        if not block_of:
            return {}
        conditions = [
            AgentSession.id.in_(list(block_of)),
            AiUsageEntry.project_id == project.id,
            AiUsageEntry.excluded_at.is_(None),
        ]
        visible = await AiUsageService().visible_entries_clause(session, user, project)
        if visible is not None:
            conditions.append(visible)
        stmt = (
            select(
                AgentSession.id,
                AiUsageEntry.id,
                AiUsageEntry.usage_date,
                AiUsageEntry.profile,
                AiUsageEntry.unattended,
            )
            .join(AiUsageEntry, session_match())
            .where(*conditions)
            .order_by(AiUsageEntry.usage_date.desc(), AiUsageEntry.id.desc())
            .limit(MAX_COST_ENTRIES)
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return {}
        last_day: date = max(r[2] for r in rows)
        first_allowed = last_day - timedelta(days=_MAX_COST_DAYS - 1)
        entries_of: dict[NodeKey, set[int]] = defaultdict(set)
        profiles: dict[NodeKey, set[str]] = defaultdict(set)
        unattended: dict[NodeKey, bool] = defaultdict(bool)
        days: list[date] = []
        for session_id, entry_id, usage_date, profile, is_unattended in rows:
            if usage_date < first_allowed:
                continue
            key = block_of[session_id]
            entries_of[key].add(entry_id)
            days.append(usage_date)
            if profile:
                profiles[key].add(profile)
            if is_unattended:
                unattended[key] = True
        all_ids = sorted({eid for ids in entries_of.values() for eid in ids})
        currency = await reporting_currency_for(session, project)
        priced = await AiSpendReportService().entry_spend(
            session,
            user,
            project=project,
            date_from=min(days),
            date_to=max(days),
            entry_ids=all_ids,
            currency=currency,
            include_seat_cost=False,
        )
        out: dict[NodeKey, BlockCost] = {}
        for key, ids in entries_of.items():
            parts = [priced[eid] for eid in ids if eid in priced]
            if not parts:
                continue
            total = fold_rows(parts, currency, False)
            out[key] = BlockCost(
                entries=total.entries,
                tokens=total.total_tokens,
                currency=currency,
                marginal=total.marginal,
                api_equivalent=total.api_equivalent,
                profiles=sorted(profiles[key]),
                unattended=unattended[key],
            )
        return out


__all__ = [
    "BLOCKS_PER_PAGE",
    "AgentPage",
    "STEPS_SHOWN",
    "AgentBlock",
    "AgentTimelineService",
    "BlockCost",
    "BlockCounters",
    "SessionNode",
]
