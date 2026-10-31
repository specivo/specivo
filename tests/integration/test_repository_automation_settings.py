"""Commit command and merge automation settings of a repository integration, and the ``$git`` membership they need.

Enabling merge automation gives ``$git`` a project role (the seeded
Automation role unless another is picked); turning it off everywhere in the
project removes that membership. ``$git`` never shows up as a person: not in
member lists, pickers or counts.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.project_service import ProjectService
from tests.factories.lookups import DoneStatusFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_BODY = {
    "provider": "github",
    "repo_full_name": "example-org/widgets",
    "repo_url": "https://github.example.com/example-org/widgets",
}


@pytest_asyncio.fixture
async def project(db_session: AsyncSession):
    proj = ProjectFactory.build(key="RAUTO", name="Automation settings", identifier="rauto", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def resolved(db_session: AsyncSession):
    status = DoneStatusFactory.build(name="Rauto Resolved")
    db_session.add(status)
    await db_session.commit()
    return status


async def _member(db: AsyncSession, project, login: str, permissions: list[str]) -> User:
    user = UserFactory.build(login=login, status="active")
    role = Role(name=f"rauto-{login}", permissions=permissions)
    db.add_all([user, role])
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    return user


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def manager(client, db_session, project):
    await _member(db_session, project, "rauto_manager", ["view_issues", "manage_project", "manage_members"])
    return await _headers(client, "rauto_manager")


@pytest_asyncio.fixture
async def settings_only_manager(client, db_session, project):
    await _member(db_session, project, "rauto_settings", ["view_issues", "manage_project"])
    return await _headers(client, "rauto_settings")


def _base(project) -> str:
    return f"/api/v1/projects/{project.key}/repositories/"


async def _git_roles(db: AsyncSession, project) -> list[str]:
    rows = await db.execute(
        select(Role.name)
        .join(MemberRole, MemberRole.role_id == Role.id)
        .join(Member, Member.id == MemberRole.member_id)
        .join(User, User.id == Member.user_id)
        .where(User.login == "$git", Member.project_id == project.id)
    )
    return sorted(rows.scalars().all())


async def _automation_role(db: AsyncSession) -> Role:
    return (await db.execute(select(Role).where(Role.name == "Automation"))).scalar_one()


class TestSettingsValidation:
    async def test_defaults_are_off(self, client, project, manager):
        resp = await client.post(_base(project), json=_BODY, headers=manager)
        assert resp.status_code == 201, resp.text
        assert resp.json()["automation_role_id"] is None

    @pytest.mark.parametrize(
        "settings",
        [
            {"merge_automation_enabled": True},  # no merge status
            {"merge_automation_enabled": True, "merge_status_id": 999_999},  # unknown status
            {"commands_enabled": True, "fixing_status_id": 999_999},
            {"time_logging_enabled": True},  # time logging is a commit command
        ],
    )
    async def test_invalid_settings(self, client, project, manager, settings):
        resp = await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)
        assert resp.status_code == 422, resp.text

    async def test_commands_without_fixing_status_are_allowed(self, client, project, manager):
        settings = {"commands_enabled": True, "time_logging_enabled": True}
        resp = await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)
        assert resp.status_code == 201, resp.text
        assert resp.json()["settings"]["time_logging_enabled"] is True


class TestGitMembership:
    async def test_enabling_automation_gives_git_the_automation_role(
        self, client, db_session, project, manager, resolved
    ):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        resp = await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)
        assert resp.status_code == 201, resp.text
        role = await _automation_role(db_session)
        assert resp.json()["automation_role_id"] == role.id
        assert await _git_roles(db_session, project) == ["Automation"]

    async def test_picking_another_role(self, client, db_session, project, manager, resolved):
        other = Role(name="Rauto Bot", permissions=["view_issues", "edit_issues"])
        db_session.add(other)
        await db_session.commit()
        created = (await client.post(_base(project), json=_BODY, headers=manager)).json()
        resp = await client.patch(
            f"{_base(project)}{created['id']}/",
            json={
                "settings": {"merge_automation_enabled": True, "merge_status_id": resolved.id},
                "automation_role_id": other.id,
            },
            headers=manager,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["automation_role_id"] == other.id
        assert await _git_roles(db_session, project) == ["Rauto Bot"]

    async def test_picking_a_role_needs_manage_members(
        self, client, db_session, project, settings_only_manager, resolved
    ):
        other = Role(name="Rauto Powerful", permissions=["*"])
        db_session.add(other)
        await db_session.commit()
        body = {
            **_BODY,
            "settings": {"merge_automation_enabled": True, "merge_status_id": resolved.id},
            "automation_role_id": other.id,
        }
        resp = await client.post(_base(project), json=body, headers=settings_only_manager)
        assert resp.status_code == 403, resp.text
        assert await _git_roles(db_session, project) == []
        # The default Automation role needs no more than manage_project.
        del body["automation_role_id"]
        resp = await client.post(_base(project), json=body, headers=settings_only_manager)
        assert resp.status_code == 201, resp.text

    async def _with_custom_git_role(self, client, db_session, project, manager, resolved) -> tuple[dict, Role]:
        """An integration with merge automation whose ``$git`` role a member manager picked."""
        bot = Role(name="Rauto Custom Bot", permissions=["view_issues", "edit_issues"])
        db_session.add(bot)
        await db_session.commit()
        body = {
            **_BODY,
            "settings": {"merge_automation_enabled": True, "merge_status_id": resolved.id},
            "automation_role_id": bot.id,
        }
        resp = await client.post(_base(project), json=body, headers=manager)
        assert resp.status_code == 201, resp.text
        return resp.json(), bot

    async def test_resending_the_current_role_needs_no_manage_members(
        self, client, db_session, project, manager, settings_only_manager, resolved
    ):
        created, bot = await self._with_custom_git_role(client, db_session, project, manager, resolved)
        resp = await client.patch(
            f"{_base(project)}{created['id']}/",
            json={"settings": {**created["settings"], "commands_enabled": True}, "automation_role_id": bot.id},
            headers=settings_only_manager,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"]["commands_enabled"] is True
        assert await _git_roles(db_session, project) == ["Rauto Custom Bot"]

    async def test_changing_to_the_automation_role_needs_manage_project_only(
        self, client, db_session, project, manager, settings_only_manager, resolved
    ):
        created, _bot = await self._with_custom_git_role(client, db_session, project, manager, resolved)
        automation = await _automation_role(db_session)
        resp = await client.patch(
            f"{_base(project)}{created['id']}/",
            json={"automation_role_id": automation.id},
            headers=settings_only_manager,
        )
        assert resp.status_code == 200, resp.text
        assert await _git_roles(db_session, project) == ["Automation"]

    async def test_changing_to_another_role_needs_manage_members(
        self, client, db_session, project, manager, settings_only_manager, resolved
    ):
        created, _bot = await self._with_custom_git_role(client, db_session, project, manager, resolved)
        other = Role(name="Rauto Other Bot", permissions=["*"])
        db_session.add(other)
        await db_session.commit()
        resp = await client.patch(
            f"{_base(project)}{created['id']}/", json={"automation_role_id": other.id}, headers=settings_only_manager
        )
        assert resp.status_code == 403, resp.text
        assert await _git_roles(db_session, project) == ["Rauto Custom Bot"]

    async def test_builtin_or_unknown_role_refused(self, client, db_session, project, manager, resolved):
        non_member = (await db_session.execute(select(Role).where(Role.builtin == 1))).scalar_one_or_none()
        for role_id in [999_999] + ([non_member.id] if non_member else []):
            body = {
                **_BODY,
                "settings": {"merge_automation_enabled": True, "merge_status_id": resolved.id},
                "automation_role_id": role_id,
            }
            resp = await client.post(_base(project), json=body, headers=manager)
            assert resp.status_code in (404, 422), resp.text

    async def test_disabling_removes_the_membership(self, client, db_session, project, manager, resolved):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        created = (await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)).json()
        resp = await client.patch(
            f"{_base(project)}{created['id']}/",
            json={"settings": {"merge_automation_enabled": False, "merge_status_id": resolved.id}},
            headers=manager,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["automation_role_id"] is None
        assert await _git_roles(db_session, project) == []

    async def test_membership_stays_while_another_integration_uses_it(
        self, client, db_session, project, manager, resolved
    ):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        first = (await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)).json()
        second_body = {**_BODY, "repo_full_name": "example-org/gadgets", "settings": settings}
        await client.post(_base(project), json=second_body, headers=manager)
        resp = await client.delete(f"{_base(project)}{first['id']}/", headers=manager)
        assert resp.status_code == 204
        assert await _git_roles(db_session, project) == ["Automation"]


async def _git_audit(db: AsyncSession, project) -> list[tuple[str, int, str, list[str]]]:
    """(action, actor id, target login, roles) of the membership audit rows about ``$git``, oldest first."""
    from specivo.models.security_audit import SecurityAuditLog
    from specivo.services.security_audit_service import AuditEvent

    rows = await db.execute(
        select(SecurityAuditLog)
        .where(SecurityAuditLog.event_type == AuditEvent.MEMBER_CHANGE, SecurityAuditLog.project_id == project.id)
        .order_by(SecurityAuditLog.id)
    )
    return [
        (log.details["action"], log.user_id, log.details.get("target_login"), log.details.get("roles", []))
        for log in rows.scalars()
        if log.details.get("target_login") == "$git"
    ]


async def _login_id(db: AsyncSession, login: str) -> int:
    return (await db.execute(select(User.id).where(User.login == login))).scalar_one()


class TestGitMembershipAudit:
    """Each ``$git`` membership change writes the audit row the members API writes, by whoever changed the setting."""

    async def test_turning_automation_on_audits_the_new_membership(
        self, client, db_session, project, manager, resolved
    ):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        resp = await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)
        assert resp.status_code == 201, resp.text
        actor = await _login_id(db_session, "rauto_manager")
        assert await _git_audit(db_session, project) == [("added", actor, "$git", ["Automation"])]

    async def test_saving_without_a_change_writes_nothing_more(self, client, db_session, project, manager, resolved):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        created = (await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)).json()
        resp = await client.patch(f"{_base(project)}{created['id']}/", json={"settings": settings}, headers=manager)
        assert resp.status_code == 200, resp.text
        assert [row[0] for row in await _git_audit(db_session, project)] == ["added"]

    async def test_changing_the_role_audits_the_new_roles(self, client, db_session, project, manager, resolved):
        other = Role(name="Rauto Audit Bot", permissions=["view_issues", "edit_issues"])
        db_session.add(other)
        await db_session.commit()
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        created = (await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)).json()
        resp = await client.patch(
            f"{_base(project)}{created['id']}/", json={"automation_role_id": other.id}, headers=manager
        )
        assert resp.status_code == 200, resp.text
        actor = await _login_id(db_session, "rauto_manager")
        assert (await _git_audit(db_session, project))[-1] == ("roles_changed", actor, "$git", ["Rauto Audit Bot"])

    async def test_turning_automation_off_audits_the_removal(
        self, client, db_session, project, manager, settings_only_manager, resolved
    ):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        created = (await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)).json()
        resp = await client.patch(
            f"{_base(project)}{created['id']}/",
            json={"settings": {**settings, "merge_automation_enabled": False}},
            headers=settings_only_manager,
        )
        assert resp.status_code == 200, resp.text
        actor = await _login_id(db_session, "rauto_settings")
        assert [row[:3] for row in await _git_audit(db_session, project)] == [
            ("added", await _login_id(db_session, "rauto_manager"), "$git"),
            ("removed", actor, "$git"),
        ]

    async def test_disconnecting_the_last_integration_audits_the_removal(
        self, client, db_session, project, manager, resolved
    ):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        created = (await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)).json()
        resp = await client.delete(f"{_base(project)}{created['id']}/", headers=manager)
        assert resp.status_code == 204
        assert [row[0] for row in await _git_audit(db_session, project)] == ["added", "removed"]


class TestGitHidden:
    async def test_git_is_not_a_person(self, client, db_session, project, manager, resolved):
        settings = {"merge_automation_enabled": True, "merge_status_id": resolved.id}
        await client.post(_base(project), json={**_BODY, "settings": settings}, headers=manager)
        service = ProjectService()

        members = await service.list_members(db_session, project)
        assert "$git" not in [m["login"] for m in members]
        assert await service.count_people_with_access(db_session, project) == 1
        assert await service.count_membership_rows(db_session, project) == 1

        resp = await client.get(f"/api/v1/projects/{project.key}/members/", headers=manager)
        assert resp.status_code == 200, resp.text
        assert "$git" not in resp.text

        page = await client.get(f"/projects/{project.identifier}/settings/", headers=manager)
        assert "$git" not in page.text
