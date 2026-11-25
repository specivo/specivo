"""The time entries lists: a project's and every project's, with project, user and issue filters.

Three private projects with time tracking on:

- ``TLA``: ``alice`` and ``bob`` read every entry (role "all"); bob also
  logged time on a private issue alice cannot see;
- ``TLB``: alice and bob read only their own entries (role "own");
- ``TLC``: only ``carol``; alice is no member.

``dave`` is in no project, ``admin`` is an administrator.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


class World:
    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.users: dict[str, User] = {}
        self.issues: dict[str, Issue] = {}


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    world = World()
    status = StatusFactory.build(name="Tl Open")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Tl Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Tl Normal")
    activity = TimeEntryActivityFactory.build(name="Tl Development")
    db.add_all([tracker, priority, activity])
    for key in ("TLA", "TLB", "TLC"):
        world.projects[key] = ProjectFactory.build(
            key=key, identifier=key.lower(), name=f"Ledger {key}", is_public=False
        )
    db.add_all(world.projects.values())
    await db.flush()
    db.add_all([EnabledModule(project_id=p.id, name="time_tracking") for p in world.projects.values()])

    def role(name: str, visibility: str) -> Role:
        return Role(
            name=name,
            permissions=["view_issues", "log_time", "view_time_entries"],
            builtin=0,
            issues_visibility="all",
            time_entries_visibility=visibility,
        )

    all_role, own_role = role("Tl All", "all"), role("Tl Own", "own")
    db.add_all([all_role, own_role])
    for name in ("alice", "bob", "carol", "dave"):
        world.users[name] = UserFactory.build(login=f"tl_{name}", display_name=f"Tl {name.title()}", status="active")
    world.users["admin"] = AdminUserFactory.build(login="tl_admin", status="active")
    db.add_all(world.users.values())
    await db.flush()

    async def member(project: str, user: str, r: Role) -> None:
        m = Member(project_id=world.projects[project].id, user_id=world.users[user].id)
        db.add(m)
        await db.flush()
        db.add(MemberRole(member_id=m.id, role_id=r.id))

    for user in ("alice", "bob"):
        await member("TLA", user, all_role)
        await member("TLB", user, own_role)
    await member("TLC", "carol", all_role)
    await db.flush()

    def issue(project: str, seq: int, subject: str, **kw) -> Issue:
        p = world.projects[project]
        row = IssueFactory.build(
            project_id=p.id,
            project_key=p.key,
            sequence_number=seq,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=world.users["carol"].id,
            subject=subject,
            **kw,
        )
        world.issues[f"{p.key}-{seq}"] = row
        return row

    db.add_all(
        [
            issue("TLA", 1, "Ledger public task"),
            issue("TLA", 2, "Ledger secret task", is_private=True),
            issue("TLC", 1, "Ledger other team task"),
        ]
    )
    await db.flush()

    def entry(project: str, user: str, hours: str, issue_key: str | None = None, comments: str = "") -> None:
        db.add(
            TimeEntryFactory.build(
                project_id=world.projects[project].id,
                issue_id=world.issues[issue_key].id if issue_key else None,
                user_id=world.users[user].id,
                activity_id=activity.id,
                hours=Decimal(hours),
                comments=comments or f"{user} in {project}",
                spent_on=date(2026, 11, 20),
            )
        )

    entry("TLA", "alice", "2", "TLA-1", "alice-on-public")
    entry("TLA", "bob", "1", "TLA-1", "bob-on-public")
    entry("TLA", "bob", "4", "TLA-2", "bob-on-secret")
    entry("TLB", "alice", "3", comments="alice-in-b")
    entry("TLB", "bob", "5", comments="bob-in-b")
    entry("TLC", "carol", "7", "TLC-1", "carol-in-c")
    await db.commit()
    clear_role_cache(db)
    return world


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _get(client: AsyncClient, user: User, url: str, **params: str) -> Response:
    resp = await client.get(url, params=params, cookies=_cookies(user), follow_redirects=False)
    assert resp.status_code == 200, resp.text[:500]
    return resp


def _text(resp: Response) -> str:
    return " ".join(resp.text.split())


def _comments(resp: Response) -> set[str]:
    words = {"alice-on-public", "bob-on-public", "bob-on-secret", "alice-in-b", "bob-in-b", "carol-in-c"}
    return {w for w in words if w in resp.text}


async def test_every_project_list_shows_what_the_viewer_may_read(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.users["alice"], "/time-entries/")
    assert _comments(resp) == {"alice-on-public", "bob-on-public", "alice-in-b"}
    text = _text(resp)
    assert "Total: 6.00h (3 entries)" in text
    assert 'data-testid="time-project"' in text and "Ledger TLB" in text

    admin = await _get(client, w.users["admin"], "/time-entries/")
    assert len(_comments(admin)) == 6

    dave = await _get(client, w.users["dave"], "/time-entries/")
    assert 'data-testid="time-empty"' in dave.text


async def test_issue_column_links_to_the_issue_by_key(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.users["alice"], "/projects/TLA/time-entries/")
    text = _text(resp)
    assert text.count('data-testid="time-issue-link"') == 2
    assert 'href="/issue/TLA-1/" data-testid="time-issue-link">TLA-1</a>' in text
    assert "Ledger public task" in text
    assert 'href="#"' not in text


async def test_project_filter(client: AsyncClient, w: World) -> None:
    alice = w.users["alice"]
    resp = await _get(client, alice, "/time-entries/", project="tla")
    assert _comments(resp) == {"alice-on-public", "bob-on-public"}
    assert 'data-filter-chip="project"' in resp.text and "Ledger TLA" in resp.text

    # A project the viewer cannot read time entries of matches nothing.
    resp = await _get(client, alice, "/time-entries/", project="TLC")
    assert _comments(resp) == set()


async def test_user_and_issue_filters(client: AsyncClient, w: World) -> None:
    alice, bob, carol = w.users["alice"], w.users["bob"], w.users["carol"]
    resp = await _get(client, alice, "/time-entries/", user_id=str(bob.id))
    assert _comments(resp) == {"bob-on-public"}
    assert 'data-filter-chip="user_id"' in resp.text and "Tl Bob" in resp.text

    # Someone whose entries the viewer cannot read is not named.
    resp = await _get(client, alice, "/time-entries/", user_id=str(carol.id))
    assert _comments(resp) == set() and "Tl Carol" not in resp.text and "Unknown person" in resp.text

    resp = await _get(client, alice, "/projects/TLA/time-entries/", issue="TLA-1")
    assert _comments(resp) == {"alice-on-public", "bob-on-public"}
    resp = await _get(client, alice, "/time-entries/", issue="TLA-2")  # a private issue alice cannot see
    assert _comments(resp) == set()
    resp = await _get(client, alice, "/time-entries/", issue="TLA-1", project="TLB")
    assert _comments(resp) == set()


async def test_suggestion_endpoints(client: AsyncClient, w: World) -> None:
    alice = w.users["alice"]
    projects = (await _get(client, alice, "/time-entries/projects/")).json()
    assert [p["id"] for p in projects] == ["TLA", "TLB"]
    assert projects[0]["name"] == "Ledger TLA (TLA)"
    assert [p["id"] for p in (await _get(client, alice, "/time-entries/projects/", q="tlb")).json()] == ["TLB"]

    people = (await _get(client, alice, "/time-entries/people/")).json()
    assert {p["login"] for p in people} == {"tl_alice", "tl_bob"}
    people = (await _get(client, alice, "/time-entries/people/", project="TLB")).json()
    assert [p["login"] for p in people] == ["tl_alice"]  # own entries only in TLB
    assert (await _get(client, alice, "/time-entries/people/", project="TLC")).json() == []

    issues = (await _get(client, alice, "/time-entries/issues/", q="Ledger")).json()
    assert [i["key"] for i in issues] == ["TLA-1"]

    assert (await _get(client, w.users["dave"], "/time-entries/projects/")).json() == []


async def test_the_sidebar_links_the_every_project_list(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.users["alice"], "/time-entries/")
    assert 'data-testid="sidebar-time-entries"' in resp.text
