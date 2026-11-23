"""Managing Hermes event receivers: people their own, administrators anyone's.

- a person creates a receiver and gets its token, secret, URL and the
  ``hooks.outbound`` block once; the list never answers them again;
- renaming, switching off and on, rotating (a new secret, answered once) and
  deleting; another account's receiver is 404;
- every change is audited with ids and names, never the token or the secret;
- a key limited to some projects cannot manage receivers;
- an administrator manages a service account's receivers (the API-key gate on
  minting is in ``test_admin_api_key_gates.py``);
- the pages: ``/my/hermes-receivers/`` and the panel on Admin > Users.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.models.hermes_receiver import HermesReceiver
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.api_key_service import ApiKeyService
from specivo.services.hermes_receiver_service import HERMES_RECEIVER_SECRET_PURPOSE, SUBSCRIBED_EVENTS
from tests.factories.project import ProjectFactory
from tests.factories.user import ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MY = "/api/v1/my/hermes-receivers/"


async def _audit_rows(db: AsyncSession, prefix: str = "hermes_receiver_") -> list[SecurityAuditLog]:
    stmt = (
        select(SecurityAuditLog)
        .where(SecurityAuditLog.event_type.startswith(prefix))
        .order_by(SecurityAuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(stmt)).scalars().all())


async def test_a_person_creates_a_receiver_and_sees_its_credentials_once(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await auth_client.post(MY, json={"name": "  laptop profile  "})

    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "laptop profile"
    assert created["user_id"] == auth_client.state.user.id  # type: ignore[attr-defined]
    assert created["url"].endswith(f"/api/v1/hermes/events/{created['token']}/")
    assert created["secret_env"] == "SPECIVO_WEBHOOK_SECRET"
    config = created["hooks_config"]
    assert f"/api/v1/hermes/events/{created['token']}/" in config
    assert "secret_env: SPECIVO_WEBHOOK_SECRET" in config
    assert all(event in config for event in SUBSCRIBED_EVENTS)
    assert created["secret"] not in config

    stored = await db_session.get(HermesReceiver, created["id"])
    assert stored.secret != created["secret"]
    assert secret_box.decrypt(stored.secret, HERMES_RECEIVER_SECRET_PURPOSE) == created["secret"]

    listed = (await auth_client.get(MY)).json()
    assert [r["id"] for r in listed] == [created["id"]]
    assert "token" not in listed[0] and "secret" not in listed[0]

    [row] = await _audit_rows(db_session)
    assert row.event_type == "hermes_receiver_created"
    assert row.details["receiver_id"] == created["id"]
    assert created["secret"] not in str(row.details)
    assert created["token"] not in str(row.details)


async def test_rename_switch_rotate_and_delete(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    created = (await auth_client.post(MY, json={"name": "first"})).json()
    url = f"{MY}{created['id']}/"

    renamed = await auth_client.patch(url, json={"name": "second", "is_active": False})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "second"
    assert renamed.json()["is_active"] is False
    assert renamed.json()["secret"] is None

    rotated = (await auth_client.patch(url, json={"rotate_secret": True, "is_active": True})).json()
    assert rotated["is_active"] is True
    assert rotated["secret"] and rotated["secret"] != created["secret"]
    stored = await db_session.get(HermesReceiver, created["id"])
    await db_session.refresh(stored)
    assert secret_box.decrypt(stored.secret, HERMES_RECEIVER_SECRET_PURPOSE) == rotated["secret"]

    assert (await auth_client.delete(url)).status_code == 204
    assert (await auth_client.get(MY)).json() == []

    rows = await _audit_rows(db_session)
    assert [r.event_type for r in rows] == [
        "hermes_receiver_created",
        "hermes_receiver_updated",
        "hermes_receiver_updated",
        "hermes_receiver_deleted",
    ]
    assert rows[1].details["changes"] == ["name", "is_active"]
    assert rows[2].details["secret_rotated"] is True
    assert all(rotated["secret"] not in str(r.details) for r in rows)


async def test_another_accounts_receiver_is_not_found(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    from specivo.services.hermes_receiver_service import HermesReceiverService

    other = UserFactory.build(login="hrecv_other", status="active")
    db_session.add(other)
    await db_session.flush()
    theirs = (await HermesReceiverService().create(db_session, owner=other, name="theirs", actor=other)).receiver
    await db_session.commit()

    assert (await auth_client.patch(f"{MY}{theirs.id}/", json={"rotate_secret": True})).status_code == 404
    assert (await auth_client.delete(f"{MY}{theirs.id}/")).status_code == 404
    assert (await auth_client.get(MY)).json() == []


async def test_blank_and_long_names_are_refused(auth_client: AsyncClient) -> None:
    assert (await auth_client.post(MY, json={"name": "   "})).status_code == 422
    assert (await auth_client.post(MY, json={"name": "n" * 101})).status_code == 422


async def test_a_project_limited_key_cannot_manage_receivers(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = auth_client.state.user  # type: ignore[attr-defined]
    project = ProjectFactory.build(key="HRLIM", identifier="hrlim", name="Limited", is_public=True)
    db_session.add(project)
    await db_session.flush()
    _, raw = await ApiKeyService().create_key(
        db_session, user_id=user.id, name="limited", scopes={"projects": [project.id]}, check_scope_access=False
    )
    await db_session.commit()
    transport = auth_client._transport
    assert isinstance(transport, ASGITransport)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {raw}"}
    ) as limited:
        assert (await limited.post(MY, json={"name": "nope"})).status_code == 403


async def test_an_admin_manages_a_service_accounts_receivers(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    robot = ServiceAccountFactory.build(login="hrecv_robot", status="active")
    db_session.add(robot)
    await db_session.commit()
    base = f"/api/v1/admin/users/{robot.id}/hermes-receivers/"

    created = await admin_client.post(base, json={"name": "robot host"})
    assert created.status_code == 201, created.text
    assert created.json()["user_id"] == robot.id
    assert created.json()["created_by_id"] == admin_client.state.user.id  # type: ignore[attr-defined]
    listed = (await admin_client.get(base)).json()
    assert [r["name"] for r in listed] == ["robot host"]
    assert (await admin_client.delete(f"{base}{listed[0]['id']}/")).status_code == 204

    [created_row, deleted_row] = await _audit_rows(db_session)
    assert created_row.user_id == admin_client.state.user.id  # type: ignore[attr-defined]
    assert created_row.details["target_login"] == "hrecv_robot"
    assert deleted_row.event_type == "hermes_receiver_deleted"


async def test_a_person_cannot_use_the_admin_routes(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    robot = ServiceAccountFactory.build(login="hrecv_robot2", status="active")
    db_session.add(robot)
    await db_session.commit()
    resp = await auth_client.post(f"/api/v1/admin/users/{robot.id}/hermes-receivers/", json={"name": "x"})
    assert resp.status_code == 403


async def test_the_my_page_and_the_admin_panel_render(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    page = await admin_client.get("/my/hermes-receivers/")
    assert page.status_code == 200, page.text
    assert 'data-api="/api/v1/my/hermes-receivers/"' in page.text
    assert 'x-data="hermesReceiverManager"' in page.text

    robot = ServiceAccountFactory.build(login="hrecv_robot3", status="active")
    db_session.add(robot)
    await db_session.commit()
    detail = await admin_client.get(f"/admin/users/{robot.id}/")
    assert detail.status_code == 200, detail.text
    assert f'data-api="/api/v1/admin/users/{robot.id}/hermes-receivers/"' in detail.text


async def test_the_my_page_needs_a_sign_in(client: AsyncClient) -> None:
    resp = await client.get("/my/hermes-receivers/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
