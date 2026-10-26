"""Private-by-default trackers, and the admin pages that edit role and tracker visibility.

A tracker's ``private_by_default`` applies whenever an issue is created
without an explicit privacy: through the REST API (and so the web form), the
MCP tool, and recurring patterns. Overriding the default needs
``set_issues_private``. The importer is covered in the import loader tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.schemas.recurring_pattern import RecurringPatternCreate
from specivo.services.permission_service import clear_role_cache
from specivo.services.recurring_pattern_service import RecurringPatternService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _login(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> dict:
    """A project, a private-by-default tracker, a normal one, and two members.

    ``reporter`` may add issues; ``lead`` may also set issues private.
    """
    status = StatusFactory.build(name="Privdef Open")
    db_session.add(status)
    await db_session.flush()
    secret = TrackerFactory.build(name="Privdef Security", default_status_id=status.id, private_by_default=True)
    plain = TrackerFactory.build(name="Privdef Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Privdef Normal", is_default=False)
    project = ProjectFactory.build(key="PRVD", identifier="prvd")
    db_session.add_all([secret, plain, priority, project])
    await db_session.flush()

    users = {}
    for login, permissions in (
        ("privdef_reporter", ["view_issues", "add_issues", "manage_recurring_tasks"]),
        ("privdef_lead", ["view_issues", "add_issues", "set_issues_private", "manage_recurring_tasks"]),
    ):
        user = UserFactory.build(login=login, status="active")
        role = Role(name=f"Privdef {login}", permissions=permissions, builtin=0, issues_visibility="default")
        db_session.add_all([user, role])
        await db_session.flush()
        member = Member(project_id=project.id, user_id=user.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
        users[login.removeprefix("privdef_")] = user
    await db_session.commit()
    clear_role_cache(db_session)
    return {"project": project, "secret": secret, "plain": plain, "status": status, "priority": priority, **users}


async def _create(client: AsyncClient, headers: dict, tracker: Tracker, **extra) -> object:
    body = {"project_key": "PRVD", "tracker_id": tracker.id, "subject": "Privdef issue", **extra}
    return await client.post("/api/v1/projects/PRVD/issues/", json=body, headers=headers)


# ---------------------------------------------------------------------------
# REST API (the web form posts here)
# ---------------------------------------------------------------------------


async def test_issue_of_private_tracker_is_private_without_asking(client: AsyncClient, world: dict) -> None:
    headers = await _login(client, "privdef_reporter")
    resp = await _create(client, headers, world["secret"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_private"] is True


async def test_other_trackers_are_unchanged(client: AsyncClient, world: dict) -> None:
    headers = await _login(client, "privdef_reporter")
    resp = await _create(client, headers, world["plain"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_private"] is False
    # Repeating the default explicitly needs no permission.
    resp = await _create(client, headers, world["secret"], is_private=True)
    assert resp.status_code == 201, resp.text


async def test_unticking_needs_set_issues_private(client: AsyncClient, world: dict) -> None:
    reporter = await _login(client, "privdef_reporter")
    resp = await _create(client, reporter, world["secret"], is_private=False)
    assert resp.status_code == 403

    lead = await _login(client, "privdef_lead")
    resp = await _create(client, lead, world["secret"], is_private=False)
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_private"] is False


async def test_making_an_issue_private_needs_set_issues_private(client: AsyncClient, world: dict) -> None:
    reporter = await _login(client, "privdef_reporter")
    assert (await _create(client, reporter, world["plain"], is_private=True)).status_code == 403

    lead = await _login(client, "privdef_lead")
    resp = await _create(client, lead, world["plain"], is_private=True)
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_private"] is True


async def test_admin_may_override(admin_client: AsyncClient, world: dict) -> None:
    resp = await _create(admin_client, {}, world["secret"], is_private=False)
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_private"] is False


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def test_mcp_create_issue_applies_the_tracker_default(db_session: AsyncSession, world: dict) -> None:
    from specivo.mcp.tools import _create_issue

    await _create_issue(db_session, world["reporter"], "PRVD", world["secret"].id, "Privdef via MCP")
    issue = (await db_session.execute(select(Issue).where(Issue.subject == "Privdef via MCP"))).scalar_one()
    assert issue.is_private is True

    await _create_issue(db_session, world["lead"], "PRVD", world["secret"].id, "Privdef public", is_private=False)
    issue = (await db_session.execute(select(Issue).where(Issue.subject == "Privdef public"))).scalar_one()
    assert issue.is_private is False


# ---------------------------------------------------------------------------
# Recurring patterns
# ---------------------------------------------------------------------------


def _pattern(world: dict, **overrides) -> RecurringPatternCreate:
    data = {
        "name": "Privdef weekly audit",
        "template_tracker_id": world["secret"].id,
        "template_status_id": world["status"].id,
        "template_priority_id": world["priority"].id,
        "template_subject": "Weekly audit",
        "freq": "daily",
        "rrule_interval": 1,
        "dtstart": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "anchor_mode": "fixed",
        "creation_lead_time_days": 1,
    }
    data.update(overrides)
    return RecurringPatternCreate(**data)


async def test_recurring_pattern_and_its_issues_follow_the_tracker(db_session: AsyncSession, world: dict) -> None:
    service = RecurringPatternService()
    pattern = await service.create(db_session, world["project"], _pattern(world), world["reporter"])
    assert pattern.is_private is True

    created = await service.materialize(db_session, pattern, datetime(2026, 1, 2, 12, 0, tzinfo=UTC))
    assert created
    assert all(issue.is_private for issue in created)


async def test_recurring_pattern_override_needs_set_issues_private(db_session: AsyncSession, world: dict) -> None:
    from specivo.core.exceptions import PermissionDeniedError

    service = RecurringPatternService()
    with pytest.raises(PermissionDeniedError):
        await service.create(db_session, world["project"], _pattern(world, is_private=False), world["reporter"])

    pattern = await service.create(db_session, world["project"], _pattern(world, is_private=False), world["lead"])
    created = await service.materialize(db_session, pattern, datetime(2026, 1, 2, 12, 0, tzinfo=UTC))
    assert created
    assert not any(issue.is_private for issue in created)


# ---------------------------------------------------------------------------
# Web form
# ---------------------------------------------------------------------------


async def test_issue_form_shows_privacy_only_with_permission(client: AsyncClient, world: dict) -> None:
    reporter = (await _login(client, "privdef_reporter"))["Authorization"].removeprefix("Bearer ")
    resp = await client.get("/projects/PRVD/issues/new/", cookies={"access_token": reporter})
    assert resp.status_code == 200
    assert 'id="is_private"' not in resp.text
    assert f'"{world["secret"].id}": true' in resp.text

    lead = (await _login(client, "privdef_lead"))["Authorization"].removeprefix("Bearer ")
    resp = await client.get("/projects/PRVD/issues/new/", cookies={"access_token": lead})
    assert resp.status_code == 200
    assert 'id="is_private"' in resp.text


# ---------------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------------


async def test_admin_edits_role_visibility(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = Role(name="Privdef Contractor", permissions=[], builtin=0, issues_visibility="default")
    db_session.add(role)
    await db_session.commit()

    page = await admin_client.get("/admin/roles/")
    assert page.status_code == 200
    assert "Privdef Contractor" in page.text

    resp = await admin_client.post(
        f"/admin/roles/{role.id}/",
        data={"users_visibility": "members_of_visible_projects", "time_entries_visibility": "own"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await db_session.refresh(role)
    assert (role.users_visibility, role.time_entries_visibility) == ("members_of_visible_projects", "own")

    # Unknown values leave the role unchanged.
    await admin_client.post(
        f"/admin/roles/{role.id}/", data={"users_visibility": "everyone", "time_entries_visibility": "x"}
    )
    await db_session.refresh(role)
    assert (role.users_visibility, role.time_entries_visibility) == ("members_of_visible_projects", "own")


async def test_admin_edits_tracker_private_default(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    from specivo.core.lookup_cache import get_lookups

    tracker = TrackerFactory.build(name="Privdef HR")
    db_session.add(tracker)
    await db_session.commit()
    await get_lookups(db_session)  # warm the cache the issue form reads

    page = await admin_client.get("/admin/trackers/")
    assert page.status_code == 200
    assert "Privdef HR" in page.text

    resp = await admin_client.post(f"/admin/trackers/{tracker.id}/", data={"private_by_default": "1"})
    assert resp.status_code in (200, 303)
    await db_session.refresh(tracker)
    assert tracker.private_by_default is True
    cached = {t.id: t for t in (await get_lookups(db_session)).trackers}
    assert cached[tracker.id].private_by_default is True

    # An unticked checkbox sends nothing: off.
    await admin_client.post(f"/admin/trackers/{tracker.id}/", data={})
    await db_session.refresh(tracker)
    assert tracker.private_by_default is False


async def test_admin_pages_refuse_non_admins(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    tracker = TrackerFactory.build(name="Privdef Locked")
    db_session.add(tracker)
    await db_session.commit()
    token = auth_client.state.token
    for path in ("/admin/roles/", "/admin/trackers/"):
        resp = await auth_client.get(path, cookies={"access_token": token})
        assert resp.status_code == 403
    resp = await auth_client.post(
        f"/admin/trackers/{tracker.id}/", data={"private_by_default": "1"}, cookies={"access_token": token}
    )
    assert resp.status_code == 403
    await db_session.refresh(tracker)
    assert tracker.private_by_default is False


@pytest.fixture(autouse=True)
def _fresh_lookup_cache():
    """The lookup cache is process-global; keep other tests' trackers out of it."""
    from specivo.core.lookup_cache import invalidate_lookups

    invalidate_lookups()
    yield
    invalidate_lookups()
