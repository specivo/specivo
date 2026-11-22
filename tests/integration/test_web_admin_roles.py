"""Web admin role pages: the list, the "who can do what" matrix, the new-role form and the role editor.

The pages are GET only; every write goes through ``/api/v1/admin/roles/``.
So these tests check what the pages show (the summary, the matrix marks,
the Non member and Automation variants, why delete is refused) and who may
see them, plus that the old form POST is gone and the API refuses a
cookie-session write without the CSRF header.

Role names are unique database-wide and test modules run in parallel
inside uncommitted transactions, so names here carry a module-local prefix.
"""

from __future__ import annotations

import itertools

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.role import ROLE_SYSTEM_KEY_AUTOMATION, Role, RoleBuiltin
from specivo.models.user import User
from specivo.models.workflow import WorkflowTransition
from specivo.services.auth_service import _make_access_token
from tests.factories.lookups import StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_PREFIX = "webrole"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


async def _role(db: AsyncSession, stem: str, permissions: list[str], **kwargs: object) -> Role:
    role = Role(name=_name(stem), permissions=permissions, builtin=0, issues_visibility="default", **kwargs)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def _non_member(db: AsyncSession) -> Role:
    return (await db.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()


async def _automation(db: AsyncSession) -> Role:
    existing = (
        await db.execute(select(Role).where(Role.system_key == ROLE_SYSTEM_KEY_AUTOMATION))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return await _role(db, "Automation", ["view_issues"], system_key=ROLE_SYSTEM_KEY_AUTOMATION, assignable=False)


async def _admin(db: AsyncSession) -> User:
    user = AdminUserFactory.build()
    db.add(user)
    await db.commit()
    return user


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_summary_leaves_out_permissions_not_used_yet(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    # manage_subtasks and delete_own_notes are "Not used yet": neither number counts them.
    await _role(db_session, "Unused perms", ["view_issues", "manage_subtasks", "delete_own_notes"])

    resp = await admin_client.get("/admin/roles/")

    assert '"summary": "1 of 25"' in resp.text


async def test_list_shows_roles_summary_and_order(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    partial = await _role(db_session, "Partial", ["view_issues", "add_issues"])
    full = await _role(db_session, "Lead", ["*"])

    resp = await admin_client.get("/admin/roles/")
    assert resp.status_code == 200
    text = resp.text
    assert partial.name in text
    assert full.name in text
    assert "adminRoles(" in text
    assert "/admin/roles/new/" in text
    assert "/admin/roles/matrix/" in text
    # The summary: a count for a partial role, "Full access" for "*".
    assert '"summary": "2 of 25"' in text
    assert '"summary": "Full access"' in text
    # Non member is listed after the custom roles, marked built-in, with no arrows.
    non_member = await _non_member(db_session)
    assert non_member.name in text
    assert text.index(full.name) < text.index(f"/admin/roles/{non_member.id}/")
    assert "Built-in" in text
    # The visibility selects moved to the role page.
    assert 'name="users_visibility"' not in text


async def test_list_counts_memberships(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, "Counted", ["view_issues"])
    project = ProjectFactory.build()
    user = UserFactory.build()
    db_session.add_all([project, user])
    await db_session.flush()
    member = Member(project_id=project.id, user_id=user.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    resp = await admin_client.get("/admin/roles/")
    assert resp.status_code == 200
    assert f'"id": {role.id}' in resp.text
    row = resp.text[resp.text.index(f'"id": {role.id}') :]
    assert '"membership_count": 1' in row[: row.index("}")]


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------


async def test_matrix_marks_granted_permissions(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    reader = await _role(db_session, "WikiReader", ["view_wiki"])
    await _role(db_session, "Everything", ["*"])

    resp = await admin_client.get("/admin/roles/matrix/")
    assert resp.status_code == 200
    text = resp.text
    assert reader.name in text
    assert "sp-roles-matrix" in text
    assert "Full access" in text
    assert "Not used yet" in text
    assert "&#10003;" in text or "✓" in text
    # Server-rendered, no Alpine component.
    assert "adminRoles(" not in text
    assert 'aria-current="page"' in text


# ---------------------------------------------------------------------------
# New role
# ---------------------------------------------------------------------------


async def test_new_role_form_offers_copy_sources(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    source = await _role(db_session, "Source", ["view_issues"])

    resp = await admin_client.get("/admin/roles/new/")
    assert resp.status_code == 200
    text = resp.text
    assert "adminRoleNew(" in text
    assert f'<option value="{source.id}">' in text
    assert "A blank role" in text
    assert "workflow transitions" in text


# ---------------------------------------------------------------------------
# Role editor
# ---------------------------------------------------------------------------


async def test_editor_for_custom_role(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, "Editor", ["view_wiki", "manage_wiki"])

    resp = await admin_client.get(f"/admin/roles/{role.id}/")
    assert resp.status_code == 200
    text = resp.text
    assert "adminRoleEditor(" in text
    assert f'"lock_version": {role.lock_version}' in text
    # Every catalogue permission is a checkbox, grouped, with its closure for the JS.
    assert 'value="delete_wiki_pages"' in text
    assert '"requires": ["view_wiki", "manage_wiki"]' in text
    assert "Full access" in text
    assert 'role="switch"' in text
    assert "Offered when adding members" in text
    assert "Not used yet" in text
    assert "Only applies in projects with the Wiki module enabled." in text
    # No workflow transitions yet: the warning links to Workflows.
    assert "This role can&#39;t change issue status yet." in text or "This role can't change issue status yet." in text
    assert "/admin/workflows/" in text
    # Deletable: the button is live.
    assert "Delete role" in text
    assert "Built-in role" not in text


async def test_editor_hides_workflow_warning_with_transitions(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    role = await _role(db_session, "Mover", ["view_issues"])
    tracker: Tracker = TrackerFactory.build()
    old: IssueStatus = StatusFactory.build()
    new: IssueStatus = StatusFactory.build()
    db_session.add_all([tracker, old, new])
    await db_session.flush()
    db_session.add(
        WorkflowTransition(tracker_id=tracker.id, role_id=role.id, old_status_id=old.id, new_status_id=new.id)
    )
    await db_session.commit()

    resp = await admin_client.get(f"/admin/roles/{role.id}/")
    assert resp.status_code == 200
    assert "change issue status yet" not in resp.text


async def test_editor_for_non_member(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _non_member(db_session)

    resp = await admin_client.get(f"/admin/roles/{role.id}/")
    assert resp.status_code == 200
    text = resp.text
    assert '"non_member": true' in text
    # No Full access switch, no assignable box, read-only name.
    assert 'role="switch"' not in text
    assert "Offered when adding members" not in text
    assert "readonly" in text
    # Permissions outside the ceiling are disabled and say why.
    assert "Not available for Non member" in text
    # A built-in role has no workflow warning and cannot be deleted.
    assert "change issue status yet" not in text
    assert "Built-in role: it can&#39;t be deleted." in text or "Built-in role: it can't be deleted." in text


async def test_editor_for_automation_role(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _automation(db_session)

    resp = await admin_client.get(f"/admin/roles/{role.id}/")
    assert resp.status_code == 200
    assert "Used by merge automation" in resp.text


async def test_editor_lists_projects_that_block_delete(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, "Busy", ["view_issues"])
    project = ProjectFactory.build(name="Webrole Busy Project")
    user = UserFactory.build()
    db_session.add_all([project, user])
    await db_session.flush()
    member = Member(project_id=project.id, user_id=user.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    resp = await admin_client.get(f"/admin/roles/{role.id}/")
    assert resp.status_code == 200
    text = resp.text
    assert "1 project membership uses this role." in text
    assert "Webrole Busy Project" in text
    assert f"/projects/{project.key}/settings/" in text


async def test_editor_unknown_role_is_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/admin/roles/999999999/")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


async def test_pages_refuse_non_admins(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, "Hidden", ["view_issues"])
    token = auth_client.state.token
    for path in ("/admin/roles/", "/admin/roles/matrix/", "/admin/roles/new/", f"/admin/roles/{role.id}/"):
        resp = await auth_client.get(path, cookies={"access_token": token})
        assert resp.status_code == 403, path


async def test_pages_redirect_anonymous_to_login(client: AsyncClient) -> None:
    for path in ("/admin/roles/", "/admin/roles/matrix/", "/admin/roles/new/"):
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303, 307), path
        assert "/login/" in resp.headers["location"]


async def test_old_form_post_is_gone(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, "Legacy", ["view_issues"])
    resp = await admin_client.post(f"/admin/roles/{role.id}/", data={"users_visibility": "all"})
    assert resp.status_code == 405


async def test_cookie_session_patch_without_csrf_header_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _admin(db_session)
    role = await _role(db_session, "Csrf", ["view_issues"])

    resp = await client.patch(
        f"/api/v1/admin/roles/{role.id}/",
        json={"lock_version": role.lock_version, "users_visibility": "members_of_visible_projects"},
        cookies=_cookies(admin),
    )
    assert resp.status_code == 403
    await db_session.refresh(role)
    assert role.users_visibility == "all"
