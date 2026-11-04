"""``GET /api/v1/projects/{key}/issues/similar/``: access, visibility, validation and the switches.

Callers are non-admin users holding roles on the project unless a test says
otherwise. The ranking itself is covered in ``tests/services/test_similar_issues.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import Permission, clear_role_cache
from specivo.services.similar_issues import SIMILAR_ISSUES_SETTING_KEY
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SUBJECT = "Login page shows a timeout error"
Q = "Timeout when opening the login page"
CREATE = [Permission.VIEW_ISSUES, Permission.ADD_ISSUES]


@dataclass
class World:
    project: Project
    public: Project
    tracker: Tracker
    status: IssueStatus
    priority: IssuePriority
    caller: User
    other: User
    admin: User


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    status = StatusFactory.build(name="Simapi New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Simapi Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Simapi Normal", is_default=False, position=2)
    project = ProjectFactory.build(key="SIMAPI", identifier="sim-api", is_public=False)
    public = ProjectFactory.build(key="SIMPUB", identifier="sim-pub", is_public=True)
    caller = UserFactory.build(login="simapi_caller", status="active")
    other = UserFactory.build(login="simapi_other", status="active")
    admin = AdminUserFactory.build(login="simapi_admin", status="active")
    db_session.add_all([tracker, priority, project, public, caller, other, admin])
    await db_session.commit()
    return World(project, public, tracker, status, priority, caller, other, admin)


async def grant(
    db: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str],
    *,
    visibility: str = "default",
    via_group: bool = False,
) -> None:
    role = Role(
        name=f"Simapi-{uuid.uuid4().hex[:8]}",
        position=2,
        assignable=True,
        builtin=0,
        permissions=list(permissions),
        issues_visibility=visibility,
        settings={},
    )
    db.add(role)
    await db.flush()
    if via_group:
        group = UserGroup(name=f"simapi-group-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(UserGroupMember(group_id=group.id, user_id=user.id))
        member = Member(project_id=project.id, group_id=group.id)
    else:
        member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)


_seq = iter(range(1, 100_000))


async def make_issue(
    db: AsyncSession,
    w: World,
    subject: str = SUBJECT,
    *,
    project: Project | None = None,
    author: User | None = None,
    assignee: User | None = None,
    is_private: bool = False,
    commit: bool = True,
) -> Issue:
    project = project or w.project
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=next(_seq),
        tracker_id=w.tracker.id,
        status_id=w.status.id,
        priority_id=w.priority.id,
        author_id=(author or w.other).id,
        assigned_to_id=assignee.id if assignee else None,
        subject=subject,
        is_private=is_private,
    )
    db.add(issue)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return issue


async def login(client: AsyncClient, user: User) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def similar(client: AsyncClient, headers: dict[str, str], project_key: str = "SIMAPI", **params):
    params.setdefault("q", Q)
    return await client.get(f"/api/v1/projects/{project_key}/issues/similar/", params=params, headers=headers)


async def keys(client: AsyncClient, headers: dict[str, str], project_key: str = "SIMAPI", **params) -> list[str]:
    resp = await similar(client, headers, project_key, **params)
    assert resp.status_code == 200, resp.text
    return [item["key"] for item in resp.json()]


async def set_instance(db: AsyncSession, value: str | None) -> None:
    row = await db.scalar(select(Setting).where(Setting.key == SIMILAR_ISSUES_SETTING_KEY))
    if value is None:
        if row is not None:
            await db.delete(row)
    elif row is None:
        db.add(Setting(key=SIMILAR_ISSUES_SETTING_KEY, value=value))
    else:
        row.value = value
    await db.commit()


async def set_project(db: AsyncSession, project: Project, enabled: bool) -> None:
    project.settings = {**(project.settings or {}), "similar_issues": enabled}
    await db.commit()


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


async def test_response_shape(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    issue = await make_issue(db_session, world)

    resp = await similar(client, await login(client, world.caller))

    assert resp.status_code == 200, resp.text
    [item] = resp.json()
    assert set(item) == {"key", "subject", "status", "is_closed", "score"}
    assert item["key"] == issue.display_key
    assert item["subject"] == SUBJECT
    assert item["status"] == {"id": world.status.id, "name": "Simapi New"}
    assert item["is_closed"] is False
    assert 0 < item["score"] <= 1


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


async def test_anonymous_gets_401_even_with_anonymous_access_on(client, db_session, world) -> None:
    await set_anonymous_permissions(db_session, world.public, [Permission.VIEW_ISSUES], world.admin)
    await set_anonymous_access_enabled(db_session, True, world.admin, confirmed_projects=[world.public.key])
    await db_session.commit()
    await make_issue(db_session, world, project=world.public)

    resp = await similar(client, {}, "SIMPUB")

    assert resp.status_code == 401


async def test_private_project_without_membership_is_404(client, db_session, world) -> None:
    await make_issue(db_session, world)

    resp = await similar(client, await login(client, world.caller))

    assert resp.status_code == 404


async def test_view_issues_only_is_403(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, [Permission.VIEW_ISSUES])
    await make_issue(db_session, world)

    resp = await similar(client, await login(client, world.caller))

    assert resp.status_code == 403


async def test_public_project_non_member_without_add_issues_is_403(client, db_session, world) -> None:
    non_member = await db_session.scalar(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))
    non_member.permissions = [Permission.VIEW_ISSUES]
    await db_session.commit()
    clear_role_cache(db_session)
    await make_issue(db_session, world, project=world.public)

    resp = await similar(client, await login(client, world.caller), "SIMPUB")

    assert resp.status_code == 403


async def test_project_limited_api_key(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    await grant(db_session, world.public, world.caller, CREATE)
    issue = await make_issue(db_session, world)
    await make_issue(db_session, world, project=world.public)
    key, raw = await ApiKeyService().create_key(session=db_session, user_id=world.caller.id, name="simapi-limited")
    key.scopes = {"projects": [world.project.key]}
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}

    outside = await similar(client, headers, "SIMPUB")
    assert outside.status_code == 404

    assert await keys(client, headers) == [issue.display_key]


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def test_other_users_private_issue_hidden(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    shown = await make_issue(db_session, world)
    await make_issue(db_session, world, is_private=True)

    assert await keys(client, await login(client, world.caller)) == [shown.display_key]


async def test_own_private_issue_shown_as_author_and_as_assignee(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    authored = await make_issue(db_session, world, author=world.caller, is_private=True)
    assigned = await make_issue(db_session, world, assignee=world.caller, is_private=True)

    assert set(await keys(client, await login(client, world.caller))) == {authored.display_key, assigned.display_key}


async def test_own_visibility_role_sees_only_own_issues(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE, visibility="own")
    authored = await make_issue(db_session, world, author=world.caller)
    assigned = await make_issue(db_session, world, assignee=world.caller)
    await make_issue(db_session, world)

    assert set(await keys(client, await login(client, world.caller))) == {authored.display_key, assigned.display_key}


async def test_add_issues_without_view_issues_gets_empty_list(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, [Permission.ADD_ISSUES])
    await make_issue(db_session, world)

    assert await keys(client, await login(client, world.caller)) == []


async def test_group_granted_access(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE, via_group=True)
    issue = await make_issue(db_session, world)

    assert await keys(client, await login(client, world.caller)) == [issue.display_key]


async def test_visibility_applies_before_the_candidate_cut(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    q = "Webhook delivery retries missing after server restart"
    # Created first: the lowest id, and fewer shared terms than the private ones.
    visible = await make_issue(db_session, world, "Webhook delivery retries")
    for _ in range(60):
        await make_issue(db_session, world, q, is_private=True, commit=False)
    await db_session.commit()

    assert await keys(client, await login(client, world.caller), q=q, limit=10) == [visible.display_key]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{"limit": 11}, {"limit": 0}, {"q": "x" * 256}, {"q": None}])
async def test_invalid_parameters_are_422(client, db_session, world, params) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    query = {"q": Q, **params}
    query = {k: v for k, v in query.items() if v is not None}

    resp = await client.get(
        "/api/v1/projects/SIMAPI/issues/similar/", params=query, headers=await login(client, world.caller)
    )

    assert resp.status_code == 422


@pytest.mark.parametrize("q", ["the and of to", "!!!???---", "foo:* & !bar | (baz", "login\x00 page", "a<->b"])
async def test_awkward_input_answers_200_with_a_list(client, db_session, world, q) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    await make_issue(db_session, world)

    resp = await similar(client, await login(client, world.caller), q=q)

    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


async def test_on_by_default(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    issue = await make_issue(db_session, world)
    await set_instance(db_session, None)

    assert await keys(client, await login(client, world.caller)) == [issue.display_key]


async def test_instance_off_gives_empty_list(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    await make_issue(db_session, world)
    await set_instance(db_session, "false")

    assert await keys(client, await login(client, world.caller)) == []


async def test_project_off_gives_empty_list(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    await make_issue(db_session, world)
    await set_project(db_session, world.project, False)

    assert await keys(client, await login(client, world.caller)) == []


async def test_instance_off_beats_project_on(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)
    await make_issue(db_session, world)
    await set_project(db_session, world.project, True)
    await set_instance(db_session, "false")

    assert await keys(client, await login(client, world.caller)) == []


async def test_disabled_without_add_issues_is_still_403(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, [Permission.VIEW_ISSUES])
    await set_instance(db_session, "false")

    resp = await similar(client, await login(client, world.caller))

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Project setting (PATCH /projects/{key}/)
# ---------------------------------------------------------------------------


async def test_manager_turns_project_setting_off_and_keeps_other_settings(client, db_session, world) -> None:
    world.project.settings = {"computed_metadata": {"team": "core"}}
    await db_session.commit()
    await grant(db_session, world.project, world.caller, [*CREATE, Permission.MANAGE_PROJECT])
    headers = await login(client, world.caller)

    before = await client.get("/api/v1/projects/SIMAPI/", headers=headers)
    assert before.json()["similar_issues"] is True

    resp = await client.patch("/api/v1/projects/SIMAPI/", json={"similar_issues": False}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["similar_issues"] is False

    detail = await client.get("/api/v1/projects/SIMAPI/", headers=headers)
    assert detail.json()["similar_issues"] is False
    await db_session.refresh(world.project)
    assert world.project.settings == {"computed_metadata": {"team": "core"}, "similar_issues": False}

    listed = await client.get("/api/v1/projects/", headers=headers)
    assert all(p["similar_issues"] is None for p in listed.json()["items"])


async def test_member_cannot_change_project_setting(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)

    resp = await client.patch(
        "/api/v1/projects/SIMAPI/", json={"similar_issues": False}, headers=await login(client, world.caller)
    )

    assert resp.status_code == 403
    await db_session.refresh(world.project)
    assert "similar_issues" not in (world.project.settings or {})


# ---------------------------------------------------------------------------
# Instance setting (admin Defaults form)
# ---------------------------------------------------------------------------


async def _instance_value(db: AsyncSession) -> str | None:
    db.expire_all()
    return await db.scalar(select(Setting.value).where(Setting.key == SIMILAR_ISSUES_SETTING_KEY))


async def test_admin_defaults_form_saves_the_instance_switch(admin_client: AsyncClient, db_session) -> None:
    async def post(data) -> None:
        resp = await admin_client.post(
            "/admin/settings/defaults/",
            data=data,
            cookies={"access_token": admin_client.state.token},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    # As the browser sends it: the hidden "false" first, the checkbox's "true" after it when ticked.
    await post({"similar_issues_enabled": ["false"]})
    assert await _instance_value(db_session) == "false"
    await post({"similar_issues_enabled": ["false", "true"]})
    assert await _instance_value(db_session) == "true"
    await post({"default_timezone": "Asia/Bangkok"})
    assert await _instance_value(db_session) == "true"
    await post({"similar_issues_enabled": "junk"})
    assert await _instance_value(db_session) == "true"


async def test_admin_settings_page_shows_the_instance_switch(admin_client: AsyncClient, db_session) -> None:
    await set_instance(db_session, "false")

    resp = await admin_client.get("/admin/settings/", cookies={"access_token": admin_client.state.token})

    assert resp.status_code == 200
    assert 'name="similar_issues_enabled"' in resp.text
    assert 'id="similar_issues_enabled"' in resp.text
    assert 'id="similar_issues_enabled" value="true" checked' not in resp.text


async def test_project_settings_page_shows_switch_and_instance_off_hint(admin_client: AsyncClient, db_session, world):
    cookies = {"access_token": admin_client.state.token}
    await set_project(db_session, world.project, False)

    resp = await admin_client.get("/projects/SIMAPI/settings/", cookies=cookies)
    assert resp.status_code == 200
    assert 'id="project-similar-issues"' in resp.text
    assert "similarIssues: false" in resp.text
    assert "similar-issues-instance-off" not in resp.text

    await set_instance(db_session, "false")
    resp = await admin_client.get("/projects/SIMAPI/settings/", cookies=cookies)
    assert "similar-issues-instance-off" in resp.text


# ---------------------------------------------------------------------------
# Issue form pages
# ---------------------------------------------------------------------------


async def _page(client: AsyncClient, headers: dict[str, str], path: str) -> str:
    resp = await client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


async def test_new_issue_page_enables_panel_by_settings(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, [*CREATE, Permission.MANAGE_ISSUE_RELATIONS])
    headers = await login(client, world.caller)

    text = await _page(client, headers, "/projects/SIMAPI/issues/new/")
    assert "similarEnabled: true" in text
    assert 'data-testid="similar-issues"' in text
    assert 'aria-live="polite"' in text
    assert "markDuplicate(item)" in text

    await set_project(db_session, world.project, False)
    text = await _page(client, headers, "/projects/SIMAPI/issues/new/")
    assert "similarEnabled: false" in text
    assert 'data-testid="similar-issues"' not in text

    await set_project(db_session, world.project, True)
    await set_instance(db_session, "false")
    text = await _page(client, headers, "/projects/SIMAPI/issues/new/")
    assert "similarEnabled: false" in text


async def test_new_issue_page_without_relations_permission_has_no_mark_button(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, CREATE)

    text = await _page(client, await login(client, world.caller), "/projects/SIMAPI/issues/new/")

    assert "similarEnabled: true" in text
    assert "markDuplicate(item)" not in text


async def test_edit_page_never_enables_panel(client, db_session, world) -> None:
    await grant(db_session, world.project, world.caller, [*CREATE, Permission.EDIT_ISSUES])
    issue = await make_issue(db_session, world, author=world.caller)

    text = await _page(client, await login(client, world.caller), f"/issue/{issue.display_key}/edit/")

    assert "similarEnabled: false" in text
    assert 'data-testid="similar-issues"' not in text
