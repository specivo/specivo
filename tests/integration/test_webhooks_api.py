"""Outgoing webhook API: secrets shown once, the delivery log, the test event, and emission from MCP."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.webhook import Webhook, WebhookDelivery
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_URL = "https://hooks.example.com/specivo"
_APPLY_ASYNC = "specivo.tasks.webhooks.deliver_webhook.apply_async"


@pytest_asyncio.fixture
async def project(db_session: AsyncSession):
    proj = ProjectFactory.build(key="WHAPI", name="Webhook API", identifier="whapi", is_public=False)
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
    role = Role(name=f"whapi-{user.login}", permissions=permissions)
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient, db_session: AsyncSession):
    await _user(db_session, "whapi_admin", admin=True)
    return await _token(client, "whapi_admin")


async def _create(client: AsyncClient, project, headers, **body) -> dict:
    payload = {"url": _URL, "events": ["issue.created"], **body}
    resp = await client.post(f"/api/v1/projects/{project.key}/webhooks/", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCrud:
    async def test_create_generates_secret_shown_once(self, client, db_session, project, admin_headers):
        body = await _create(client, project, admin_headers, include_private=True)
        assert len(body["secret"]) == 64
        assert body["include_private"] is True

        stored = (await db_session.execute(select(Webhook.secret).where(Webhook.id == body["id"]))).scalar_one()
        assert stored != body["secret"]
        assert secret_box.decrypt(stored, "webhook-secret") == body["secret"]

        listed = await client.get(f"/api/v1/projects/{project.key}/webhooks/", headers=admin_headers)
        assert listed.status_code == 200
        assert "secret" not in listed.json()[0]

    async def test_update_without_secret_change_returns_no_secret(self, client, project, admin_headers):
        body = await _create(client, project, admin_headers)
        resp = await client.patch(
            f"/api/v1/projects/{project.key}/webhooks/{body['id']}/",
            json={"is_active": False, "events": ["issue.updated", "wiki.page.updated"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["secret"] is None
        assert resp.json()["is_active"] is False
        assert resp.json()["events"] == ["issue.updated", "wiki.page.updated"]

    async def test_rotate_secret(self, client, project, admin_headers):
        body = await _create(client, project, admin_headers)
        resp = await client.patch(
            f"/api/v1/projects/{project.key}/webhooks/{body['id']}/",
            json={"rotate_secret": True},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["secret"] and resp.json()["secret"] != body["secret"]

    async def test_unknown_event_rejected(self, client, project, admin_headers):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/webhooks/",
            json={"url": _URL, "events": ["issue.exploded"]},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    async def test_delete(self, client, project, admin_headers):
        body = await _create(client, project, admin_headers)
        resp = await client.delete(f"/api/v1/projects/{project.key}/webhooks/{body['id']}/", headers=admin_headers)
        assert resp.status_code == 204


class TestDeliveriesAndTest:
    async def test_test_event_creates_delivery_listed_newest_first(self, client, project, admin_headers):
        body = await _create(client, project, admin_headers)
        base = f"/api/v1/projects/{project.key}/webhooks/{body['id']}"
        with patch(_APPLY_ASYNC) as apply_async:
            first = await client.post(f"{base}/test/", headers=admin_headers)
            second = await client.post(f"{base}/test/", headers=admin_headers)
        assert first.status_code == 202, first.text
        assert first.json()["event"] == "ping"
        assert first.json()["status"] == "pending"
        assert apply_async.call_count == 2

        resp = await client.get(f"{base}/deliveries/", headers=admin_headers)
        assert resp.status_code == 200
        page = resp.json()
        assert page["total_count"] == 2
        assert [d["id"] for d in page["items"]] == [second.json()["id"], first.json()["id"]]

        paged = await client.get(f"{base}/deliveries/?limit=1&offset=1", headers=admin_headers)
        assert [d["id"] for d in paged.json()["items"]] == [first.json()["id"]]

    async def test_other_projects_webhook_is_not_found(self, client, db_session, project, admin_headers):
        body = await _create(client, project, admin_headers)
        other = ProjectFactory.build(key="WHAPI2", name="Other", identifier="whapi2")
        db_session.add(other)
        await db_session.commit()
        resp = await client.get(f"/api/v1/projects/WHAPI2/webhooks/{body['id']}/deliveries/", headers=admin_headers)
        assert resp.status_code == 404


class TestPermissions:
    async def test_non_member_gets_404(self, client, db_session, project, admin_headers):
        body = await _create(client, project, admin_headers)
        await _user(db_session, "whapi_outsider")
        headers = await _token(client, "whapi_outsider")
        base = f"/api/v1/projects/{project.key}/webhooks/{body['id']}"
        assert (await client.get(f"{base}/deliveries/", headers=headers)).status_code == 404
        assert (await client.post(f"{base}/test/", headers=headers)).status_code == 404

    async def test_member_without_manage_project_is_refused(self, client, db_session, project, admin_headers):
        body = await _create(client, project, admin_headers)
        user = await _user(db_session, "whapi_viewer")
        await _member(db_session, user, project, ["view_issues"])
        headers = await _token(client, "whapi_viewer")
        base = f"/api/v1/projects/{project.key}/webhooks/{body['id']}"
        with patch(_APPLY_ASYNC) as apply_async:
            assert (await client.get(f"{base}/deliveries/", headers=headers)).status_code in (403, 404)
            assert (await client.post(f"{base}/test/", headers=headers)).status_code in (403, 404)
            resp = await client.post(
                f"/api/v1/projects/{project.key}/webhooks/", json={"url": _URL, "events": ["ping"]}, headers=headers
            )
            assert resp.status_code in (403, 404, 422)
        apply_async.assert_not_called()

    async def test_member_without_manage_project_cannot_list(self, client, db_session, project, admin_headers):
        # Receiver URLs often carry tokens, so even the list is for managers only.
        await _create(client, project, admin_headers)
        user = await _user(db_session, "whapi_lister")
        await _member(db_session, user, project, ["view_issues"])
        headers = await _token(client, "whapi_lister")
        resp = await client.get(f"/api/v1/projects/{project.key}/webhooks/", headers=headers)
        assert resp.status_code in (403, 404)

    async def test_invalid_port_is_refused_on_save(self, client, project, admin_headers):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/webhooks/",
            json={"url": "https://hooks.example.com:99999/in", "events": ["issue.created"]},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    async def test_manager_may_use_the_log(self, client, db_session, project, admin_headers):
        body = await _create(client, project, admin_headers)
        user = await _user(db_session, "whapi_manager")
        await _member(db_session, user, project, ["view_issues", "manage_project"])
        headers = await _token(client, "whapi_manager")
        resp = await client.get(f"/api/v1/projects/{project.key}/webhooks/{body['id']}/deliveries/", headers=headers)
        assert resp.status_code == 200


class TestEmissionSurfaces:
    async def test_issue_created_over_rest_emits(self, client, db_session, project, admin_headers):
        status = StatusFactory.build(name="New", position=1, category="backlog")
        db_session.add(status)
        await db_session.flush()
        tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
        db_session.add_all([tracker, PriorityFactory.build(name="Normal", is_default=True, position=2)])
        await db_session.commit()
        hook = await _create(client, project, admin_headers)

        with patch(_APPLY_ASYNC) as apply_async:
            resp = await client.post(
                f"/api/v1/projects/{project.key}/issues/",
                json={"project_key": project.key, "tracker_id": tracker.id, "subject": "From REST"},
                headers=admin_headers,
            )
        assert resp.status_code == 201, resp.text
        rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
        assert [(r.webhook_id, r.event) for r in rows] == [(hook["id"], "issue.created")]
        # The payload is the REST representation of the issue.
        assert rows[0].payload["data"]["issue"] == resp.json()
        apply_async.assert_called_once_with(args=[rows[0].id, 0], retry=False)

    async def test_issue_created_over_mcp_emits(self, client, db_session, project, admin_headers):
        from specivo.mcp.tools import _create_issue

        status = StatusFactory.build(name="New", position=1, category="backlog")
        db_session.add(status)
        await db_session.flush()
        tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
        db_session.add_all([tracker, PriorityFactory.build(name="Normal", is_default=True, position=2)])
        await db_session.commit()
        hook = await _create(client, project, admin_headers)
        admin = (await db_session.execute(select(User).where(User.login == "whapi_admin"))).scalar_one()

        await _create_issue(db_session, admin, project.key, tracker.id, "From MCP")
        rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
        assert [(r.webhook_id, r.event) for r in rows] == [(hook["id"], "issue.created")]
        assert rows[0].payload["data"]["issue"]["subject"] == "From MCP"
