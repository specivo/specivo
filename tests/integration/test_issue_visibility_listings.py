"""Every listing of issues applies the same visibility as the issue list.

The world: a public project P (opted in to anonymous reading) holds a public
and a private issue in a sprint and a version, and a public and a private
issue in the backlog. Its readers:

- the manager, a member whose role sees every issue;
- the reporter, a member whose role sees issues but not other people's
  private ones;
- the outsider, a member of another (private) project only, who reads P as a
  non-member;
- anonymous visitors, once the instance switch is on;
- a key of the manager limited to another project S.

None of them but the manager may learn that the private issues exist: not
through a sprint board or backlog (REST and web), a recurring pattern's
instances, the dashboard's agent activity, the project's agent sessions, nor
through an issue key autolink, where a hidden key must render exactly like a
key that does not exist. Token logs may only be written to the caller's own
agent session.

The MCP version and sprint issue lists are covered in
``test_mcp_issue_visibility_listings`` (serial).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.agent_costs import log_tokens
from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError
from specivo.models.agent_cost import AgentTokenLog
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.models.version import Version
from specivo.schemas.agent_cost import AgentTokenLogCreate
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.api_key_scope import ProjectScope, bound_scope
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, clear_role_cache
from specivo.services.sprint_service import SprintService
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.recurring_pattern import RecurringPatternFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory
from tests.factories.version import VersionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_MODULES = ("issue_tracking", "wiki", "time_tracking")
_issue_svc = IssueService()
_sprint_svc = SprintService()


@dataclass
class World:
    admin: User
    manager: User
    reporter: User
    outsider: User
    agent: User
    p: Project
    s: Project
    n: Project
    sprint: Sprint
    version: Version
    pub: Issue
    priv: Issue
    pub_backlog: Issue
    priv_backlog: Issue
    s_issue: Issue
    n_issue: Issue
    limited_raw: str
    agent_raw: str
    wiki_slug: str


async def _project(db: AsyncSession, key: str, *, is_public: bool) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=is_public)
    db.add(project)
    await db.flush()
    db.add_all([EnabledModule(project_id=project.id, name=name) for name in _MODULES])
    await db.flush()
    return project


async def _join(db: AsyncSession, user: User, project: Project, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _issue(
    db: AsyncSession,
    project: Project,
    lookups: tuple,
    author: User,
    subject: str,
    *,
    is_private: bool = False,
    sprint: Sprint | None = None,
    version: Version | None = None,
) -> Issue:
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
        # Unassigned: a private issue nobody but its author may see.
        assigned_to_id=None,
        subject=subject,
        is_private=is_private,
        sprint_id=sprint.id if sprint else None,
        fixed_version_id=version.id if version else None,
    )
    db.add(issue)
    project.issue_sequence = seq + 1
    await db.flush()
    return issue


async def build_world(db: AsyncSession, prefix: str) -> World:
    """Build the module's world under project keys starting with *prefix*."""
    low = prefix.lower()
    admin = AdminUserFactory.build(login=f"{low}_admin", status="active")
    manager = UserFactory.build(login=f"{low}_manager", status="active")
    reporter = UserFactory.build(login=f"{low}_reporter", status="active")
    outsider = UserFactory.build(login=f"{low}_outsider", status="active")
    agent = ServiceAccountFactory.build(login=f"{low}_agent", status="active")
    db.add_all([admin, manager, reporter, outsider, agent])
    await db.flush()

    status = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    await db.flush()
    lookups = (tracker, status, priority)

    sees_all = Role(name=f"{prefix} Manager", permissions=["*"], builtin=0, issues_visibility="all")
    sees_default = Role(
        name=f"{prefix} Reporter",
        permissions=["view_issues", "add_issues", "view_wiki_pages"],
        builtin=0,
        issues_visibility="default",
    )
    db.add_all([sees_all, sees_default])
    await db.flush()

    p = await _project(db, f"{prefix}P", is_public=True)
    s = await _project(db, f"{prefix}S", is_public=False)
    o = await _project(db, f"{prefix}O", is_public=False)
    n = await _project(db, f"{prefix}N", is_public=True)
    await _join(db, manager, p, sees_all)
    await _join(db, manager, s, sees_all)
    await _join(db, reporter, p, sees_default)
    await _join(db, outsider, o, sees_default)
    await _join(db, agent, p, sees_all)

    sprint = Sprint(project_id=p.id, name=f"{prefix} sprint", status="active")
    version = VersionFactory.build(project_id=p.id, name=f"{prefix} release")
    db.add_all([sprint, version])
    await db.flush()

    pub = await _issue(db, p, lookups, manager, f"{prefix} open marmot", sprint=sprint, version=version)
    priv = await _issue(
        db, p, lookups, manager, f"{prefix} secret marmot", is_private=True, sprint=sprint, version=version
    )
    pub_backlog = await _issue(db, p, lookups, manager, f"{prefix} open backlog marmot")
    priv_backlog = await _issue(db, p, lookups, manager, f"{prefix} secret backlog marmot", is_private=True)
    s_issue = await _issue(db, s, lookups, manager, f"{prefix} closed-door marmot")
    n_issue = await _issue(db, n, lookups, manager, f"{prefix} not-opted-in marmot")

    keys = ApiKeyService()
    limited, limited_raw = await keys.create_key(session=db, user_id=manager.id, name=f"{low}-limited")
    # Stored directly: what create_key keeps when the api_key_scopes feature is on.
    limited.scopes = {"projects": [s.key]}
    _agent_key, agent_raw = await keys.create_key(session=db, user_id=agent.id, name=f"{low}-agent")
    await db.commit()

    page, _content = await WikiService().create_page(
        db, p.id, "Marmot notes", _refs_text(pub, priv, s_issue, n_issue), admin
    )
    await set_anonymous_permissions(db, p, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI], admin)
    await db.commit()
    clear_role_cache(db)

    return World(
        admin=admin,
        manager=manager,
        reporter=reporter,
        outsider=outsider,
        agent=agent,
        p=p,
        s=s,
        n=n,
        sprint=sprint,
        version=version,
        pub=pub,
        priv=priv,
        pub_backlog=pub_backlog,
        priv_backlog=priv_backlog,
        s_issue=s_issue,
        n_issue=n_issue,
        limited_raw=limited_raw,
        agent_raw=agent_raw,
        wiki_slug=page.slug,
    )


def _refs_text(*issues: Issue) -> str:
    return "Related: " + ", ".join(i.display_key for i in issues) + ", ZZNOPE-9."


async def go_live(db: AsyncSession, w: World) -> None:
    """Turn the anonymous access instance switch on, with P the only opted-in project."""
    await set_anonymous_access_enabled(db, True, w.admin, confirmed_projects=[w.p.key])
    await db.commit()
    clear_role_cache(db)


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    clear_role_cache(db_session)
    w = await build_world(db_session, "VIZ")
    yield w
    clear_role_cache(db_session)


async def _bearer(client: AsyncClient, user: User) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _cookie(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _subjects(items: list[dict]) -> set[str]:
    return {i["subject"] for i in items}


# ---------------------------------------------------------------------------
# Sprint board and backlog (REST)
# ---------------------------------------------------------------------------


async def _board_and_backlog(client: AsyncClient, w: World, headers: dict[str, str]) -> tuple[set[str], set[str]]:
    resp = await client.get(f"/api/v1/projects/{w.p.key}/sprints/{w.sprint.id}/board/", headers=headers)
    assert resp.status_code == 200, resp.text
    board = {subject for column in resp.json().values() for subject in _subjects(column)}
    resp = await client.get(f"/api/v1/projects/{w.p.key}/backlog/", headers=headers)
    assert resp.status_code == 200, resp.text
    return board, _subjects(resp.json())


async def test_rest_board_and_backlog_hide_private_issues_from_a_member_without_the_right(
    client: AsyncClient, world: World
) -> None:
    w = world
    board, backlog = await _board_and_backlog(client, w, await _bearer(client, w.reporter))
    assert board == {w.pub.subject}
    assert backlog == {w.pub_backlog.subject}


async def test_rest_board_and_backlog_hide_private_issues_from_a_member_of_another_project(
    client: AsyncClient, world: World
) -> None:
    w = world
    board, backlog = await _board_and_backlog(client, w, await _bearer(client, w.outsider))
    assert board == {w.pub.subject}
    assert backlog == {w.pub_backlog.subject}


async def test_rest_board_and_backlog_show_everything_to_a_member_who_sees_all(
    client: AsyncClient, world: World
) -> None:
    w = world
    board, backlog = await _board_and_backlog(client, w, await _bearer(client, w.manager))
    assert board == {w.pub.subject, w.priv.subject}
    assert backlog == {w.pub_backlog.subject, w.priv_backlog.subject}


async def test_rest_board_and_backlog_are_not_found_for_a_key_limited_to_another_project(
    client: AsyncClient, world: World
) -> None:
    w = world
    headers = {"Authorization": f"Bearer {w.limited_raw}"}
    resp = await client.get(f"/api/v1/projects/{w.p.key}/sprints/{w.sprint.id}/board/", headers=headers)
    assert resp.status_code == 404
    resp = await client.get(f"/api/v1/projects/{w.p.key}/backlog/", headers=headers)
    assert resp.status_code == 404


async def test_sprint_service_lists_follow_the_anonymous_rules(db_session: AsyncSession, world: World) -> None:
    w = world
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None

    # Switch off: the anonymous user sees nothing at all.
    assert await _sprint_svc.board_data(db_session, w.sprint, anonymous) == {}
    assert (await _sprint_svc.backlog_issues(db_session, w.p.id, anonymous))[1] == 0

    await go_live(db_session, w)
    board = await _sprint_svc.board_data(db_session, w.sprint, anonymous)
    assert {i.id for issues in board.values() for i in issues} == {w.pub.id}
    backlog, total = await _sprint_svc.backlog_issues(db_session, w.p.id, anonymous)
    assert [i.id for i in backlog] == [w.pub_backlog.id]
    assert total == 1
    assert await _sprint_svc.sprint_issue_count(db_session, w.sprint.id, anonymous) == 1


async def test_sprint_service_lists_follow_the_key_limit(db_session: AsyncSession, world: World) -> None:
    w = world
    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        assert await _sprint_svc.board_data(db_session, w.sprint, w.manager) == {}
        assert (await _sprint_svc.backlog_issues(db_session, w.p.id, w.manager))[1] == 0
        assert await _sprint_svc.sprint_issue_count(db_session, w.sprint.id, w.manager) == 0


# ---------------------------------------------------------------------------
# Sprint and recurring pattern pages (web)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_web_sprint_pages_hide_private_issues(
    client: AsyncClient, db_session: AsyncSession, world: World, reader: str
) -> None:
    w = world
    user = getattr(w, reader)
    pages = [
        f"/projects/{w.p.key}/backlog/",
        f"/projects/{w.p.key}/sprints/{w.sprint.id}/",
        f"/projects/{w.p.key}/sprints/{w.sprint.id}/board/",
    ]
    for path in pages:
        resp = await client.get(path, cookies=_cookie(user))
        assert resp.status_code == 200, (path, resp.status_code)
        assert "secret" not in resp.text, path
        assert w.priv.display_key not in resp.text and w.priv_backlog.display_key not in resp.text, path
    backlog = (await client.get(pages[0], cookies=_cookie(user))).text
    assert w.pub_backlog.subject in backlog
    detail = (await client.get(pages[1], cookies=_cookie(user))).text
    assert w.pub.subject in detail


async def test_web_sprint_edit_page_hides_private_issues_from_a_sprint_manager_without_the_right(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    planner = Role(
        name="VIZ Planner", permissions=["view_issues", "manage_sprints"], builtin=0, issues_visibility="default"
    )
    db_session.add(planner)
    await db_session.flush()
    member = (
        await db_session.execute(select(Member).where(Member.user_id == w.reporter.id, Member.project_id == w.p.id))
    ).scalar_one()
    db_session.add(MemberRole(member_id=member.id, role_id=planner.id))
    await db_session.commit()
    clear_role_cache(db_session)

    resp = await client.get(f"/projects/{w.p.key}/sprints/{w.sprint.id}/edit/", cookies=_cookie(w.reporter))
    assert resp.status_code == 200
    assert w.pub.subject in resp.text
    assert w.priv.subject not in resp.text


async def test_web_recurring_pattern_page_hides_private_instances(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    pattern = RecurringPatternFactory.build(
        project_id=w.p.id,
        author_id=w.manager.id,
        template_tracker_id=w.pub.tracker_id,
        freq="daily",
        dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )
    db_session.add(pattern)
    await db_session.flush()
    for issue, day in ((w.pub, 1), (w.priv, 2)):
        issue.recurring_pattern_id = pattern.id
        issue.original_occurrence_at = datetime(2026, 1, day, 9, 0, tzinfo=UTC)
    await db_session.commit()

    path = f"/projects/{w.p.key}/recurring-patterns/{pattern.id}/"
    for reader in (w.reporter, w.outsider):
        resp = await client.get(path, cookies=_cookie(reader))
        assert resp.status_code == 200
        assert w.pub.subject in resp.text
        assert w.priv.subject not in resp.text
    resp = await client.get(path, cookies=_cookie(w.manager))
    assert w.priv.subject in resp.text


# ---------------------------------------------------------------------------
# Dashboard agent activity
# ---------------------------------------------------------------------------


async def test_dashboard_agent_activity_leaves_out_hidden_issues(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    for issue, note in ((w.pub, "visible agent remark"), (w.priv, "hidden agent remark")):
        seq = (
            await db_session.execute(
                select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == issue.id)
            )
        ).scalar_one()
        db_session.add(Journal(issue_id=issue.id, project_id=w.p.id, user_id=w.agent.id, notes=note, sequence=seq + 1))
    await db_session.commit()

    resp = await client.get("/", cookies=_cookie(w.reporter))
    assert resp.status_code == 200
    assert "visible agent remark" in resp.text
    assert "hidden agent remark" not in resp.text
    assert w.priv.display_key not in resp.text

    resp = await client.get("/", cookies=_cookie(w.manager))
    assert "hidden agent remark" in resp.text


# ---------------------------------------------------------------------------
# Agent sessions and token logs
# ---------------------------------------------------------------------------


async def _agent_session(db: AsyncSession, user: User, issue: Issue | None) -> AgentSession:
    _key, _raw = await ApiKeyService().create_key(session=db, user_id=user.id, name=f"{user.login}-s")
    session = AgentSession(api_key_id=_key.id, user_id=user.id, issue_id=issue.id if issue else None)
    db.add(session)
    await db.commit()
    return session


async def test_token_logs_are_written_only_to_the_callers_own_session(db_session: AsyncSession, world: World) -> None:
    # The endpoint's router is mounted by an edition plugin, not by core, so
    # the route function is called directly.
    w = world
    own = await _agent_session(db_session, w.agent, None)
    others = await _agent_session(db_session, w.manager, None)

    def body(session_id: int) -> AgentTokenLogCreate:
        return AgentTokenLogCreate(session_id=session_id, model_name="claude-test", input_tokens=10, output_tokens=5)

    for caller, session_id in ((w.agent, others.id), (w.agent, 987654321), (w.admin, own.id)):
        # Not even an administrator may charge tokens to somebody else's session.
        with pytest.raises(NotFoundError):
            await log_tokens(body(session_id), current_user=caller, db=db_session)
    assert (await db_session.execute(select(func.count()).select_from(AgentTokenLog))).scalar_one() == 0

    logged = await log_tokens(body(own.id), current_user=w.agent, db=db_session)
    assert logged.session_id == own.id


async def test_agent_sessions_list_only_the_projects_visible_sessions(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    on_public = await _agent_session(db_session, w.agent, w.pub)
    on_private = await _agent_session(db_session, w.agent, w.priv)
    on_other_project = await _agent_session(db_session, w.manager, w.s_issue)
    no_issue = await _agent_session(db_session, w.agent, None)
    stranger = await _agent_session(db_session, w.outsider, None)

    async def listed(user: User) -> set[int]:
        resp = await client.get(f"/api/v1/projects/{w.p.key}/agent-sessions/", headers=await _bearer(client, user))
        assert resp.status_code == 200, resp.text
        return {item["id"] for item in resp.json()} & {
            on_public.id,
            on_private.id,
            on_other_project.id,
            no_issue.id,
            stranger.id,
        }

    assert await listed(w.reporter) == {on_public.id, no_issue.id}
    assert await listed(w.outsider) == {on_public.id, no_issue.id}
    assert await listed(w.manager) == {on_public.id, on_private.id, no_issue.id}


# ---------------------------------------------------------------------------
# Issue key autolinks
# ---------------------------------------------------------------------------


async def _preview(client: AsyncClient, headers: dict[str, str], text: str) -> str:
    resp = await client.post("/api/v1/markdown/preview/", json={"text": text, "context": "issue"}, headers=headers)
    assert resp.status_code == 200, resp.text
    html: str = resp.json()["html"]
    return html


async def _assert_links(client: AsyncClient, headers: dict[str, str], linked: list[Issue], hidden: list[Issue]) -> None:
    for issue in linked:
        assert f"/issue/{issue.display_key}/" in await _preview(client, headers, f"See {issue.display_key} now.")
    for issue in hidden:
        key = issue.display_key
        html = await _preview(client, headers, f"See {key} now.")
        unknown = await _preview(client, headers, "See ZZNOPE-9 now.")
        # Exactly what a key that does not exist renders as.
        assert html == unknown.replace("ZZNOPE-9", key)


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_autolink_hides_issues_the_reader_cannot_see(client: AsyncClient, world: World, reader: str) -> None:
    w = world
    headers = await _bearer(client, getattr(w, reader))
    await _assert_links(client, headers, linked=[w.pub, w.n_issue], hidden=[w.priv, w.s_issue])


async def test_autolink_links_everything_the_manager_can_see(client: AsyncClient, world: World) -> None:
    w = world
    headers = await _bearer(client, w.manager)
    await _assert_links(client, headers, linked=[w.pub, w.priv, w.s_issue, w.n_issue], hidden=[])


async def test_autolink_follows_the_key_limit(client: AsyncClient, world: World) -> None:
    w = world
    headers = {"Authorization": f"Bearer {w.limited_raw}"}
    await _assert_links(client, headers, linked=[w.s_issue], hidden=[w.pub, w.priv, w.n_issue])


async def test_autolink_follows_the_anonymous_rules(db_session: AsyncSession, world: World) -> None:
    w = world
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    text = _refs_text(w.pub, w.priv, w.s_issue, w.n_issue)

    assert await _issue_svc.resolve_known_issue_refs(db_session, anonymous, text) == set()
    await go_live(db_session, w)
    assert await _issue_svc.resolve_known_issue_refs(db_session, anonymous, text) == {w.pub.display_key}


async def test_autolink_of_a_moved_issue_follows_its_visibility(db_session: AsyncSession, world: World) -> None:
    w = world
    old_key = w.priv.display_key
    await _issue_svc.move(db_session, w.priv, w.s, w.admin)
    await db_session.commit()
    clear_role_cache(db_session)
    assert await _issue_svc.resolve_known_issue_refs(db_session, w.reporter, old_key) == set()
    assert await _issue_svc.resolve_known_issue_refs(db_session, w.manager, old_key) == {old_key}


async def test_anonymous_wiki_page_links_only_visible_issues(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    await go_live(db_session, w)
    resp = await client.get(f"/projects/{w.p.key}/wiki/{w.wiki_slug}/")
    assert resp.status_code == 200, resp.status_code
    assert f"/issue/{w.pub.display_key}/" in resp.text
    for issue in (w.priv, w.s_issue, w.n_issue):
        assert issue.display_key in resp.text
        assert f"/issue/{issue.display_key}/" not in resp.text


async def test_issue_page_links_only_visible_issues(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    w.pub.description = _refs_text(w.priv, w.s_issue, w.n_issue)
    await db_session.commit()
    resp = await client.get(f"/issue/{w.pub.display_key}/", cookies=_cookie(w.reporter))
    assert resp.status_code == 200
    assert f"/issue/{w.n_issue.display_key}/" in resp.text
    for issue in (w.priv, w.s_issue):
        assert f"/issue/{issue.display_key}/" not in resp.text

    await go_live(db_session, w)
    resp = await client.get(f"/issue/{w.pub.display_key}/")
    assert resp.status_code == 200
    for issue in (w.priv, w.s_issue, w.n_issue):
        assert f"/issue/{issue.display_key}/" not in resp.text
