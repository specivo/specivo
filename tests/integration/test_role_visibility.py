"""Role visibility for time entries and users.

``roles.time_entries_visibility`` (``all`` / ``own``) and
``roles.users_visibility`` (``all`` / ``members_of_visible_projects``) across
every surface that reads them: the REST time entry list and single entry, the
web time list, the issue time tab and version sums, the billing report, the
dashboard's cross-project clause, the user autocomplete and watcher lists.

Covers the defaults (unchanged behaviour), the restricted values, the
most-permissive-wins combination across several roles (direct and through a
group), and the Non member fallback.
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
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.time_entry import TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.watcher import Watcher
from specivo.services.permission_service import clear_role_cache, sees_all_users
from specivo.services.time_entry_service import TimeEntryService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = TimeEntryService()

_TIME_PERMS = ["view_issues", "log_time", "view_time_entries"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _user(db: AsyncSession, login: str, *, admin: bool = False) -> User:
    factory = AdminUserFactory if admin else UserFactory
    user = factory.build(login=login, status="active", display_name=login.replace("_", " ").title())
    db.add(user)
    await db.flush()
    return user


async def _role(
    db: AsyncSession,
    name: str,
    *,
    users_visibility: str = "all",
    time_entries_visibility: str = "all",
    permissions: list[str] | None = None,
) -> Role:
    role = Role(
        name=name,
        permissions=permissions if permissions is not None else list(_TIME_PERMS),
        builtin=0,
        issues_visibility="default",
        users_visibility=users_visibility,
        time_entries_visibility=time_entries_visibility,
    )
    db.add(role)
    await db.flush()
    return role


async def _member(
    db: AsyncSession, project: Project, roles: list[Role], *, user: User | None = None, group: UserGroup | None = None
) -> None:
    member = Member(project_id=project.id, user_id=user.id if user else None, group_id=group.id if group else None)
    db.add(member)
    await db.flush()
    for role in roles:
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    clear_role_cache(db)


async def _project(db: AsyncSession, key: str, *, public: bool = False) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=public)
    db.add(project)
    await db.flush()
    return project


async def _entry(
    db: AsyncSession,
    project: Project,
    user: User,
    activity: TimeEntryActivity,
    *,
    comments: str,
    issue: Issue | None = None,
    hours: str = "1.00",
    billable: bool = False,
) -> TimeEntry:
    entry = TimeEntryFactory.build(
        project_id=project.id,
        issue_id=issue.id if issue else None,
        user_id=user.id,
        activity_id=activity.id,
        hours=Decimal(hours),
        comments=comments,
        spent_on=date(2026, 10, 1),
        is_billable=billable,
    )
    db.add(entry)
    await db.flush()
    return entry


async def _non_member_role(db: AsyncSession) -> Role:
    return (await db.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()


@pytest_asyncio.fixture
async def activity(db_session: AsyncSession) -> TimeEntryActivity:
    act = TimeEntryActivityFactory.build(name="Rolevis Development")
    db_session.add(act)
    await db_session.flush()
    return act


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="Rolevis Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Rolevis Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Rolevis Normal")
    db_session.add_all([tracker, priority])
    await db_session.flush()
    return tracker, status, priority


@pytest_asyncio.fixture
async def setup(db_session: AsyncSession, activity: TimeEntryActivity) -> dict:
    """A private project with a viewer, a colleague, and one entry each."""
    project = await _project(db_session, "RVTIME")
    viewer = await _user(db_session, "rv_viewer")
    colleague = await _user(db_session, "rv_colleague")
    mine = await _entry(db_session, project, viewer, activity, comments="logged-by-viewer")
    theirs = await _entry(db_session, project, colleague, activity, comments="logged-by-colleague")
    await db_session.commit()
    return {"project": project, "viewer": viewer, "colleague": colleague, "mine": mine, "theirs": theirs}


async def _api_comments(client: AsyncClient, headers: dict[str, str], key: str) -> set[str]:
    resp = await client.get(f"/api/v1/projects/{key}/time-entries/", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] == len(body["items"])
    return {item["comments"] for item in body["items"]}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


async def test_new_role_and_tracker_defaults_keep_current_behaviour(db_session: AsyncSession) -> None:
    """Rows written without the new columns get the permissive defaults from the database."""
    await db_session.execute(
        Role.__table__.insert().values(name="Rolevis Bare", permissions=[], builtin=0, issues_visibility="default")
    )
    status = StatusFactory.build(name="Rolevis Bare Status")
    db_session.add(status)
    await db_session.flush()
    await db_session.execute(Tracker.__table__.insert().values(name="Rolevis Bare Tracker"))
    role = (await db_session.execute(select(Role).where(Role.name == "Rolevis Bare"))).scalar_one()
    tracker = (await db_session.execute(select(Tracker).where(Tracker.name == "Rolevis Bare Tracker"))).scalar_one()
    assert (role.users_visibility, role.time_entries_visibility) == ("all", "all")
    assert tracker.private_by_default is False

    non_member = await _non_member_role(db_session)
    assert (non_member.users_visibility, non_member.time_entries_visibility) == ("all", "all")


async def test_default_role_sees_every_entry(client: AsyncClient, db_session: AsyncSession, setup: dict) -> None:
    role = await _role(db_session, "Rolevis Default")
    await _member(db_session, setup["project"], [role], user=setup["viewer"])
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    assert await _api_comments(client, headers, "RVTIME") == {"logged-by-viewer", "logged-by-colleague"}
    resp = await client.get(f"/api/v1/time-entries/{setup['theirs'].id}/", headers=headers)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Time entries: "own"
# ---------------------------------------------------------------------------


async def test_own_role_lists_only_own_entries(client: AsyncClient, db_session: AsyncSession, setup: dict) -> None:
    role = await _role(db_session, "Rolevis Own", time_entries_visibility="own")
    await _member(db_session, setup["project"], [role], user=setup["viewer"])
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    assert await _api_comments(client, headers, "RVTIME") == {"logged-by-viewer"}

    # Filtering by another user cannot reveal their entries either.
    resp = await client.get(f"/api/v1/projects/RVTIME/time-entries/?user_id={setup['colleague'].id}", headers=headers)
    assert resp.json()["total_count"] == 0


async def test_own_role_single_entry_is_404_for_others(
    client: AsyncClient, db_session: AsyncSession, setup: dict
) -> None:
    role = await _role(db_session, "Rolevis Own Single", time_entries_visibility="own")
    await _member(db_session, setup["project"], [role], user=setup["viewer"])
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    assert (await client.get(f"/api/v1/time-entries/{setup['mine'].id}/", headers=headers)).status_code == 200
    theirs = f"/api/v1/time-entries/{setup['theirs'].id}/"
    assert (await client.get(theirs, headers=headers)).status_code == 404
    assert (await client.patch(theirs, json={"comments": "x"}, headers=headers)).status_code == 404
    assert (await client.delete(theirs, headers=headers)).status_code == 404


async def test_reachable_entry_of_someone_else_still_cannot_be_deleted(
    client: AsyncClient, db_session: AsyncSession, setup: dict
) -> None:
    """Seeing another user's entry does not make it editable: 403, not 404."""
    role = await _role(db_session, "Rolevis Delete")
    await _member(db_session, setup["project"], [role], user=setup["viewer"])
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    resp = await client.delete(f"/api/v1/time-entries/{setup['theirs'].id}/", headers=headers)
    assert resp.status_code == 403


async def test_single_entry_in_unreachable_project_is_404(
    client: AsyncClient, db_session: AsyncSession, setup: dict
) -> None:
    await _user(db_session, "rv_outsider")
    await db_session.commit()
    headers = await _login(client, "rv_outsider")
    assert (await client.get(f"/api/v1/time-entries/{setup['theirs'].id}/", headers=headers)).status_code == 404


async def test_own_role_on_the_web_time_list(client: AsyncClient, db_session: AsyncSession, setup: dict) -> None:
    role = await _role(db_session, "Rolevis Own Web", time_entries_visibility="own")
    await _member(db_session, setup["project"], [role], user=setup["viewer"])
    await db_session.commit()

    token = (await _login(client, "rv_viewer"))["Authorization"].removeprefix("Bearer ")
    resp = await client.get("/projects/RVTIME/time-entries/", cookies={"access_token": token})
    assert resp.status_code == 200
    assert "logged-by-viewer" in resp.text
    assert "logged-by-colleague" not in resp.text


async def test_own_role_on_issue_tab_and_version_sum(
    db_session: AsyncSession, setup: dict, activity: TimeEntryActivity, lookups: tuple
) -> None:
    tracker, status, priority = lookups
    project, viewer, colleague = setup["project"], setup["viewer"], setup["colleague"]
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=colleague.id,
    )
    db_session.add(issue)
    await db_session.flush()
    await _entry(db_session, project, viewer, activity, comments="issue-viewer", issue=issue, hours="2.00")
    await _entry(db_session, project, colleague, activity, comments="issue-colleague", issue=issue, hours="3.00")
    role = await _role(db_session, "Rolevis Own Issue", time_entries_visibility="own")
    await _member(db_session, project, [role], user=viewer)

    entries = await _svc.list_for_issue(db_session, issue.id, project, viewer)
    assert [e.comments for e in entries] == ["issue-viewer"]
    assert await _svc.sum_hours_for_issues(db_session, [issue.id], project, viewer) == Decimal("2.00")
    assert await _svc.sum_hours_for_issues(db_session, [issue.id], project, colleague) == Decimal("5.00")


async def test_own_role_on_billing_report(db_session: AsyncSession, setup: dict, activity: TimeEntryActivity) -> None:
    from specivo.services.billing_rate_service import BillingRateService

    project, viewer, colleague = setup["project"], setup["viewer"], setup["colleague"]
    await _entry(db_session, project, viewer, activity, comments="bill-viewer", billable=True)
    await _entry(db_session, project, colleague, activity, comments="bill-colleague", billable=True)
    db_session.add(BillingRate(project_id=project.id, hourly_rate=Decimal("10"), effective_from=date(2026, 1, 1)))
    role = await _role(db_session, "Rolevis Own Billing", time_entries_visibility="own")
    await _member(db_session, project, [role], user=viewer)

    service = BillingRateService()
    span = (date(2026, 1, 1), date(2026, 12, 31))
    report = await service.billable_report(db_session, project, viewer, *span)
    assert {e["user_id"] for e in report["entries"]} == {viewer.id}
    report = await service.billable_report(db_session, project, colleague, *span)
    assert {e["user_id"] for e in report["entries"]} == {viewer.id, colleague.id}


# ---------------------------------------------------------------------------
# Time entries: combining roles
# ---------------------------------------------------------------------------


async def test_most_permissive_role_wins(client: AsyncClient, db_session: AsyncSession, setup: dict) -> None:
    own = await _role(db_session, "Rolevis Combo Own", time_entries_visibility="own")
    everything = await _role(db_session, "Rolevis Combo All")
    await _member(db_session, setup["project"], [own, everything], user=setup["viewer"])
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    assert await _api_comments(client, headers, "RVTIME") == {"logged-by-viewer", "logged-by-colleague"}


async def test_group_role_widens_a_restricted_direct_role(
    client: AsyncClient, db_session: AsyncSession, setup: dict
) -> None:
    own = await _role(db_session, "Rolevis Group Own", time_entries_visibility="own")
    everything = await _role(db_session, "Rolevis Group All")
    await _member(db_session, setup["project"], [own], user=setup["viewer"])
    group = UserGroup(name="Rolevis Reviewers")
    db_session.add(group)
    await db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=setup["viewer"].id))
    await _member(db_session, setup["project"], [everything], group=group)
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    assert await _api_comments(client, headers, "RVTIME") == {"logged-by-viewer", "logged-by-colleague"}


async def test_all_roles_own_restricts(client: AsyncClient, db_session: AsyncSession, setup: dict) -> None:
    first = await _role(db_session, "Rolevis Both Own 1", time_entries_visibility="own")
    second = await _role(db_session, "Rolevis Both Own 2", time_entries_visibility="own")
    await _member(db_session, setup["project"], [first, second], user=setup["viewer"])
    await db_session.commit()

    headers = await _login(client, "rv_viewer")
    assert await _api_comments(client, headers, "RVTIME") == {"logged-by-viewer"}


async def test_non_member_role_restricts_public_project(
    client: AsyncClient, db_session: AsyncSession, activity: TimeEntryActivity
) -> None:
    project = await _project(db_session, "RVPUB", public=True)
    viewer = await _user(db_session, "rv_public_viewer")
    colleague = await _user(db_session, "rv_public_colleague")
    await _entry(db_session, project, viewer, activity, comments="public-viewer")
    await _entry(db_session, project, colleague, activity, comments="public-colleague")
    non_member = await _non_member_role(db_session)
    non_member.time_entries_visibility = "own"
    await db_session.commit()

    headers = await _login(client, "rv_public_viewer")
    assert await _api_comments(client, headers, "RVPUB") == {"public-viewer"}


async def test_admin_sees_every_entry_whatever_the_roles(
    client: AsyncClient, db_session: AsyncSession, setup: dict
) -> None:
    await _user(db_session, "rv_admin", admin=True)
    await db_session.commit()
    headers = await _login(client, "rv_admin")
    assert await _api_comments(client, headers, "RVTIME") == {"logged-by-viewer", "logged-by-colleague"}


# ---------------------------------------------------------------------------
# Time entries: the cross-project clause (dashboard)
# ---------------------------------------------------------------------------


async def test_cross_project_clause_matches_the_per_project_rule(
    db_session: AsyncSession, activity: TimeEntryActivity
) -> None:
    viewer = await _user(db_session, "rv_cross_viewer")
    colleague = await _user(db_session, "rv_cross_colleague")
    restricted = await _project(db_session, "RVCRO")
    open_project = await _project(db_session, "RVCRA")
    combined = await _project(db_session, "RVCRC")
    public = await _project(db_session, "RVCRP", public=True)
    own = await _role(db_session, "Rolevis Cross Own", time_entries_visibility="own")
    everything = await _role(db_session, "Rolevis Cross All")
    await _member(db_session, restricted, [own], user=viewer)
    await _member(db_session, open_project, [everything], user=viewer)
    await _member(db_session, combined, [own, everything], user=viewer)
    for project in (restricted, open_project, combined, public):
        await _entry(db_session, project, viewer, activity, comments=f"{project.key}-viewer")
        await _entry(db_session, project, colleague, activity, comments=f"{project.key}-colleague")

    async def visible() -> set[str]:
        stmt = select(TimeEntry.comments).where(
            TimeEntry.project_id.in_([restricted.id, open_project.id, combined.id, public.id])
        )
        clause = await _svc.visible_entries_clause_any(db_session, viewer)
        if clause is not None:
            stmt = stmt.where(clause)
        return set((await db_session.execute(stmt)).scalars().all())

    expected_always = {
        "RVCRO-viewer",
        "RVCRA-viewer",
        "RVCRA-colleague",
        "RVCRC-viewer",
        "RVCRC-colleague",
        "RVCRP-viewer",
    }
    assert await visible() == expected_always | {"RVCRP-colleague"}

    non_member = await _non_member_role(db_session)
    non_member.time_entries_visibility = "own"
    await db_session.flush()
    clear_role_cache(db_session)
    assert await visible() == expected_always

    # Each project agrees with the per-project rule.
    for project in (restricted, open_project, combined, public):
        per_project = await _svc.visible_entries_clause(db_session, viewer, project)
        stmt = select(TimeEntry.comments).where(TimeEntry.project_id == project.id)
        if per_project is not None:
            stmt = stmt.where(per_project)
        assert set((await db_session.execute(stmt)).scalars().all()) <= expected_always


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def people(db_session: AsyncSession) -> dict:
    """A viewer on a private project, a teammate there, and a stranger with no shared project."""
    project = await _project(db_session, "RVUSR")
    elsewhere = await _project(db_session, "RVUSX")
    viewer = await _user(db_session, "rvu_viewer")
    teammate = await _user(db_session, "rvu_teammate")
    stranger = await _user(db_session, "rvu_stranger")
    filler = await _role(db_session, "Rolevis Users Filler")
    await _member(db_session, project, [filler], user=teammate)
    await _member(db_session, elsewhere, [filler], user=stranger)
    await db_session.commit()
    return {"project": project, "viewer": viewer, "teammate": teammate, "stranger": stranger}


async def _autocomplete(client: AsyncClient, headers: dict[str, str]) -> set[str]:
    resp = await client.get("/api/v1/users/autocomplete/?q=rvu_", headers=headers)
    assert resp.status_code == 200, resp.text
    return {u["login"] for u in resp.json()}


async def test_users_default_lists_everyone(client: AsyncClient, db_session: AsyncSession, people: dict) -> None:
    role = await _role(db_session, "Rolevis Users All")
    await _member(db_session, people["project"], [role], user=people["viewer"])
    await db_session.commit()

    headers = await _login(client, "rvu_viewer")
    assert await _autocomplete(client, headers) == {"rvu_viewer", "rvu_teammate", "rvu_stranger"}


async def test_users_members_of_visible_projects(client: AsyncClient, db_session: AsyncSession, people: dict) -> None:
    role = await _role(db_session, "Rolevis Users Members", users_visibility="members_of_visible_projects")
    await _member(db_session, people["project"], [role], user=people["viewer"])
    await db_session.commit()

    headers = await _login(client, "rvu_viewer")
    assert await _autocomplete(client, headers) == {"rvu_viewer", "rvu_teammate"}


async def test_users_members_reached_through_a_group_or_a_public_project(
    client: AsyncClient, db_session: AsyncSession, people: dict
) -> None:
    role = await _role(db_session, "Rolevis Users Members 2", users_visibility="members_of_visible_projects")
    await _member(db_session, people["project"], [role], user=people["viewer"])
    via_group = await _user(db_session, "rvu_group_member")
    group = UserGroup(name="Rolevis Users Group")
    db_session.add(group)
    await db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=via_group.id))
    await _member(db_session, people["project"], [role], group=group)
    public = await _project(db_session, "RVUSP", public=True)
    on_public = await _user(db_session, "rvu_public_member")
    await _member(db_session, public, [role], user=on_public)
    await db_session.commit()

    headers = await _login(client, "rvu_viewer")
    assert await _autocomplete(client, headers) == {
        "rvu_viewer",
        "rvu_teammate",
        "rvu_group_member",
        "rvu_public_member",
    }


async def test_users_any_unrestricted_membership_role_wins(
    client: AsyncClient, db_session: AsyncSession, people: dict
) -> None:
    restricted = await _role(db_session, "Rolevis Users R", users_visibility="members_of_visible_projects")
    unrestricted = await _role(db_session, "Rolevis Users U")
    other = await _project(db_session, "RVUSO")
    await _member(db_session, people["project"], [restricted], user=people["viewer"])
    await _member(db_session, other, [unrestricted], user=people["viewer"])
    await db_session.commit()

    headers = await _login(client, "rvu_viewer")
    assert await _autocomplete(client, headers) == {"rvu_viewer", "rvu_teammate", "rvu_stranger"}


async def test_users_without_membership_follow_the_non_member_role(
    client: AsyncClient, db_session: AsyncSession, people: dict
) -> None:
    non_member = await _non_member_role(db_session)
    headers = await _login(client, "rvu_viewer")
    assert await _autocomplete(client, headers) == {"rvu_viewer", "rvu_teammate", "rvu_stranger"}

    non_member.users_visibility = "members_of_visible_projects"
    await db_session.commit()
    assert await sees_all_users(db_session, people["viewer"]) is False
    assert await _autocomplete(client, headers) == {"rvu_viewer"}


async def test_users_role_less_membership_keeps_the_default(
    client: AsyncClient, db_session: AsyncSession, people: dict
) -> None:
    """A membership carrying no role does not count as a restriction."""
    await _member(db_session, people["project"], [], user=people["viewer"])
    await db_session.commit()
    headers = await _login(client, "rvu_viewer")
    assert await _autocomplete(client, headers) == {"rvu_viewer", "rvu_teammate", "rvu_stranger"}


async def test_watcher_list_hides_users_the_viewer_cannot_list(
    client: AsyncClient, db_session: AsyncSession, people: dict, lookups: tuple
) -> None:
    tracker, status, priority = lookups
    project = people["project"]
    role = await _role(
        db_session,
        "Rolevis Users Watchers",
        users_visibility="members_of_visible_projects",
        permissions=["view_issues"],
    )
    await _member(db_session, project, [role], user=people["viewer"])
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=people["teammate"].id,
    )
    db_session.add(issue)
    await db_session.flush()
    for watcher in (people["viewer"], people["teammate"], people["stranger"]):
        db_session.add(Watcher(issue_id=issue.id, user_id=watcher.id))
    await db_session.commit()

    headers = await _login(client, "rvu_viewer")
    resp = await client.get(f"/api/v1/issues/{issue.display_key}/watchers/", headers=headers)
    assert resp.status_code == 200, resp.text
    assert {w["login"] for w in resp.json()} == {"rvu_viewer", "rvu_teammate"}
