"""Repository integration API: manage_project everywhere, the secret shown once, rotation, validation."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.models.member import Member, MemberRole
from specivo.models.repository import RepositoryDelivery, RepositoryIntegration
from specivo.models.role import Role
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_PURPOSE = "repo-integration-secret"
_BODY = {
    "provider": "github",
    "repo_full_name": "example-org/widgets",
    "repo_url": "https://github.example.com/example-org/widgets",
}


@pytest_asyncio.fixture
async def project(db_session: AsyncSession):
    proj = ProjectFactory.build(key="RPAPI", name="Repository API", identifier="rpapi", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


async def _token(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _user(db_session: AsyncSession, login: str, *, admin: bool = False):
    factory = AdminUserFactory if admin else UserFactory
    user = factory.build(login=login, status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _member(db_session: AsyncSession, user, project, permissions: list[str]) -> None:
    role = Role(name=f"rpapi-{user.login}", permissions=permissions)
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()


@pytest_asyncio.fixture
async def manager_headers(client: AsyncClient, db_session: AsyncSession, project):
    user = await _user(db_session, "rpapi_manager")
    await _member(db_session, user, project, ["view_issues", "manage_project"])
    return await _token(client, "rpapi_manager")


async def _user_id(db_session: AsyncSession, login: str) -> int:
    from specivo.models.user import User

    return (await db_session.execute(select(User.id).where(User.login == login))).scalar_one()


def _base(project) -> str:
    return f"/api/v1/projects/{project.key}/repositories/"


async def _create(client: AsyncClient, project, headers, **body) -> dict:
    resp = await client.post(_base(project), json={**_BODY, **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCrud:
    async def test_create_returns_hook_url_and_secret_once(self, client, db_session, project, manager_headers):
        body = await _create(client, project, manager_headers)
        assert len(body["secret"]) == 64
        assert body["hook_url"].endswith(f"/hooks/github/{body['public_id']}/")
        assert body["scope"] == "project"
        assert body["default_branch"] == "main"
        assert body["last_delivery_at"] is None
        assert body["settings"] == {
            "commands_enabled": False,
            "time_logging_enabled": False,
            "fixing_status_id": None,
            "merge_status_id": None,
            "merge_automation_enabled": False,
        }

        stored = (
            await db_session.execute(
                select(RepositoryIntegration.secret_encrypted).where(RepositoryIntegration.id == body["id"])
            )
        ).scalar_one()
        assert stored != body["secret"]
        assert secret_box.decrypt(stored, _PURPOSE) == body["secret"]

        listed = await client.get(_base(project), headers=manager_headers)
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()] == [body["id"]]
        assert "secret" not in listed.json()[0]
        one = await client.get(f"{_base(project)}{body['id']}/", headers=manager_headers)
        assert one.status_code == 200
        assert "secret" not in one.json()

    async def test_update_returns_no_secret(self, client, project, manager_headers):
        body = await _create(client, project, manager_headers)
        resp = await client.patch(
            f"{_base(project)}{body['id']}/",
            json={"is_active": False, "scope": "project_and_subprojects", "repo_url": "https://example.org/w"},
            headers=manager_headers,
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert "secret" not in out
        assert (out["is_active"], out["scope"], out["repo_url"]) == (
            False,
            "project_and_subprojects",
            "https://example.org/w",
        )

    async def test_rotate_replaces_secret(self, client, db_session, project, manager_headers):
        body = await _create(client, project, manager_headers)
        resp = await client.post(f"{_base(project)}{body['id']}/rotate-secret/", headers=manager_headers)
        assert resp.status_code == 200, resp.text
        new_secret = resp.json()["secret"]
        assert new_secret and new_secret != body["secret"]
        stored = (
            await db_session.execute(
                select(RepositoryIntegration.secret_encrypted).where(RepositoryIntegration.id == body["id"])
            )
        ).scalar_one()
        assert secret_box.decrypt(stored, _PURPOSE) == new_secret

    async def test_delete_removes_integration_and_log(self, client, db_session, project, manager_headers):
        body = await _create(client, project, manager_headers)
        db_session.add(
            RepositoryDelivery(integration_id=body["id"], delivery_id="d-1", event="ping", status="processed")
        )
        await db_session.commit()
        resp = await client.delete(f"{_base(project)}{body['id']}/", headers=manager_headers)
        assert resp.status_code == 204
        assert (await db_session.get(RepositoryIntegration, body["id"])) is None
        assert (await db_session.execute(select(RepositoryDelivery))).scalars().all() == []

    async def test_deliveries_newest_first(self, client, db_session, project, manager_headers):
        body = await _create(client, project, manager_headers)
        from datetime import timedelta

        from specivo.core.utils import utcnow

        now = utcnow()
        for i in range(3):
            db_session.add(
                RepositoryDelivery(
                    integration_id=body["id"],
                    delivery_id=f"d-{i}",
                    event="push",
                    status="processed",
                    received_at=now - timedelta(minutes=3 - i),
                )
            )
        await db_session.commit()
        resp = await client.get(f"{_base(project)}{body['id']}/deliveries/?limit=2", headers=manager_headers)
        assert resp.status_code == 200
        page = resp.json()
        assert page["total_count"] == 3
        assert [d["delivery_id"] for d in page["items"]] == ["d-2", "d-1"]
        assert "payload" not in page["items"][0]

    async def test_same_repo_twice_is_a_conflict(self, client, project, manager_headers):
        await _create(client, project, manager_headers)
        resp = await client.post(
            _base(project), json={**_BODY, "repo_full_name": "Example-Org/Widgets"}, headers=manager_headers
        )
        assert resp.status_code == 409
        assert resp.json()["errors"][0]["code"] == "repository_already_connected"
        # The same repository on another provider is a different integration.
        await _create(client, project, manager_headers, provider="gitea")

    async def test_same_repo_in_another_project(self, client, db_session, project, manager_headers):
        first = await _create(client, project, manager_headers)
        other = ProjectFactory.build(key="RPAPJ", name="Other", identifier="rpapj", is_public=False)
        db_session.add(other)
        await db_session.commit()
        user = (await db_session.execute(select(Member).where(Member.project_id == project.id))).scalar_one()
        role = Role(name="rpapi-other", permissions=["manage_project"])
        db_session.add(role)
        await db_session.flush()
        member = Member(user_id=user.user_id, project_id=other.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
        await db_session.commit()
        second = await _create(client, other, manager_headers)
        assert second["public_id"] != first["public_id"]
        assert second["secret"] != first["secret"]

    async def test_other_projects_integration_is_not_found(self, client, db_session, project, manager_headers):
        other = ProjectFactory.build(key="RPAPK", name="Other K", identifier="rpapk", is_public=False)
        db_session.add(other)
        await db_session.commit()
        integration = RepositoryIntegration(
            project_id=other.id,
            provider="github",
            repo_full_name="example-org/other",
            repo_url="https://example.com/other",
            secret_encrypted=secret_box.encrypt("x" * 64, _PURPOSE),
        )
        db_session.add(integration)
        await db_session.commit()
        base = f"{_base(project)}{integration.id}/"
        assert (await client.get(base, headers=manager_headers)).status_code == 404
        assert (await client.patch(base, json={"is_active": False}, headers=manager_headers)).status_code == 404
        assert (await client.post(f"{base}rotate-secret/", headers=manager_headers)).status_code == 404
        assert (await client.delete(base, headers=manager_headers)).status_code == 404


class TestResponsibleUser:
    """Whoever last edits an integration or rotates its secret is the user whose rights bound its scope."""

    async def test_edit_and_rotate_record_the_actor(self, client, db_session, project, manager_headers):
        admin = await _user(db_session, "rpapi_owner_admin", admin=True)
        admin_headers = await _token(client, "rpapi_owner_admin")
        body = await _create(client, project, admin_headers, scope="project_and_subprojects")
        integration = await db_session.get(RepositoryIntegration, body["id"])
        assert integration.created_by_id == admin.id

        manager = await _user_id(db_session, "rpapi_manager")
        resp = await client.patch(f"{_base(project)}{body['id']}/", json={"is_active": True}, headers=manager_headers)
        assert resp.status_code == 200
        await db_session.refresh(integration)
        assert integration.created_by_id == manager

        resp = await client.post(f"{_base(project)}{body['id']}/rotate-secret/", headers=admin_headers)
        assert resp.status_code == 200
        await db_session.refresh(integration)
        assert integration.created_by_id == admin.id


class TestValidation:
    @pytest.mark.parametrize(
        "override",
        [
            {"provider": "bitbucket"},
            {"repo_full_name": "widgets"},
            {"repo_full_name": "example-org/../widgets"},
            {"repo_full_name": "example org/widgets"},
            {"repo_url": "javascript:alert(1)"},
            {"repo_url": "ftp://example.com/widgets"},
            {"scope": "everything"},
            {"settings": {"commands_enabled": "yes"}},
            {"settings": {"fixing_status_id": "3"}},
            {"settings": {"fixing_status_id": 0}},
            {"settings": {"time_logging_enabled": 1}},
            {"settings": {"unknown_switch": True}},
        ],
    )
    async def test_rejected(self, client, project, manager_headers, override):
        resp = await client.post(_base(project), json={**_BODY, **override}, headers=manager_headers)
        assert resp.status_code == 422, resp.text

    async def test_gitlab_subgroup_and_settings_accepted(self, client, project, manager_headers):
        body = await _create(
            client,
            project,
            manager_headers,
            provider="gitlab",
            repo_full_name="example-group/sub.group/widgets_2",
            settings={"commands_enabled": True, "fixing_status_id": 3},
        )
        assert body["repo_full_name"] == "example-group/sub.group/widgets_2"
        assert body["settings"]["commands_enabled"] is True
        assert body["settings"]["fixing_status_id"] == 3


class TestPermissions:
    async def test_non_member_gets_404(self, client, db_session, project, manager_headers):
        body = await _create(client, project, manager_headers)
        await _user(db_session, "rpapi_outsider")
        headers = await _token(client, "rpapi_outsider")
        base = f"{_base(project)}{body['id']}/"
        assert (await client.get(_base(project), headers=headers)).status_code == 404
        assert (await client.post(_base(project), json=_BODY, headers=headers)).status_code == 404
        assert (await client.get(base, headers=headers)).status_code == 404
        assert (await client.patch(base, json={"is_active": False}, headers=headers)).status_code == 404
        assert (await client.delete(base, headers=headers)).status_code == 404
        assert (await client.post(f"{base}rotate-secret/", headers=headers)).status_code == 404
        assert (await client.get(f"{base}deliveries/", headers=headers)).status_code == 404

    async def test_member_without_manage_project_is_refused(self, client, db_session, project, manager_headers):
        body = await _create(client, project, manager_headers)
        user = await _user(db_session, "rpapi_viewer")
        await _member(db_session, user, project, ["view_issues", "edit_issues"])
        headers = await _token(client, "rpapi_viewer")
        base = f"{_base(project)}{body['id']}/"
        assert (await client.get(_base(project), headers=headers)).status_code == 403
        assert (await client.post(_base(project), json=_BODY, headers=headers)).status_code == 403
        assert (await client.get(base, headers=headers)).status_code == 403
        assert (await client.patch(base, json={"is_active": False}, headers=headers)).status_code == 403
        assert (await client.delete(base, headers=headers)).status_code == 403
        assert (await client.post(f"{base}rotate-secret/", headers=headers)).status_code == 403
        assert (await client.get(f"{base}deliveries/", headers=headers)).status_code == 403
        stored = await db_session.get(RepositoryIntegration, body["id"])
        await db_session.refresh(stored)
        assert stored.is_active is True

    async def test_admin_may_manage(self, client, db_session, project):
        await _user(db_session, "rpapi_admin", admin=True)
        headers = await _token(client, "rpapi_admin")
        await _create(client, project, headers)

    async def test_unauthenticated_is_refused(self, client, project):
        assert (await client.get(_base(project))).status_code == 401
