"""Time entries on issues the reader cannot see are treated as not there.

A private issue in a project where everyone below holds ``view_time_entries``
with ``time_entries_visibility = 'all'``:

- ``author`` logged time on it and authored it; they keep seeing their entry
  even after losing sight of the issue;
- ``blind`` cannot see the private issue: their lists, sums, billing report,
  the dashboard clause and the single entry (404, edit and delete included)
  leave the entry out;
- ``assignee`` can see it (private issues show to their author and assignee);
- ``admin`` sees everything.

Project-level entries (no issue) and entries on public issues are unaffected.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.agent_cost import BillingRate
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.version import Version
from specivo.services.billing_rate_service import BillingRateService
from specivo.services.permission_service import clear_role_cache
from specivo.services.time_entry_service import TimeEntryService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = TimeEntryService()

PUBLIC_HOURS = Decimal("1.00")
PRIVATE_HOURS = Decimal("4.00")
PROJECT_HOURS = Decimal("2.00")
SPAN = (date(2026, 1, 1), date(2026, 12, 31))


async def _user(db: AsyncSession, login: str, *, admin: bool = False) -> User:
    factory = AdminUserFactory if admin else UserFactory
    user = factory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    return user


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _cookies(client: AsyncClient, login: str) -> dict[str, str]:
    return {"access_token": (await _headers(client, login))["Authorization"].removeprefix("Bearer ")}


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> dict:
    status = StatusFactory.build(name="Tiv Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Tiv Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Tiv Normal")
    activity = TimeEntryActivityFactory.build(name="Tiv Development")
    project = ProjectFactory.build(key="TIV", identifier="tiv", is_public=False)
    db_session.add_all([tracker, priority, activity, project])
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="time_tracking"))
    version = Version(project_id=project.id, name="Tiv 1.0", status="open")
    role = Role(
        name="Tiv Developer",
        permissions=["view_issues", "log_time", "view_time_entries"],
        builtin=0,
        issues_visibility="all",
        time_entries_visibility="all",
    )
    db_session.add_all([version, role])
    await db_session.flush()

    users = {name: await _user(db_session, f"tiv_{name}") for name in ("author", "blind", "assignee")}
    users["admin"] = await _user(db_session, "tiv_admin", admin=True)
    for name in ("author", "blind", "assignee"):
        await _member(db_session, project, users[name], role)

    def issue(**kw) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=users["author"].id,
            fixed_version_id=version.id,
            **kw,
        )

    public = issue(subject="Tiv public issue")
    private = issue(subject="Tiv secret subject", is_private=True, assigned_to_id=users["assignee"].id)
    db_session.add_all([public, private])
    await db_session.flush()

    def entry(issue_id: int | None, hours: Decimal, comments: str) -> TimeEntry:
        return TimeEntryFactory.build(
            project_id=project.id,
            issue_id=issue_id,
            user_id=users["author"].id,
            activity_id=activity.id,
            hours=hours,
            comments=comments,
            spent_on=date(2026, 10, 1),
            is_billable=True,
        )

    entries = {
        "public": entry(public.id, PUBLIC_HOURS, "tiv-public-work"),
        "private": entry(private.id, PRIVATE_HOURS, "tiv-hidden-work"),
        "project": entry(None, PROJECT_HOURS, "tiv-project-work"),
    }
    db_session.add_all(entries.values())
    db_session.add(
        BillingRate(
            user_id=users["author"].id,
            project_id=project.id,
            hourly_rate=Decimal("10.00"),
            currency="USD",
            effective_from=date(2026, 1, 1),
        )
    )
    await db_session.commit()
    clear_role_cache(db_session)
    return {
        "project": project,
        "version": version,
        "activity": activity,
        "users": users,
        "public": public,
        "private": private,
        "entries": entries,
        "role": role,
    }


EVERYTHING = {"tiv-public-work", "tiv-hidden-work", "tiv-project-work"}
WITHOUT_HIDDEN = {"tiv-public-work", "tiv-project-work"}
SEES_HIDDEN = {"author": True, "blind": False, "assignee": True, "admin": True}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("login", list(SEES_HIDDEN))
async def test_service_list_sums_and_billing(db_session: AsyncSession, world: dict, login: str) -> None:
    user, project = world["users"][login], world["project"]
    sees = SEES_HIDDEN[login]
    expected = EVERYTHING if sees else WITHOUT_HIDDEN

    entries, total = await _svc.list_for_project(db_session, project, user)
    assert {e.comments for e in entries} == expected
    assert total == len(expected)

    issue_ids = [world["public"].id, world["private"].id]
    issue_hours = PUBLIC_HOURS + (PRIVATE_HOURS if sees else 0)
    assert await _svc.sum_hours_for_issues(db_session, issue_ids, project, user) == issue_hours
    private_tab = await _svc.list_for_issue(db_session, world["private"].id, project, user)
    assert [e.comments for e in private_tab] == (["tiv-hidden-work"] if sees else [])

    report = await BillingRateService().billable_report(db_session, project, user, *SPAN)
    all_hours = PUBLIC_HOURS + PROJECT_HOURS + (PRIVATE_HOURS if sees else 0)
    assert report["total_hours"] == all_hours
    assert report["total_amount"] == all_hours * Decimal("10.00")
    assert (world["entries"]["private"].id in {e["time_entry_id"] for e in report["entries"]}) == sees


@pytest.mark.parametrize("login", list(SEES_HIDDEN))
async def test_cross_project_clause(db_session: AsyncSession, world: dict, login: str) -> None:
    """The clause behind the dashboard feed drops the hidden entry too."""
    stmt = select(TimeEntry.comments).where(TimeEntry.project_id == world["project"].id)
    clause = await _svc.visible_entries_clause_any(db_session, world["users"][login])
    if clause is not None:
        stmt = stmt.where(clause)
    seen = set((await db_session.execute(stmt)).scalars().all())
    assert seen == (EVERYTHING if SEES_HIDDEN[login] else WITHOUT_HIDDEN)


async def test_author_keeps_their_entry_after_losing_sight_of_the_issue(db_session: AsyncSession, world: dict) -> None:
    """Someone else's private issue: its author logged time, then the issue changed hands."""
    private = world["private"]
    private.author_id = world["users"]["assignee"].id
    await db_session.commit()
    author, project = world["users"]["author"], world["project"]

    entries, _ = await _svc.list_for_project(db_session, project, author)
    assert {e.comments for e in entries} == EVERYTHING
    assert await _svc.get_visible(db_session, world["entries"]["private"].id, author)
    blind_entries, _ = await _svc.list_for_project(db_session, project, world["users"]["blind"])
    assert {e.comments for e in blind_entries} == WITHOUT_HIDDEN


async def test_own_visibility_is_unchanged(db_session: AsyncSession, world: dict) -> None:
    """A role limited to its own time still reads only that, hidden issue or not."""
    world["role"].time_entries_visibility = "own"
    await db_session.commit()
    clear_role_cache(db_session)
    entries, _ = await _svc.list_for_project(db_session, world["project"], world["users"]["author"])
    assert {e.comments for e in entries} == EVERYTHING
    entries, _ = await _svc.list_for_project(db_session, world["project"], world["users"]["blind"])
    assert entries == []


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("login", list(SEES_HIDDEN))
async def test_rest_list_and_single_entry(client: AsyncClient, world: dict, login: str) -> None:
    headers = await _headers(client, f"tiv_{login}")
    resp = await client.get("/api/v1/projects/TIV/time-entries/", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected = EVERYTHING if SEES_HIDDEN[login] else WITHOUT_HIDDEN
    assert {item["comments"] for item in body["items"]} == expected
    assert body["total_count"] == len(expected)

    single = await client.get(f"/api/v1/time-entries/{world['entries']['private'].id}/", headers=headers)
    assert single.status_code == (200 if SEES_HIDDEN[login] else 404)
    project_level = await client.get(f"/api/v1/time-entries/{world['entries']['project'].id}/", headers=headers)
    assert project_level.status_code == 200


async def test_rest_edit_and_delete_of_a_hidden_entry_is_404(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    """Even with manage_time_entries, an entry on an issue the editor cannot see is missing."""
    world["role"].permissions = [*world["role"].permissions, "manage_time_entries"]
    await db_session.commit()
    headers = await _headers(client, "tiv_blind")
    url = f"/api/v1/time-entries/{world['entries']['private'].id}/"
    assert (await client.patch(url, json={"comments": "edited"}, headers=headers)).status_code == 404
    assert (await client.delete(url, headers=headers)).status_code == 404
    entry = await db_session.get(TimeEntry, world["entries"]["private"].id)
    await db_session.refresh(entry)
    assert entry.comments == "tiv-hidden-work"

    visible_url = f"/api/v1/time-entries/{world['entries']['public'].id}/"
    assert (await client.patch(visible_url, json={"comments": "edited"}, headers=headers)).status_code == 200


async def test_api_key_limited_to_another_project_sees_nothing(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    """The author's own entries stay behind their key's project limit."""
    from specivo.services.api_key_service import ApiKeyService

    other = ProjectFactory.build(key="TIVO", identifier="tivo", is_public=False)
    db_session.add(other)
    await db_session.flush()
    await _member(db_session, other, world["users"]["author"], world["role"])
    key, raw = await ApiKeyService().create_key(session=db_session, user_id=world["users"]["author"].id, name="tiv")
    # Stored directly: what create_key keeps when the api_key_scopes feature is on.
    key.scopes = {"projects": [other.key]}
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.get("/api/v1/projects/TIV/time-entries/", headers=headers)).status_code == 404
    for name in ("private", "project"):
        url = f"/api/v1/time-entries/{world['entries'][name].id}/"
        assert (await client.get(url, headers=headers)).status_code == 404


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("login", ["blind", "assignee"])
async def test_web_readers(client: AsyncClient, world: dict, login: str) -> None:
    """Time list, version total and the public issue's sidebar, one regression per page."""
    cookies = await _cookies(client, f"tiv_{login}")
    sees = SEES_HIDDEN[login]

    time_list = await client.get("/projects/TIV/time-entries/", cookies=cookies)
    assert time_list.status_code == 200
    assert ("tiv-hidden-work" in time_list.text) == sees
    assert "tiv-public-work" in time_list.text

    version = await client.get(f"/projects/TIV/versions/{world['version'].id}/", cookies=cookies)
    assert version.status_code == 200
    assert ('<span class="mono">5.0h</span>' if sees else '<span class="mono">1.0h</span>') in version.text

    private_page = await client.get(f"/issue/{world['private'].display_key}/", cookies=cookies)
    assert private_page.status_code == (200 if sees else 404)


@pytest.mark.parametrize("login", ["blind", "assignee"])
async def test_dashboard_agent_feed(client: AsyncClient, db_session: AsyncSession, world: dict, login: str) -> None:
    """Time an agent logged on the private issue is left out of the feed of readers who cannot see it."""
    world["users"]["author"].is_service_account = True
    await db_session.commit()
    resp = await client.get("/", cookies=await _cookies(client, f"tiv_{login}"))
    assert resp.status_code == 200
    assert ("logged 4.0h" in resp.text) == SEES_HIDDEN[login]
    assert "logged 1.0h" in resp.text
