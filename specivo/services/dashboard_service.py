"""The data behind the home dashboard: attention counts, active agent sessions, AI spend, hours, grouped activity.

Every block reads only what its viewer may see, through the same rules the
full pages apply:

- issues: ``IssueService.visible_issues_clause`` within the projects
  ``ProjectService.accessible_projects_clause`` admits;
- agent sessions: the viewer's own (sessions of their account's API keys),
  with the issue shown only when the viewer can see it; administrators see
  every session;
- AI spend and hours: administrators (the instance report of
  :class:`AiSpendReportService`) and managers of AI spend (the report of
  the projects where their role reads everyone's AI usage); hours of the same
  projects through ``TimeEntryService.visible_entries_clause_any``;
- activity: the journals of visible issues and readable wikis (private
  notes for administrators only), time entries as above, issues created and
  commits linked to visible issues.

Every query is bounded (``LIMIT`` or an aggregate), and lookups are batched,
so the page costs the same number of queries whatever the data volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, true
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.repository import IssueCommitLink, RepositoryCommit
from specivo.models.time_entry import ActiveTimer, TimeEntry
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiPage
from specivo.services.ai_spend_report_service import AiSpendReportService, SpendReport
from specivo.services.api_key_scope import is_global_admin
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, entry_visibility_projects, projects_granting
from specivo.services.project_service import ProjectService
from specivo.services.time_entry_service import TimeEntryService

#: Spend periods of the dashboard switch: today, the last seven days, the running month (UTC days).
SPEND_PERIODS: tuple[str, ...] = ("today", "7d", "month")
#: Activity filters: everyone, agents (service accounts and work done through an API key), people.
ACTIVITY_FILTERS: tuple[str, ...] = ("all", "agents", "people")

#: Active sessions listed at most (the total is counted apart).
SESSIONS_LIMIT = 6
#: Events read per activity source; the merged feed keeps the newest of them.
ACTIVITY_SOURCE_LIMIT = 60
#: Events the grouped feed is built from.
ACTIVITY_EVENTS_LIMIT = 60
#: Groups shown.
ACTIVITY_GROUPS_LIMIT = 8
#: Rows per spend breakdown.
BREAKDOWN_LIMIT = 5

_CLOSED = ("closed",)
_FINISHED = ("done", "closed")


def _visible(clause: ColumnElement[bool] | None) -> ColumnElement[bool]:
    """*clause*, or an always-true predicate when there is none (administrators)."""
    return true() if clause is None else clause


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@dataclass
class DashboardScope:
    """What the viewer may reach, computed once per request and shared by the blocks."""

    user: User
    projects: list[Project]
    project_ids: set[int]
    wiki_project_ids: set[int]
    issue_visibility: ColumnElement[bool] | None

    @property
    def is_admin(self) -> bool:
        return is_global_admin(self.user)

    def visible_issue_ids(self) -> Any:
        """A subquery of the ids of the issues the viewer may see."""
        return select(Issue.id).where(Issue.project_id.in_(self.project_ids), _visible(self.issue_visibility))


async def load_scope(session: AsyncSession, user: User) -> DashboardScope:
    """The projects *user* may reach (by name), those whose wiki they may read, and the issue visibility clause."""
    stmt = select(Project).order_by(Project.name)
    accessible = await ProjectService().accessible_projects_clause(session, user)
    if accessible is not None:
        stmt = stmt.where(accessible)
    projects = list((await session.execute(stmt)).scalars().all())
    wiki_ids = await projects_granting(session, user, projects, Permission.VIEW_WIKI) if projects else set()
    return DashboardScope(
        user=user,
        projects=projects,
        project_ids={p.id for p in projects},
        wiki_project_ids=wiki_ids,
        issue_visibility=await IssueService().visible_issues_clause(session, user),
    )


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------


@dataclass
class AttentionCounts:
    open_issues: int = 0
    feedback: int = 0
    blocked: int = 0
    overdue: int = 0


def _blocked_clause(scope: DashboardScope) -> ColumnElement[bool]:
    """Issues another visible, unfinished issue blocks (an open ``blocks`` relation)."""
    blocker = aliased(Issue)
    blocker_status = aliased(IssueStatus)
    return exists().where(
        IssueRelation.issue_to_id == Issue.id,
        IssueRelation.relation_type == "blocks",
        blocker.id == IssueRelation.issue_from_id,
        blocker_status.id == blocker.status_id,
        blocker_status.category.not_in(_FINISHED),
        blocker.id.in_(scope.visible_issue_ids()),
    )


async def attention_counts(session: AsyncSession, scope: DashboardScope, today: date) -> AttentionCounts:
    """Counts over the viewer's assigned issues, in one query.

    - open: not in a ``closed`` status (the "My open issues" table);
    - feedback: in a status named Feedback;
    - overdue: unfinished (not ``done`` or ``closed``) and due before *today*;
    - blocked: unfinished and blocked by an unfinished issue the viewer can see.
    """
    if not scope.project_ids:
        return AttentionCounts()
    unfinished = IssueStatus.category.not_in(_FINISHED)
    row = (
        await session.execute(
            select(
                func.count().filter(IssueStatus.category.not_in(_CLOSED)),
                func.count().filter(func.lower(IssueStatus.name) == "feedback"),
                func.count().filter(unfinished, _blocked_clause(scope)),
                func.count().filter(unfinished, Issue.due_date.is_not(None), Issue.due_date < today),
            )
            .select_from(Issue)
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(
                Issue.assigned_to_id == scope.user.id,
                Issue.project_id.in_(scope.project_ids),
                _visible(scope.issue_visibility),
            )
        )
    ).one()
    return AttentionCounts(open_issues=row[0], feedback=row[1], blocked=row[2], overdue=row[3])


async def my_open_issues(
    session: AsyncSession, scope: DashboardScope, *, offset: int, limit: int
) -> tuple[list[Issue], int]:
    """A page of the viewer's open assigned issues, most recently updated first, and their total."""
    if not scope.project_ids:
        return [], 0
    where = (
        Issue.assigned_to_id == scope.user.id,
        IssueStatus.category.not_in(_CLOSED),
        Issue.project_id.in_(scope.project_ids),
        _visible(scope.issue_visibility),
    )
    total = (
        await session.execute(
            select(func.count()).select_from(Issue).join(IssueStatus, Issue.status_id == IssueStatus.id).where(*where)
        )
    ).scalar_one()
    issues = list(
        (
            await session.execute(
                select(Issue)
                .join(IssueStatus, Issue.status_id == IssueStatus.id)
                .where(*where)
                .options(selectinload(Issue.tracker), selectinload(Issue.status), selectinload(Issue.priority))
                .order_by(Issue.updated_at.desc(), Issue.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return issues, int(total)


async def issue_flags(
    session: AsyncSession, scope: DashboardScope, issues: list[Issue], today: date
) -> dict[int, dict[str, bool]]:
    """Per issue of *issues*: ``overdue``, ``blocked`` and ``agent`` (an active agent session works on it)."""
    if not issues:
        return {}
    ids = [i.id for i in issues]
    blocked = set(
        (await session.execute(select(Issue.id).where(Issue.id.in_(ids), _blocked_clause(scope)))).scalars().all()
    )
    cutoff = utcnow() - timedelta(seconds=get_settings().agent_session_timeout)
    agent = set(
        (
            await session.execute(
                select(AgentSession.issue_id)
                .where(AgentSession.issue_id.in_(ids), AgentSession.last_activity_at >= cutoff)
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    flags: dict[int, dict[str, bool]] = {}
    for issue in issues:
        finished = issue.status is not None and issue.status.category in _FINISHED
        flags[issue.id] = {
            "overdue": not finished and issue.due_date is not None and issue.due_date < today,
            "blocked": not finished and issue.id in blocked,
            "agent": issue.id in agent,
        }
    return flags


# ---------------------------------------------------------------------------
# Agents working now
# ---------------------------------------------------------------------------


@dataclass
class ActiveSessionView:
    id: int
    user_id: int
    account: str
    is_service_account: bool
    client_name: str | None
    client_version: str | None
    started_at: datetime
    last_activity_at: datetime
    issue_key: str | None = None
    issue_subject: str | None = None
    status_name: str | None = None
    timer_started_at: datetime | None = None

    @property
    def timer_elapsed(self) -> str | None:
        """The running timer on the session's issue as ``H:MM``, or None."""
        if self.timer_started_at is None:
            return None
        minutes = max(0, int((utcnow() - self.timer_started_at).total_seconds() // 60))
        return f"{minutes // 60}:{minutes % 60:02d}"


def _sessions_visible(scope: DashboardScope) -> list[ColumnElement[bool]]:
    """Administrators see every session; everyone else only the sessions of their own account (their own keys)."""
    if scope.is_admin:
        return []
    return [AgentSession.user_id == scope.user.id]


async def active_sessions(
    session: AsyncSession, scope: DashboardScope, *, limit: int = SESSIONS_LIMIT
) -> tuple[list[ActiveSessionView], int]:
    """The agent sessions active now (last activity within the session timeout) the viewer may see, and their count.

    Newest activity first. A session's running timer is its account's timer
    when it runs on the session's issue.
    """
    cutoff = utcnow() - timedelta(seconds=get_settings().agent_session_timeout)
    conditions = [AgentSession.last_activity_at >= cutoff, *_sessions_visible(scope)]
    total = (await session.execute(select(func.count()).select_from(AgentSession).where(*conditions))).scalar_one()
    if not total:
        return [], 0
    rows = (
        await session.execute(
            select(AgentSession, User.login, User.display_name, User.is_service_account)
            .join(User, User.id == AgentSession.user_id)
            .where(*conditions)
            .order_by(AgentSession.last_activity_at.desc(), AgentSession.id.desc())
            .limit(limit)
        )
    ).all()
    views = [
        ActiveSessionView(
            id=s.id,
            user_id=s.user_id,
            account=login if is_sa else (display_name or login),
            is_service_account=bool(is_sa),
            client_name=s.client_name,
            client_version=s.client_version,
            started_at=s.started_at,
            last_activity_at=s.last_activity_at,
        )
        for s, login, display_name, is_sa in rows
    ]
    issue_of = {s.id: s.issue_id for s, *_rest in rows if s.issue_id is not None}
    if issue_of:
        issue_rows = (
            await session.execute(
                select(Issue.id, Issue.project_key, Issue.sequence_number, Issue.subject, IssueStatus.name)
                .join(IssueStatus, IssueStatus.id == Issue.status_id)
                # An issue the viewer can no longer see is left off their session.
                .where(Issue.id.in_(set(issue_of.values())), Issue.id.in_(scope.visible_issue_ids()))
            )
        ).all()
        issues = {r[0]: r for r in issue_rows}
        timers = {
            (t.user_id, t.issue_id): t.started_at
            for t in (
                await session.execute(
                    select(ActiveTimer).where(
                        ActiveTimer.user_id.in_({v.user_id for v in views}),
                        ActiveTimer.issue_id.in_(set(issue_of.values())),
                    )
                )
            ).scalars()
        }
        for view in views:
            issue_id = issue_of.get(view.id)
            row = issues.get(issue_id) if issue_id is not None else None
            if row is None:
                continue
            view.issue_key = f"{row[1]}-{row[2]}"
            view.issue_subject = row[3]
            view.status_name = row[4]
            view.timer_started_at = timers.get((view.user_id, issue_id))
    return views, int(total)


# ---------------------------------------------------------------------------
# AI spend and hours
# ---------------------------------------------------------------------------


async def spend_projects(session: AsyncSession, scope: DashboardScope) -> list[Project] | None:
    """The projects whose AI spend the viewer manages, or None for an administrator (the whole instance).

    A manager of AI spend in a project holds a role granting ``view_ai_usage``
    with the AI usage visibility "all" there: they read everyone's entries,
    as in the project's spend report. Empty: the viewer gets no spend block.
    """
    if scope.is_admin:
        return None
    managed = await entry_visibility_projects(session, scope.user, Permission.VIEW_AI_USAGE, "ai_usage_visibility")
    ids = set(managed.everything) & scope.project_ids
    return [p for p in scope.projects if p.id in ids]


def period_range(period: str, today: date) -> tuple[date, date]:
    """The UTC days of a :data:`SPEND_PERIODS` value (unknown values read as ``month``)."""
    if period == "today":
        return today, today
    if period == "7d":
        return today - timedelta(days=6), today
    return today.replace(day=1), today


@dataclass
class SpendSummary:
    """The dashboard's spend block: two reports over the same entries, and the hours logged."""

    period: str
    date_from: date
    date_to: date
    by_project: SpendReport
    by_model: SpendReport
    agent_hours: Decimal = Decimal(0)
    people_hours: Decimal = Decimal(0)

    @property
    def totals(self) -> Any:
        return self.by_project.totals


async def spend_summary(
    session: AsyncSession, scope: DashboardScope, period: str, today: date, projects: list[Project] | None
) -> SpendSummary:
    """AI spend of *period* grouped by project and by model, from :class:`AiSpendReportService`.

    *projects* is :func:`spend_projects`: None reads the instance report
    (administrators), a list the report of that set of projects (managers).
    Hours cover the same projects. Sorted by name, as the reports are by
    default.
    """
    first, last = period_range(period, today)
    reports = AiSpendReportService()
    common: dict[str, Any] = {"project": None, "projects": projects, "date_from": first, "date_to": last}
    by_project = await reports.report(session, scope.user, group_by=("project",), **common)
    by_model = await reports.report(session, scope.user, group_by=("model",), **common)
    hours_ids = scope.project_ids if projects is None else {p.id for p in projects}
    agent_hours, people_hours = await logged_hours(session, scope, first, last, hours_ids)
    return SpendSummary(
        period=period,
        date_from=first,
        date_to=last,
        by_project=by_project,
        by_model=by_model,
        agent_hours=agent_hours,
        people_hours=people_hours,
    )


async def logged_hours(
    session: AsyncSession, scope: DashboardScope, first: date, last: date, project_ids: set[int]
) -> tuple[Decimal, Decimal]:
    """Hours logged in *project_ids* from *first* to *last* the viewer may see: ``(service accounts, people)``.

    Time entries do not record the API key they came through, so agent time
    is the time of service accounts; time a person's agent logged under the
    person's own account counts as the person's.
    """
    if not project_ids:
        return Decimal(0), Decimal(0)
    stmt = (
        select(User.is_service_account, func.coalesce(func.sum(TimeEntry.hours), 0))
        .join(User, User.id == TimeEntry.user_id)
        .where(TimeEntry.spent_on.between(first, last), TimeEntry.project_id.in_(project_ids))
        .group_by(User.is_service_account)
    )
    visible = await TimeEntryService().visible_entries_clause_any(session, scope.user)
    if visible is not None:
        stmt = stmt.where(visible)
    hours = {bool(sa): Decimal(total) for sa, total in (await session.execute(stmt)).all()}
    return hours.get(True, Decimal(0)), hours.get(False, Decimal(0))


# ---------------------------------------------------------------------------
# Grouped activity
# ---------------------------------------------------------------------------


@dataclass
class ActivityEvent:
    """One thing that happened: ``kind`` is updated, status, comment, resolved, created, time, wiki or commit."""

    ts: datetime
    kind: str
    user_id: int
    via_key: int | None
    target: tuple[str, int]
    text: str = ""
    hours: Decimal = Decimal(0)
    activity: str = ""


@dataclass
class ActivityGroup:
    """Consecutive events by one actor on one issue or wiki page."""

    actor: str
    is_agent: bool
    is_service_account: bool
    via_client: str | None
    target_kind: str
    target_key: str
    target_title: str
    target_url: str
    events: list[ActivityEvent] = field(default_factory=list)

    @property
    def last_at(self) -> datetime:
        return self.events[0].ts

    def count(self, *kinds: str) -> int:
        return sum(1 for e in self.events if e.kind in kinds)

    @property
    def hours(self) -> Decimal:
        return sum((e.hours for e in self.events if e.kind == "time"), Decimal(0))

    @property
    def activities(self) -> list[str]:
        return sorted({e.activity for e in self.events if e.kind == "time" and e.activity})


def _actor_filter(who: str, is_sa: Any, via_key: Any | None) -> list[ColumnElement[bool]]:
    """The SQL filter of an activity source for *who*; *via_key* is the source's API key column, if any."""
    if who == "agents":
        return [is_sa.is_(True) if via_key is None else or_(is_sa.is_(True), via_key.is_not(None))]
    if who == "people":
        return [is_sa.is_(False)] if via_key is None else [is_sa.is_(False), via_key.is_(None)]
    return []


async def activity_groups(
    session: AsyncSession,
    scope: DashboardScope,
    who: str = "all",
    *,
    groups_limit: int = ACTIVITY_GROUPS_LIMIT,
) -> list[ActivityGroup]:
    """The newest activity the viewer may see, consecutive events by one actor on one target folded into a group.

    Sources: issue and wiki journals, time entries, issues created and
    commits linked to issues (by their matched account). An event is an
    agent's when its account is a service account or it came through an API
    key (journals record the key). Each source is read up to
    :data:`ACTIVITY_SOURCE_LIMIT` rows; the merged feed keeps the newest
    :data:`ACTIVITY_EVENTS_LIMIT` events.
    """
    if not scope.project_ids:
        return []
    who = who if who in ACTIVITY_FILTERS else "all"
    events: list[ActivityEvent] = []
    visible_issues = scope.visible_issue_ids()

    # Journals: issue changes and comments, wiki edits.
    journal_stmt = (
        select(Journal)
        .join(User, User.id == Journal.user_id)
        .options(selectinload(Journal.details))
        .where(
            Journal.project_id.in_(scope.project_ids),
            or_(
                Journal.issue_id.in_(visible_issues),
                and_(Journal.wiki_page_id.is_not(None), Journal.project_id.in_(scope.wiki_project_ids or {0})),
            ),
            *_actor_filter(who, User.is_service_account, Journal.api_key_id),
        )
        .order_by(Journal.created_at.desc(), Journal.id.desc())
        .limit(ACTIVITY_SOURCE_LIMIT)
    )
    # Private notes are shown to administrators only, as on the issue page.
    if not scope.user.is_admin:
        journal_stmt = journal_stmt.where(Journal.is_private.is_(False))
    journals = list((await session.execute(journal_stmt)).scalars().all())
    finished = {
        str(sid): name
        for sid, name in (
            await session.execute(select(IssueStatus.id, IssueStatus.name).where(IssueStatus.category.in_(_FINISHED)))
        ).all()
    }
    status_names = {str(sid): name for sid, name in (await session.execute(select(IssueStatus.id, IssueStatus.name)))}
    for j in journals:
        target = ("issue", j.issue_id) if j.issue_id is not None else ("wiki", j.wiki_page_id or 0)

        def journal_event(kind: str, text: str = "", j: Journal = j, target: tuple[str, int] = target) -> None:
            events.append(
                ActivityEvent(
                    ts=j.created_at, kind=kind, user_id=j.user_id, via_key=j.api_key_id, target=target, text=text
                )
            )

        if target[0] == "wiki":
            journal_event("wiki")
            continue
        status = next((d for d in j.details if d.prop_key == "status_id"), None)
        if status is not None and status.new_value in finished:
            journal_event("resolved", finished[status.new_value])
        elif status is not None:
            journal_event("status", status_names.get(status.new_value or "", ""))
        elif j.details:
            journal_event("updated")
        if j.notes:
            journal_event("comment", " ".join(j.notes.split())[:80])

    # Time logged.
    te_stmt = (
        select(TimeEntry)
        .join(User, User.id == TimeEntry.user_id)
        .options(selectinload(TimeEntry.activity))
        .where(
            TimeEntry.project_id.in_(scope.project_ids),
            TimeEntry.issue_id.in_(visible_issues),
            *_actor_filter(who, User.is_service_account, None),
        )
        .order_by(TimeEntry.created_at.desc(), TimeEntry.id.desc())
        .limit(ACTIVITY_SOURCE_LIMIT)
    )
    te_visible = await TimeEntryService().visible_entries_clause_any(session, scope.user)
    if te_visible is not None:
        te_stmt = te_stmt.where(te_visible)
    for te in (await session.execute(te_stmt)).scalars():
        events.append(
            ActivityEvent(
                ts=te.created_at,
                kind="time",
                user_id=te.user_id,
                via_key=None,
                target=("issue", te.issue_id or 0),
                hours=Decimal(te.hours),
                activity=te.activity.name if te.activity else "",
            )
        )

    # Issues created.
    created_stmt = (
        select(Issue.id, Issue.author_id, Issue.created_at)
        .join(User, User.id == Issue.author_id)
        .where(Issue.id.in_(visible_issues), *_actor_filter(who, User.is_service_account, None))
        .order_by(Issue.created_at.desc(), Issue.id.desc())
        .limit(ACTIVITY_SOURCE_LIMIT)
    )
    for issue_id, author_id, created_at in (await session.execute(created_stmt)).all():
        events.append(
            ActivityEvent(ts=created_at, kind="created", user_id=author_id, via_key=None, target=("issue", issue_id))
        )

    # Commits linked to issues, by the account their author was matched to.
    commit_stmt = (
        select(
            IssueCommitLink.issue_id,
            RepositoryCommit.matched_user_id,
            func.coalesce(RepositoryCommit.committed_at, RepositoryCommit.created_at),
            RepositoryCommit.sha,
            RepositoryCommit.message,
        )
        .join(RepositoryCommit, RepositoryCommit.id == IssueCommitLink.commit_id)
        .join(User, User.id == RepositoryCommit.matched_user_id)
        .where(IssueCommitLink.issue_id.in_(visible_issues), *_actor_filter(who, User.is_service_account, None))
        .order_by(func.coalesce(RepositoryCommit.committed_at, RepositoryCommit.created_at).desc())
        .limit(ACTIVITY_SOURCE_LIMIT)
    )
    for issue_id, user_id, ts, sha, message in (await session.execute(commit_stmt)).all():
        if user_id is None:
            continue
        first_line = (message or "").strip().splitlines()[0][:80] if (message or "").strip() else ""
        events.append(
            ActivityEvent(
                ts=ts,
                kind="commit",
                user_id=user_id,
                via_key=None,
                target=("issue", issue_id),
                text=f"{sha[:7]} {first_line}".strip(),
            )
        )

    events.sort(key=lambda e: e.ts, reverse=True)
    events = events[:ACTIVITY_EVENTS_LIMIT]
    return await _fold(session, scope, events, groups_limit)


async def _fold(
    session: AsyncSession, scope: DashboardScope, events: list[ActivityEvent], groups_limit: int
) -> list[ActivityGroup]:
    """Fold consecutive events of one actor (and key) on one target into groups, and label them."""
    runs: list[tuple[tuple[Any, ...], list[ActivityEvent]]] = []
    for event in events:
        key = (event.user_id, event.via_key is not None, event.target)
        if runs and runs[-1][0] == key:
            runs[-1][1].append(event)
        elif len(runs) < groups_limit:
            runs.append((key, [event]))
        else:
            break
    if not runs:
        return []

    user_ids = {key[0] for key, _ in runs}
    issue_ids = {key[2][1] for key, _ in runs if key[2][0] == "issue"}
    wiki_ids = {key[2][1] for key, _ in runs if key[2][0] == "wiki"}
    key_ids = {e.via_key for _, evs in runs for e in evs if e.via_key is not None}

    users = {
        u.id: u
        for u in (
            await session.execute(
                select(User.id, User.login, User.display_name, User.is_service_account).where(User.id.in_(user_ids))
            )
        ).all()
    }
    issues: dict[int, tuple[str, str]] = {}
    if issue_ids:
        for iid, pkey, seq, subject in (
            await session.execute(
                select(Issue.id, Issue.project_key, Issue.sequence_number, Issue.subject).where(Issue.id.in_(issue_ids))
            )
        ).all():
            issues[iid] = (f"{pkey}-{seq}", subject)
    pages: dict[int, tuple[str, str, str]] = {}
    if wiki_ids:
        for pid, title, slug, pkey in (
            await session.execute(
                select(WikiPage.id, WikiPage.title, WikiPage.slug, Project.key)
                .join(Wiki, WikiPage.wiki_id == Wiki.id)
                .join(Project, Wiki.project_id == Project.id)
                .where(WikiPage.id.in_(wiki_ids))
            )
        ).all():
            pages[pid] = (title, slug, pkey)
    clients: dict[int, str] = {}
    if key_ids:
        # The client of each key's latest session names the agent ("via claude-code").
        latest = (
            select(AgentSession.api_key_id, AgentSession.client_name)
            .where(AgentSession.api_key_id.in_(key_ids), AgentSession.client_name.is_not(None))
            .ext(distinct_on(AgentSession.api_key_id))
            .order_by(AgentSession.api_key_id, AgentSession.last_activity_at.desc())
        )
        clients = {kid: name or "" for kid, name in (await session.execute(latest)).all()}

    groups: list[ActivityGroup] = []
    for (user_id, via, target), evs in runs:
        user = users.get(user_id)
        if user is None:
            continue
        is_sa = bool(user.is_service_account)
        via_key = next((e.via_key for e in evs if e.via_key is not None), None)
        if target[0] == "issue":
            if target[1] not in issues:
                continue
            target_key, title = issues[target[1]]
            url = f"/issue/{target_key}/"
        else:
            if target[1] not in pages:
                continue
            title, slug, pkey = pages[target[1]]
            target_key, url = pkey, f"/projects/{pkey}/wiki/{slug}/"
        groups.append(
            ActivityGroup(
                actor=user.login if is_sa else (user.display_name or user.login),
                is_agent=is_sa or via,
                is_service_account=is_sa,
                via_client=(clients.get(via_key, "") if via_key is not None else None) if via else None,
                target_kind=target[0],
                target_key=target_key,
                target_title=title,
                target_url=url,
                events=evs,
            )
        )
    return groups


__all__ = [
    "ACTIVITY_FILTERS",
    "SPEND_PERIODS",
    "ActiveSessionView",
    "ActivityEvent",
    "ActivityGroup",
    "AttentionCounts",
    "DashboardScope",
    "SpendSummary",
    "active_sessions",
    "activity_groups",
    "attention_counts",
    "issue_flags",
    "load_scope",
    "logged_hours",
    "my_open_issues",
    "period_range",
    "spend_projects",
    "spend_summary",
]
