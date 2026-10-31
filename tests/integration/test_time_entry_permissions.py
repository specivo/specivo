"""Time tracking permissions: ``view_time_entries``, ``log_time`` and ``manage_time_entries``.

- ``view_time_entries`` is needed to read any time entry, the user's own
  included (as in Redmine): the REST list (403) and single entry (404), the web
  time list, the issue time tab and sums, version sums, the billing report and
  the cross-project clause behind the dashboard. Only the roles granting it
  count towards ``time_entries_visibility``.
- ``log_time`` is needed to create an entry (REST, web form) and to start or
  stop a timer; stopping re-checks, so a timer whose permission was withdrawn
  is discarded without writing an entry.
- ``manage_time_entries`` edits or deletes anyone's entry; without it only
  one's own, while holding ``log_time``.

404 for a project, issue or entry the user cannot see; 403 for one they can
see but lack the permission for. MCP ``log_time`` is covered in
``test_mcp_permissions.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.services.permission_service import best_time_entries_visibility, clear_role_cache
from specivo.services.time_entry_service import TimeEntryService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import ActiveTimerFactory, TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = TimeEntryService()

VIEW = "view_time_entries"
LOG = "log_time"
MANAGE = "manage_time_entries"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _user(db: AsyncSession, login: str, *, admin: bool = False) -> User:
    factory = AdminUserFactory if admin else UserFactory
    user = factory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    return user


async def _role(db: AsyncSession, name: str, permissions: list[str], *, time_entries_visibility: str = "all") -> Role:
    role = Role(
        name=name,
        permissions=["view_issues", *permissions],
        builtin=0,
        issues_visibility="default",
        time_entries_visibility=time_entries_visibility,
    )
    db.add(role)
    await db.flush()
    return role


async def _member(db: AsyncSession, project: Project, user: User, roles: list[Role]) -> Member:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    for role in roles:
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    clear_role_cache(db)
    return member


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _cookies(client: AsyncClient, login: str) -> dict[str, str]:
    return {"access_token": (await _headers(client, login))["Authorization"].removeprefix("Bearer ")}


async def _entry(db: AsyncSession, project: Project, user: User, activity: TimeEntryActivity, **kw) -> TimeEntry:
    entry = TimeEntryFactory.build(
        project_id=project.id,
        user_id=user.id,
        activity_id=activity.id,
        hours=Decimal(kw.pop("hours", "1.00")),
        spent_on=date(2026, 10, 1),
        **kw,
    )
    db.add(entry)
    await db.flush()
    return entry


async def _issue(db: AsyncSession, project: Project, lookups: dict, author: User, **kw) -> Issue:
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        tracker_id=lookups["tracker"].id,
        status_id=lookups["status"].id,
        priority_id=lookups["priority"].id,
        author_id=author.id,
        **kw,
    )
    db.add(issue)
    await db.flush()
    return issue


async def _entry_count(db: AsyncSession, user: User) -> int:
    return (
        await db.execute(select(func.count()).select_from(TimeEntry).where(TimeEntry.user_id == user.id))
    ).scalar_one()


async def _timer(db: AsyncSession, user: User) -> ActiveTimer | None:
    return (await db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id))).scalar_one_or_none()


def _entry_body(activity: TimeEntryActivity, **extra) -> dict:
    return {"activity_id": activity.id, "hours": "1.50", "spent_on": "2026-10-02", **extra}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def activity(db_session: AsyncSession) -> TimeEntryActivity:
    act = TimeEntryActivityFactory.build(name="Ttp Development", is_default=False)
    db_session.add(act)
    await db_session.flush()
    return act


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> dict:
    status = StatusFactory.build(name="Ttp Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Ttp Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Ttp Normal")
    db_session.add_all([tracker, priority])
    await db_session.flush()
    return {"status": status, "tracker": tracker, "priority": priority}


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, activity: TimeEntryActivity, lookups: dict) -> dict:
    """A private project with one member per permission shape, an issue, and entries.

    - ``full``: view + log (the seeded Developer shape)
    - ``viewer``: view only
    - ``logger``: log only (the seeded Agent shape)
    - ``reader``: neither (the seeded Reporter shape)
    - ``manager``: view + log + manage
    - ``outsider``: no membership; the project is private
    - ``admin``: no membership, administrator
    """
    project = ProjectFactory.build(key="TTPRV", identifier="ttprv", is_public=False)
    other = ProjectFactory.build(key="TTPOTH", identifier="ttpoth", is_public=False)
    db_session.add_all([project, other])
    await db_session.flush()

    roles = {
        "full": await _role(db_session, "Ttp Full", [VIEW, LOG]),
        "viewer": await _role(db_session, "Ttp Viewer", [VIEW]),
        "logger": await _role(db_session, "Ttp Logger", [LOG]),
        "reader": await _role(db_session, "Ttp Reader", []),
        "manager": await _role(db_session, "Ttp Manager", [VIEW, LOG, MANAGE]),
    }
    users = {name: await _user(db_session, f"ttp_{name}") for name in roles}
    for name, role in roles.items():
        await _member(db_session, project, users[name], [role])
    # Everyone but the outsider may log time in the other project too.
    for name in roles:
        await _member(db_session, other, users[name], [roles["full"]])
    users["outsider"] = await _user(db_session, "ttp_outsider")
    users["admin"] = await _user(db_session, "ttp_admin", admin=True)

    issue = await _issue(db_session, project, lookups, users["full"])
    other_issue = await _issue(db_session, other, lookups, users["full"])
    entries = {
        name: await _entry(db_session, project, users[name], activity, issue_id=issue.id, comments=f"by-{name}")
        for name in ("full", "viewer", "logger", "reader", "manager")
    }
    await db_session.commit()
    return {
        "project": project,
        "other": other,
        "roles": roles,
        "users": users,
        "issue": issue,
        "other_issue": other_issue,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# best_time_entries_visibility
# ---------------------------------------------------------------------------


def test_best_visibility_without_any_granting_role_is_none() -> None:
    assert best_time_entries_visibility([]) is None
    assert best_time_entries_visibility(["own"]) == "own"
    assert best_time_entries_visibility(["own", "all"]) == "all"


async def test_only_roles_granting_view_count_towards_visibility(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    """An 'own' role with view_time_entries plus an 'all' role without it reads only its own entries."""
    project = world["project"]
    user = await _user(db_session, "ttp_multi")
    own = await _role(db_session, "Ttp Own Viewer", [VIEW], time_entries_visibility="own")
    wide = await _role(db_session, "Ttp Wide No View", [LOG], time_entries_visibility="all")
    await _member(db_session, project, user, [own, wide])
    await _entry(db_session, project, user, activity, comments="by-multi")
    await db_session.commit()

    resp = await client.get("/api/v1/projects/TTPRV/time-entries/", headers=await _headers(client, "ttp_multi"))
    assert resp.status_code == 200, resp.text
    assert {item["comments"] for item in resp.json()["items"]} == {"by-multi"}


async def test_a_second_role_with_view_widens_visibility(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    """Multi-role: the reader role grants nothing, a second role granting view_time_entries opens the list."""
    user = world["users"]["reader"]
    member = (
        await db_session.execute(
            select(Member).where(Member.project_id == world["project"].id, Member.user_id == user.id)
        )
    ).scalar_one()
    db_session.add(MemberRole(member_id=member.id, role_id=world["roles"]["viewer"].id))
    await db_session.commit()

    resp = await client.get("/api/v1/projects/TTPRV/time-entries/", headers=await _headers(client, "ttp_reader"))
    assert resp.status_code == 200
    assert resp.json()["total_count"] == 5


# ---------------------------------------------------------------------------
# Reading: REST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("login", "expected"),
    [
        ("full", 200),
        ("viewer", 200),
        ("manager", 200),
        ("admin", 200),
        ("logger", 403),
        ("reader", 403),
        ("outsider", 404),
    ],
)
async def test_rest_list_requires_view_time_entries(
    client: AsyncClient, world: dict, login: str, expected: int
) -> None:
    resp = await client.get("/api/v1/projects/TTPRV/time-entries/", headers=await _headers(client, f"ttp_{login}"))
    assert resp.status_code == expected, resp.text
    if expected == 200:
        assert resp.json()["total_count"] == 5


@pytest.mark.parametrize(("login", "expected"), [("viewer", 200), ("admin", 200), ("logger", 404), ("reader", 404)])
async def test_rest_single_entry_requires_view_time_entries_even_for_own(
    client: AsyncClient, world: dict, login: str, expected: int
) -> None:
    """Without view_time_entries an entry is invisible (404), the user's own included."""
    owner = login if login != "admin" else "full"
    entry = world["entries"][owner]
    resp = await client.get(f"/api/v1/time-entries/{entry.id}/", headers=await _headers(client, f"ttp_{login}"))
    assert resp.status_code == expected


async def test_billing_report_endpoint_requires_view_time_entries(db_session: AsyncSession, world: dict) -> None:
    """The billing report handler (its router is not mounted by core) refuses without view_time_entries."""
    from specivo.api.v1.agent_costs import get_billing_report
    from specivo.core.exceptions import NotFoundError, PermissionDeniedError

    span = {"date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31)}
    for name in ("reader", "logger"):
        with pytest.raises(PermissionDeniedError):
            await get_billing_report("TTPRV", None, current_user=world["users"][name], db=db_session, **span)
    with pytest.raises(NotFoundError):
        await get_billing_report("TTPRV", None, current_user=world["users"]["outsider"], db=db_session, **span)
    report = await get_billing_report("TTPRV", None, current_user=world["users"]["viewer"], db=db_session, **span)
    assert report.entries == []  # no billing rate, so nothing is priced


# ---------------------------------------------------------------------------
# Reading: services behind the web pages
# ---------------------------------------------------------------------------


async def test_issue_and_version_sums_and_billing_hide_everything_without_view(
    db_session: AsyncSession, world: dict
) -> None:
    from specivo.services.billing_rate_service import BillingRateService

    project, issue = world["project"], world["issue"]
    for name in ("logger", "reader"):
        user = world["users"][name]
        assert await _svc.list_for_issue(db_session, issue.id, project, user) == []
        assert await _svc.sum_hours_for_issues(db_session, [issue.id], project, user) == Decimal(0)
        report = await BillingRateService().billable_report(
            db_session, project, user, date(2026, 1, 1), date(2026, 12, 31)
        )
        assert report["entries"] == []
    viewer = world["users"]["viewer"]
    assert len(await _svc.list_for_issue(db_session, issue.id, project, viewer)) == 5
    assert await _svc.sum_hours_for_issues(db_session, [issue.id], project, viewer) == Decimal("5.00")


async def test_cross_project_clause_needs_view_time_entries(db_session: AsyncSession, world: dict) -> None:
    """The dashboard clause shows nothing in a project where no role grants view_time_entries."""

    async def visible(user: User) -> set[str]:
        stmt = select(TimeEntry.comments).where(TimeEntry.project_id == world["project"].id)
        clause = await _svc.visible_entries_clause_any(db_session, user)
        if clause is not None:
            stmt = stmt.where(clause)
        return set((await db_session.execute(stmt)).scalars().all())

    everything = {"by-full", "by-viewer", "by-logger", "by-reader", "by-manager"}
    assert await visible(world["users"]["logger"]) == set()
    assert await visible(world["users"]["reader"]) == set()
    assert await visible(world["users"]["outsider"]) == set()
    assert await visible(world["users"]["viewer"]) == everything
    assert await visible(world["users"]["admin"]) == everything


# ---------------------------------------------------------------------------
# Reading: web
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("login", "expected"), [("viewer", 200), ("logger", 403), ("reader", 403), ("outsider", 404)])
async def test_web_time_list_requires_view_time_entries(
    client: AsyncClient, world: dict, login: str, expected: int
) -> None:
    resp = await client.get("/projects/TTPRV/time-entries/", cookies=await _cookies(client, f"ttp_{login}"))
    assert resp.status_code == expected


async def test_web_time_list_controls_follow_permissions(client: AsyncClient, world: dict) -> None:
    """The Log Time button needs log_time; edit buttons appear only on editable rows."""
    viewer = await client.get("/projects/TTPRV/time-entries/", cookies=await _cookies(client, "ttp_viewer"))
    assert "/time-entries/new/" not in viewer.text
    assert 'class="action-btn"' not in viewer.text

    full = await client.get("/projects/TTPRV/time-entries/", cookies=await _cookies(client, "ttp_full"))
    assert "/time-entries/new/" in full.text
    assert full.text.count('class="action-btn"') == 1  # only their own entry

    manager = await client.get("/projects/TTPRV/time-entries/", cookies=await _cookies(client, "ttp_manager"))
    assert manager.text.count('class="action-btn"') == 5


@pytest.mark.parametrize(("login", "expected"), [("logger", 200), ("full", 200), ("viewer", 403), ("reader", 403)])
async def test_web_log_time_form_requires_log_time(client: AsyncClient, world: dict, login: str, expected: int) -> None:
    resp = await client.get("/projects/TTPRV/time-entries/new/", cookies=await _cookies(client, f"ttp_{login}"))
    assert resp.status_code == expected


async def test_issue_time_tab_follows_permissions(client: AsyncClient, world: dict) -> None:
    url = f"/issue/{world['issue'].display_key}/"

    full = (await client.get(url, cookies=await _cookies(client, "ttp_full"))).text
    assert "timeLogForm(" in full and "by-viewer" in full and "Time Logged" in full

    viewer = (await client.get(url, cookies=await _cookies(client, "ttp_viewer"))).text
    assert "timeLogForm(" not in viewer and "by-full" in viewer and "Time Logged" in viewer

    logger = (await client.get(url, cookies=await _cookies(client, "ttp_logger"))).text
    assert "timeLogForm(" in logger
    assert "by-full" not in logger and "by-logger" not in logger and "Time Logged" not in logger

    reader = (await client.get(url, cookies=await _cookies(client, "ttp_reader"))).text
    assert "tab === 'time'" not in reader and "timeLogForm(" not in reader and "by-full" not in reader


async def test_version_page_hides_time_logged_without_view(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    from specivo.models.version import Version

    version = Version(project_id=world["project"].id, name="Ttp 1.0", status="open")
    db_session.add(version)
    await db_session.flush()
    world["issue"].fixed_version_id = version.id
    await db_session.commit()
    url = f"/projects/TTPRV/versions/{version.id}/"

    assert "Time logged" in (await client.get(url, cookies=await _cookies(client, "ttp_viewer"))).text
    resp = await client.get(url, cookies=await _cookies(client, "ttp_reader"))
    assert resp.status_code == 200
    assert "Time logged" not in resp.text


# ---------------------------------------------------------------------------
# Non member of a public project
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def public_world(db_session: AsyncSession, activity: TimeEntryActivity) -> dict:
    project = ProjectFactory.build(key="TTPPUB", identifier="ttppub", is_public=True)
    db_session.add(project)
    await db_session.flush()
    stranger = await _user(db_session, "ttp_stranger")
    author = await _user(db_session, "ttp_pub_author")
    await _entry(db_session, project, stranger, activity, comments="pub-stranger")
    await _entry(db_session, project, author, activity, comments="pub-author")
    await db_session.commit()
    non_member = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()
    return {"project": project, "stranger": stranger, "non_member": non_member}


async def test_non_member_follows_the_non_member_role(
    client: AsyncClient, db_session: AsyncSession, public_world: dict, activity: TimeEntryActivity
) -> None:
    """The seeded Non member role (view_issues only) reads and logs nothing; granting permissions opens both."""
    headers = await _headers(client, "ttp_stranger")
    url = "/api/v1/projects/TTPPUB/time-entries/"
    assert (await client.get(url, headers=headers)).status_code == 403
    assert (await client.post(url, json=_entry_body(activity), headers=headers)).status_code == 403
    timer = await client.post("/api/v1/timer/start/", json={"project_id": public_world["project"].id}, headers=headers)
    assert timer.status_code == 403

    non_member = public_world["non_member"]
    non_member.permissions = [*non_member.permissions, VIEW, LOG]
    non_member.time_entries_visibility = "own"
    await db_session.commit()

    listed = await client.get(url, headers=headers)
    assert listed.status_code == 200
    assert {item["comments"] for item in listed.json()["items"]} == {"pub-stranger"}
    assert (await client.post(url, json=_entry_body(activity), headers=headers)).status_code == 201


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("login", "expected"),
    [("full", 201), ("logger", 201), ("admin", 201), ("viewer", 403), ("reader", 403), ("outsider", 404)],
)
async def test_rest_create_requires_log_time(
    client: AsyncClient, world: dict, activity: TimeEntryActivity, login: str, expected: int
) -> None:
    resp = await client.post(
        "/api/v1/projects/TTPRV/time-entries/",
        json=_entry_body(activity, issue_id=world["issue"].id),
        headers=await _headers(client, f"ttp_{login}"),
    )
    assert resp.status_code == expected, resp.text


async def test_rest_create_rejects_an_issue_of_another_project(
    client: AsyncClient, world: dict, activity: TimeEntryActivity
) -> None:
    resp = await client.post(
        "/api/v1/projects/TTPRV/time-entries/",
        json=_entry_body(activity, issue_id=world["other_issue"].id),
        headers=await _headers(client, "ttp_full"),
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "issue_id"


async def test_rest_create_on_an_invisible_issue_is_404(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity, lookups: dict
) -> None:
    """A private issue of someone else is invisible to an 'own issues' role: 404, nothing written."""
    project, user = world["project"], await _user(db_session, "ttp_own_issues")
    role = Role(name="Ttp Own Issues", permissions=["view_issues", LOG], builtin=0, issues_visibility="own")
    db_session.add(role)
    await db_session.flush()
    await _member(db_session, project, user, [role])
    hidden = await _issue(db_session, project, lookups, world["users"]["full"])
    await db_session.commit()

    resp = await client.post(
        "/api/v1/projects/TTPRV/time-entries/",
        json=_entry_body(activity, issue_id=hidden.id),
        headers=await _headers(client, "ttp_own_issues"),
    )
    assert resp.status_code == 404
    assert await _entry_count(db_session, user) == 0
    resp = await client.post(
        "/api/v1/timer/start/",
        json={"project_id": project.id, "issue_id": hidden.id},
        headers=await _headers(client, "ttp_own_issues"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Editing and deleting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("login", "owner", "expected"),
    [
        ("full", "full", 200),  # own entry with log_time
        ("full", "viewer", 403),  # someone else's, no manage_time_entries
        ("viewer", "viewer", 403),  # own entry but no log_time
        ("manager", "full", 200),  # manage_time_entries edits anyone's
        ("admin", "reader", 200),
        ("logger", "logger", 404),  # cannot see even their own entry
        ("reader", "full", 404),
        ("outsider", "full", 404),
    ],
)
async def test_edit_follows_manage_time_entries(
    client: AsyncClient, world: dict, login: str, owner: str, expected: int
) -> None:
    entry = world["entries"][owner]
    resp = await client.patch(
        f"/api/v1/time-entries/{entry.id}/", json={"comments": "edited"}, headers=await _headers(client, f"ttp_{login}")
    )
    assert resp.status_code == expected, resp.text


@pytest.mark.parametrize(
    ("login", "owner", "expected"),
    [
        ("full", "full", 204),
        ("full", "manager", 403),
        ("viewer", "viewer", 403),
        ("manager", "viewer", 204),
        ("admin", "logger", 204),
        ("logger", "logger", 404),
    ],
)
async def test_delete_follows_manage_time_entries(
    client: AsyncClient, world: dict, login: str, owner: str, expected: int
) -> None:
    entry = world["entries"][owner]
    resp = await client.delete(f"/api/v1/time-entries/{entry.id}/", headers=await _headers(client, f"ttp_{login}"))
    assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("login", "expected"),
    [("full", 201), ("logger", 201), ("admin", 201), ("viewer", 403), ("reader", 403), ("outsider", 404)],
)
async def test_timer_start_requires_log_time(client: AsyncClient, world: dict, login: str, expected: int) -> None:
    resp = await client.post(
        "/api/v1/timer/start/",
        json={"project_id": world["project"].id, "issue_id": world["issue"].id},
        headers=await _headers(client, f"ttp_{login}"),
    )
    assert resp.status_code == expected, resp.text


async def test_timer_start_rejects_unknown_project_and_foreign_issue(client: AsyncClient, world: dict) -> None:
    headers = await _headers(client, "ttp_full")
    assert (await client.post("/api/v1/timer/start/", json={"project_id": 987654}, headers=headers)).status_code == 404
    resp = await client.post(
        "/api/v1/timer/start/",
        json={"project_id": world["project"].id, "issue_id": world["other_issue"].id},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_timer_stop_rechecks_and_discards_when_permission_was_withdrawn(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    """The race: log_time is withdrawn while the timer runs; stopping writes nothing and drops the timer."""
    user = world["users"]["logger"]
    headers = await _headers(client, "ttp_logger")
    started = await client.post(
        "/api/v1/timer/start/",
        json={"project_id": world["project"].id, "issue_id": world["issue"].id},
        headers=headers,
    )
    assert started.status_code == 201
    before = await _entry_count(db_session, user)

    role = world["roles"]["logger"]
    role.permissions = ["view_issues"]
    await db_session.commit()

    resp = await client.post("/api/v1/timer/stop/", json={"activity_id": activity.id}, headers=headers)
    assert resp.status_code == 403
    assert "discarded" in resp.json()["errors"][0]["message"]
    assert await _entry_count(db_session, user) == before
    assert await _timer(db_session, user) is None


async def test_timer_stop_after_losing_the_membership_is_404(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    user = world["users"]["full"]
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id, project_id=world["project"].id, started_at=datetime.now(UTC) - timedelta(hours=1)
        )
    )
    member = (
        await db_session.execute(
            select(Member).where(Member.project_id == world["project"].id, Member.user_id == user.id)
        )
    ).scalar_one()
    await db_session.delete(member)
    await db_session.commit()
    before = await _entry_count(db_session, user)

    resp = await client.post(
        "/api/v1/timer/stop/", json={"activity_id": activity.id}, headers=await _headers(client, "ttp_full")
    )
    assert resp.status_code == 404
    assert await _entry_count(db_session, user) == before
    assert await _timer(db_session, user) is None


async def test_timer_stop_still_logs_while_permitted(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    user = world["users"]["logger"]
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=world["project"].id,
            issue_id=world["issue"].id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/timer/stop/", json={"activity_id": activity.id}, headers=await _headers(client, "ttp_logger")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["issue_id"] == world["issue"].id


async def test_starting_a_timer_discards_a_running_one_that_can_no_longer_be_logged(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    """Auto-stop of the previous timer re-checks too: no entry in a project the user lost log_time in."""
    user = world["users"]["logger"]
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id, project_id=world["project"].id, started_at=datetime.now(UTC) - timedelta(hours=1)
        )
    )
    world["roles"]["logger"].permissions = ["view_issues"]
    await db_session.commit()
    before = await _entry_count(db_session, user)

    resp = await client.post(
        "/api/v1/timer/start/", json={"project_id": world["other"].id}, headers=await _headers(client, "ttp_logger")
    )
    assert resp.status_code == 201, resp.text
    assert await _entry_count(db_session, user) == before
    timer = await _timer(db_session, user)
    assert timer is not None and timer.project_id == world["other"].id


async def test_timer_start_reports_no_replaced_timer_when_none_ran(client: AsyncClient, world: dict) -> None:
    resp = await client.post(
        "/api/v1/timer/start/", json={"project_id": world["other"].id}, headers=await _headers(client, "ttp_logger")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["replaced"] is None


async def test_timer_start_reports_a_replaced_timer_that_was_logged(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    user = world["users"]["logger"]
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=world["project"].id,
            issue_id=world["issue"].id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/timer/start/", json={"project_id": world["other"].id}, headers=await _headers(client, "ttp_logger")
    )

    assert resp.status_code == 201, resp.text
    replaced = resp.json()["replaced"]
    entry = (
        await db_session.execute(select(TimeEntry).where(TimeEntry.user_id == user.id, TimeEntry.comments.is_(None)))
    ).scalar_one()
    assert replaced == {
        "outcome": "logged",
        "project_id": world["project"].id,
        "project_key": "TTPRV",
        "issue_id": world["issue"].id,
        "issue_key": world["issue"].display_key,
        "hours": "1.00",
        "entry_id": entry.id,
    }


async def test_timer_start_reports_a_replaced_timer_that_was_discarded(
    client: AsyncClient, db_session: AsyncSession, world: dict
) -> None:
    user = world["users"]["logger"]
    db_session.add(
        ActiveTimerFactory.build(
            user_id=user.id,
            project_id=world["project"].id,
            issue_id=world["issue"].id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    world["roles"]["logger"].permissions = ["view_issues"]
    await db_session.commit()
    before = await _entry_count(db_session, user)

    resp = await client.post(
        "/api/v1/timer/start/", json={"project_id": world["other"].id}, headers=await _headers(client, "ttp_logger")
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["replaced"] == {
        "outcome": "discarded",
        "project_id": world["project"].id,
        "project_key": "TTPRV",
        "issue_id": world["issue"].id,
        "issue_key": world["issue"].display_key,
        "hours": None,
        "entry_id": None,
    }
    assert await _entry_count(db_session, user) == before


async def test_seeded_agent_role_reads_back_its_own_time(
    client: AsyncClient, db_session: AsyncSession, world: dict, activity: TimeEntryActivity
) -> None:
    """A service account with the seeded Agent permissions logs time, then lists and reads it back."""
    from specivo.cli.seed import _DEFAULT_ROLES

    agent_def = next(r for r in _DEFAULT_ROLES if r["name"] == "Agent")
    assert {VIEW, LOG} <= set(agent_def["permissions"])
    role = Role(name="Ttp Seeded Agent", permissions=agent_def["permissions"], builtin=0, issues_visibility="default")
    db_session.add(role)
    await db_session.flush()
    bot = await _user(db_session, "ttp_bot")
    await _member(db_session, world["project"], bot, [role])
    await db_session.commit()

    headers = await _headers(client, "ttp_bot")
    created = await client.post(
        "/api/v1/projects/TTPRV/time-entries/",
        json=_entry_body(activity, issue_id=world["issue"].id, comments="by-bot"),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    listed = await client.get(f"/api/v1/projects/TTPRV/time-entries/?user_id={bot.id}", headers=headers)
    assert listed.status_code == 200
    assert [item["comments"] for item in listed.json()["items"]] == ["by-bot"]
    single = await client.get(f"/api/v1/time-entries/{created.json()['id']}/", headers=headers)
    assert single.status_code == 200
