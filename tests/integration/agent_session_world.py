"""A small world of agent sessions, audit rows and AI usage for the agent session page tests.

Not a test module: the REST, web and service tests build it under their own
key prefix, so modules running on different workers never share unique keys.

- Project ``<prefix>P`` (private) with a manager (every permission), a member
  (``view_issues``, default issue visibility) and an agent service account
  (every permission); ``<prefix>Q``, another project the agent works in.
- Issues: ``pub`` (visible to the member) and ``priv`` (private, the manager's).
- Sessions of the agent: ``old`` (REST, two days old, no issue, client id
  ``<low>-root``), ``live`` (MCP, active, on ``pub``, client id ``<low>-live``,
  child of ``old``) and ``hidden`` (MCP, on ``priv``, no client id).
- Audit rows of ``live``: ``whoami`` (no project), ``create_issue`` in P,
  ``search`` in Q and a project-less ``show_issue`` naming Q's issue.
- AI usage of the agent: on ``live`` by client id (counted, 1.50 USD) and an
  excluded one; on ``hidden`` by time overlap (on ``priv``, 2.00 USD).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import bounded_log
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory


@dataclass
class SessionWorld:
    admin: User
    manager: User
    member: User
    outsider: User
    agent: User
    p: Project
    q: Project
    pub: Issue
    priv: Issue
    q_issue: Issue
    old: AgentSession
    live: AgentSession
    hidden: AgentSession
    agent_raw_key: str


async def _project(db: AsyncSession, key: str) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=False)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db.flush()
    return project


async def _join(db: AsyncSession, user: User, project: Project, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _issue(db: AsyncSession, project: Project, lookups: tuple, author: User, subject: str, **kw: Any) -> Issue:
    tracker, status, priority = lookups
    seq = (
        await db.execute(
            select(func.coalesce(func.max(Issue.sequence_number), 0)).where(Issue.project_id == project.id)
        )
    ).scalar_one()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq + 1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        assigned_to_id=None,
        subject=subject,
        **kw,
    )
    db.add(issue)
    project.issue_sequence = seq + 1
    await db.flush()
    return issue


def _usage(user: User, project: Project, issue: Issue, amount: str, **kw: Any) -> AiUsageEntry:
    day = utcnow().date()
    return AiUsageEntry(
        project_id=project.id,
        issue_id=issue.id,
        user_id=user.id,
        reporter_user_id=user.id,
        source="manual",
        attribution_method="explicit",
        tool_slug="claude-code",
        provider="testlab",
        model="lab-model-1",
        usage_date=day,
        request_count=1,
        input=1000,
        output=100,
        cost_type="api",
        priced_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
        marginal_amount=Decimal(amount),
        marginal_currency="USD",
        api_equivalent_amount=Decimal(amount),
        api_equivalent_currency="USD",
        cost_basis="calculated",
        **kw,
    )


async def add_hidden_parent(db: AsyncSession, w: SessionWorld) -> AgentSession:
    """Make ``live`` the child of a session on Q's issue, which P's pages must not reveal."""
    parent = AgentSession(
        api_key_id=w.live.api_key_id,
        user_id=w.agent.id,
        issue_id=w.q_issue.id,
        client_type="mcp",
        external_session_id="far-parent",
        started_at=w.live.started_at - timedelta(hours=1),
        last_activity_at=w.live.started_at - timedelta(minutes=30),
    )
    db.add(parent)
    await db.flush()
    w.live.parent_session_id = parent.id
    w.live.parent_external_session_id = "far-parent"
    await db.commit()
    return parent


async def build_session_world(db: AsyncSession, prefix: str) -> SessionWorld:
    """Build the world under project keys and logins starting with *prefix*."""
    low = prefix.lower()
    admin = AdminUserFactory.build(login=f"{low}_admin", status="active")
    manager = UserFactory.build(login=f"{low}_manager", status="active")
    member = UserFactory.build(login=f"{low}_member", status="active")
    outsider = UserFactory.build(login=f"{low}_outsider", status="active")
    agent = ServiceAccountFactory.build(login=f"{low}_agent", status="active", display_name=f"{prefix} Robot")
    db.add_all([admin, manager, member, outsider, agent])
    await db.flush()

    status = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    await db.flush()
    lookups = (tracker, status, priority)

    manager_role = Role(
        name=f"{prefix} Manager", permissions=["*"], builtin=0, issues_visibility="all", ai_usage_visibility="all"
    )
    viewer_role = Role(name=f"{prefix} Viewer", permissions=["view_issues"], builtin=0, issues_visibility="default")
    db.add_all([manager_role, viewer_role])
    await db.flush()

    p = await _project(db, f"{prefix}P")
    q = await _project(db, f"{prefix}Q")
    await _join(db, manager, p, manager_role)
    await _join(db, member, p, viewer_role)
    await _join(db, agent, p, manager_role)
    await _join(db, agent, q, manager_role)

    pub = await _issue(db, p, lookups, manager, f"{prefix} open otter")
    priv = await _issue(db, p, lookups, manager, f"{prefix} secret otter", is_private=True)
    q_issue = await _issue(db, q, lookups, manager, f"{prefix} far otter")

    key, raw = await ApiKeyService().create_key(session=db, user_id=agent.id, name=f"{low}-agent")
    now = utcnow()
    old = AgentSession(
        api_key_id=key.id,
        user_id=agent.id,
        client_type="rest",
        client_name="python-httpx",
        client_version="0.28.1",
        external_session_id=f"{low}-root",
        started_at=now - timedelta(days=2, hours=1),
        last_activity_at=now - timedelta(days=2),
    )
    db.add(old)
    await db.flush()
    live = AgentSession(
        api_key_id=key.id,
        user_id=agent.id,
        issue_id=pub.id,
        client_type="mcp",
        client_name="claude-code",
        client_version="2.1.0",
        connection_hash="a" * 64,
        external_session_id=f"{low}-live",
        parent_external_session_id=f"{low}-root",
        parent_session_id=old.id,
        started_at=now - timedelta(hours=1),
        last_activity_at=now - timedelta(minutes=1),
        tool_call_count=3,
    )
    hidden = AgentSession(
        api_key_id=key.id,
        user_id=agent.id,
        issue_id=priv.id,
        client_type="mcp",
        client_name="claude-code",
        connection_hash="b" * 64,
        started_at=now - timedelta(hours=5),
        last_activity_at=now - timedelta(hours=4),
    )
    db.add_all([live, hidden])
    await db.flush()

    def audit(event_type: str, tool: str, project: Project | None, created_at: datetime, **details: Any) -> None:
        db.add(
            bounded_log(
                {
                    "event_type": event_type,
                    "user_id": agent.id,
                    "project_id": project.id if project else None,
                    "agent_session_id": live.id,
                    "details": {"tool": tool, "source": "mcp", **details},
                    "created_at": created_at,
                }
            )
        )

    audit("resource_access", "whoami", None, now - timedelta(minutes=50))
    audit(
        "issue_created",
        f"create_issue_{low}",
        p,
        now - timedelta(minutes=40),
        project_key=p.key,
        issue_ref=pub.display_key,
    )
    audit("search_query", f"search_{low}", q, now - timedelta(minutes=30), project_key=q.key)
    # A project-less row of a tool that acts in projects: it names Q's issue, so P's page must not show it.
    audit("resource_access", "show_issue", None, now - timedelta(minutes=20), issue_ref=q_issue.display_key)

    db.add_all(
        [
            _usage(agent, p, pub, "1.50", external_session_id=f"{low}-live"),
            _usage(agent, p, pub, "9.00", external_session_id=f"{low}-live", excluded_at=now),
            _usage(
                agent,
                p,
                priv,
                "2.00",
                started_at=now - timedelta(hours=4, minutes=30),
                ended_at=now - timedelta(hours=4, minutes=10),
            ),
        ]
    )
    await db.commit()
    clear_role_cache(db)
    return SessionWorld(
        admin=admin,
        manager=manager,
        member=member,
        outsider=outsider,
        agent=agent,
        p=p,
        q=q,
        pub=pub,
        priv=priv,
        q_issue=q_issue,
        old=old,
        live=live,
        hidden=hidden,
        agent_raw_key=raw,
    )
