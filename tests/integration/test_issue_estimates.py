"""The original and remaining estimates of an issue.

They are set and changed like ``estimated_hours``: through the REST API, the
issue form and the sidebar (both of which PATCH the API), bulk update, with
changes journalized and covered by workflow field rules. They are shown on the
issue page next to the time spent, in the REST output and in ``show_issue``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.workflow import WorkflowFieldRule
from specivo.schemas.workflow import FieldRuleCreate
from specivo.services.permission_service import clear_role_cache
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import UserFactory

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="Estimates new", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Estimates task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Estimates normal", is_default=True, position=2)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    return tracker, status, priority


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="ESTX", identifier="estimates-display")
    db_session.add(proj)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=proj.id, name="issue_tracking"))
    db_session.add(EnabledModule(project_id=proj.id, name="time_tracking"))
    await db_session.commit()
    return proj


async def _create(client: AsyncClient, project: Project, lookups, **fields) -> dict:
    tracker, status, priority = lookups
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "status_id": status.id,
            "priority_id": priority.id,
            "subject": "Estimate me",
            **fields,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_create_returns_both_estimates(admin_client: AsyncClient, project, lookups):
    data = await _create(admin_client, project, lookups, original_estimate=8, remaining_estimate=1.25)
    assert Decimal(data["original_estimate"]) == Decimal("8")
    assert Decimal(data["remaining_estimate"]) == Decimal("1.25")


async def test_estimates_default_to_null(admin_client: AsyncClient, project, lookups):
    data = await _create(admin_client, project, lookups)
    assert data["original_estimate"] is None
    assert data["remaining_estimate"] is None


async def test_get_and_list_show_the_estimates(admin_client: AsyncClient, project, lookups):
    data = await _create(admin_client, project, lookups, original_estimate=3, remaining_estimate=0)
    one = (await admin_client.get(f"/api/v1/issues/{data['key']}/")).json()
    assert Decimal(one["original_estimate"]) == 3
    assert Decimal(one["remaining_estimate"]) == 0
    listed = (await admin_client.get(f"/api/v1/projects/{project.key}/issues/")).json()["items"]
    assert Decimal(next(i for i in listed if i["key"] == data["key"])["remaining_estimate"]) == 0


@pytest.mark.parametrize("field", ["original_estimate", "remaining_estimate"])
async def test_negative_estimate_is_rejected(admin_client: AsyncClient, project, lookups, field):
    tracker, status, priority = lookups
    resp = await admin_client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={"project_key": project.key, "tracker_id": tracker.id, "subject": "Bad", field: -1},
    )
    assert resp.status_code == 422, resp.text
    data = await _create(admin_client, project, lookups)
    resp = await admin_client.patch(
        f"/api/v1/issues/{data['key']}/", json={field: -0.5, "lock_version": data["lock_version"]}
    )
    assert resp.status_code == 422, resp.text


async def test_patch_changes_the_estimates_and_journals_them(
    admin_client: AsyncClient, db_session: AsyncSession, project, lookups
):
    data = await _create(admin_client, project, lookups, original_estimate=8, remaining_estimate=8)
    resp = await admin_client.patch(
        f"/api/v1/issues/{data['key']}/",
        json={"remaining_estimate": 2.5, "original_estimate": 10, "lock_version": data["lock_version"]},
    )
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["remaining_estimate"]) == Decimal("2.5")
    assert Decimal(resp.json()["original_estimate"]) == 10

    details = (
        await db_session.execute(
            select(JournalDetail)
            .join(Journal, JournalDetail.journal_id == Journal.id)
            .where(Journal.issue_id == data["id"])
        )
    ).scalars()
    changes = {d.prop_key: (Decimal(d.old_value), Decimal(d.new_value)) for d in details}
    assert changes["remaining_estimate"] == (Decimal(8), Decimal("2.5"))
    assert changes["original_estimate"] == (Decimal(8), Decimal(10))


async def test_patch_without_the_estimates_leaves_them(admin_client: AsyncClient, project, lookups):
    data = await _create(admin_client, project, lookups, original_estimate=8, remaining_estimate=4)
    resp = await admin_client.patch(
        f"/api/v1/issues/{data['key']}/",
        json={"estimated_hours": 6, "remaining_estimate": None, "lock_version": data["lock_version"]},
    )
    assert resp.status_code == 200, resp.text
    # As for estimated_hours, null means "no change".
    assert Decimal(resp.json()["original_estimate"]) == 8
    assert Decimal(resp.json()["remaining_estimate"]) == 4


async def test_bulk_update_sets_the_remaining_estimate(admin_client: AsyncClient, project, lookups):
    first = await _create(admin_client, project, lookups)
    second = await _create(admin_client, project, lookups)
    resp = await admin_client.post(
        "/api/v1/issues/bulk-update/",
        json={"issue_ids": [first["id"], second["id"]], "updates": {"remaining_estimate": 1.5}},
    )
    assert resp.status_code == 200, resp.text
    for key in (first["key"], second["key"]):
        assert Decimal((await admin_client.get(f"/api/v1/issues/{key}/")).json()["remaining_estimate"]) == Decimal(
            "1.5"
        )


# ---------------------------------------------------------------------------
# Permissions and field rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["original_estimate", "remaining_estimate"])
def test_field_rules_can_name_the_estimates(field):
    rule = FieldRuleCreate(tracker_id=1, role_id=1, status_id=1, field_name=field, rule="readonly")
    assert rule.field_name == field


async def _member(db_session: AsyncSession, project: Project, permissions: list[str]):
    user = UserFactory.build(status="active")
    db_session.add(user)
    role = Role(name=f"Estimates {'-'.join(permissions)}", permissions=permissions, builtin=0, issues_visibility="all")
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    clear_role_cache(db_session)
    return user, role


async def _login(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": "testpassword"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_editing_the_estimates_needs_edit_issues(
    client: AsyncClient, admin_client: AsyncClient, db_session: AsyncSession, project, lookups
):
    data = await _create(admin_client, project, lookups, remaining_estimate=4)
    viewer, _role = await _member(db_session, project, ["view_issues"])
    resp = await client.patch(
        f"/api/v1/issues/{data['key']}/",
        json={"remaining_estimate": 1, "lock_version": data["lock_version"]},
        headers=await _login(client, viewer.login),
    )
    assert resp.status_code == 403, resp.text


async def test_readonly_field_rule_protects_the_remaining_estimate(
    client: AsyncClient, admin_client: AsyncClient, db_session: AsyncSession, project, lookups
):
    tracker, status, _priority = lookups
    data = await _create(admin_client, project, lookups, remaining_estimate=4)
    editor, role = await _member(db_session, project, ["view_issues", "edit_issues"])
    db_session.add(
        WorkflowFieldRule(
            tracker_id=tracker.id,
            role_id=role.id,
            status_id=status.id,
            field_name="remaining_estimate",
            rule="readonly",
        )
    )
    await db_session.commit()
    headers = await _login(client, editor.login)
    resp = await client.patch(
        f"/api/v1/issues/{data['key']}/",
        json={"remaining_estimate": 1, "lock_version": data["lock_version"]},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    resp = await client.patch(
        f"/api/v1/issues/{data['key']}/",
        json={"original_estimate": 5, "lock_version": data["lock_version"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Issue page
# ---------------------------------------------------------------------------


async def test_issue_page_shows_both_estimates(admin_client: AsyncClient, project, lookups):
    data = await _create(
        admin_client, project, lookups, estimated_hours=6, original_estimate=8.5, remaining_estimate=1.75
    )
    resp = await admin_client.get(f"/issue/{data['key']}/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Original estimate" in html
    assert "Remaining estimate" in html
    # Sidebar editors, as hours and minutes like the estimate.
    assert 'x-ref="origH" value="8"' in html
    assert 'x-ref="origM" value="30"' in html
    assert 'x-ref="remH" value="1"' in html
    assert 'x-ref="remM" value="45"' in html


async def test_time_tab_total_shows_the_estimates_like_the_time_spent(
    admin_client: AsyncClient, db_session: AsyncSession, project, lookups
):
    data = await _create(
        admin_client, project, lookups, estimated_hours=6, original_estimate=8.5, remaining_estimate=1.75
    )
    activity = TimeEntryActivityFactory.build(name="Estimates work", is_default=False)
    db_session.add(activity)
    await db_session.flush()
    db_session.add(
        TimeEntryFactory.build(
            project_id=project.id,
            issue_id=data["id"],
            user_id=admin_client.state.user.id,
            activity_id=activity.id,
            hours=Decimal("2.5"),
        )
    )
    await db_session.commit()
    resp = await admin_client.get(f"/issue/{data['key']}/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200, resp.text
    # Hours with one decimal, like the estimate beside them.
    assert "6.0h est." in resp.text
    assert "8.5h original" in resp.text
    assert "1.8h remaining" in resp.text


async def test_issue_page_without_estimates_shows_empty_editors(admin_client: AsyncClient, project, lookups):
    data = await _create(admin_client, project, lookups)
    resp = await admin_client.get(f"/issue/{data['key']}/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200, resp.text
    assert 'x-ref="remH" value=""' in resp.text


async def test_edit_form_carries_the_estimates(admin_client: AsyncClient, project, lookups):
    data = await _create(admin_client, project, lookups, original_estimate=8, remaining_estimate=0.5)
    resp = await admin_client.get(f"/issue/{data['key']}/edit/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200, resp.text
    assert "original_estimate: " in resp.text
    assert '"0.50"' in resp.text
