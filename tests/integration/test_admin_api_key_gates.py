"""Admin API keys cannot mint credentials or sessions, nor reach the SSO configuration.

A leaked administrator API key keeps working for ordinary administration
(reading users, locking or deactivating an account, project and workflow
settings), but every route that hands out a way to sign in needs an
administrator signed in with a session. Otherwise the key could reset another
administrator's password, sign in with it and then change single sign-on, so a
session-only gate on the SSO routes alone would be bypassable.

Gated routes answer 403 ``api_key_not_allowed`` and change nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.testing.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration

NEW_PASSWORD = "brand-new-password-0001"


async def _user(db: AsyncSession, factory=UserFactory, **fields: Any) -> User:
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _key_headers(db: AsyncSession, user: User) -> dict[str, str]:
    _, raw = await ApiKeyService().create_key(db, user_id=user.id, name="admin-automation")
    await db.flush()
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
async def key_client(admin_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    """A client that authenticates as the fixture admin with an API key only (no cookies, no JWT)."""
    headers = await _key_headers(db_session, admin_client.state.user)  # type: ignore[attr-defined]
    transport = admin_client._transport
    assert isinstance(transport, ASGITransport)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        yield client


def _assert_refused(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["errors"][0]["code"] == "api_key_not_allowed"


# ---------------------------------------------------------------------------
# The escalation chain
# ---------------------------------------------------------------------------


async def test_key_cannot_reset_an_admin_password_and_take_over_sso(
    key_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
) -> None:
    other_admin = await _user(db_session, AdminUserFactory, status="active")

    # Step 1 fails: the key cannot set a password it knows.
    _assert_refused(
        await key_client.post(f"/api/v1/admin/users/{other_admin.id}/reset-password/", json={"password": NEW_PASSWORD})
    )
    # So step 2 (sign in with it) fails, and step 3 is never reachable with a session.
    login = await client.post("/api/v1/auth/login/", json={"login": other_admin.login, "password": NEW_PASSWORD})
    assert login.status_code == 401
    # And the key cannot do step 3 itself either.
    _assert_refused(
        await key_client.post(
            "/api/v1/admin/sso/providers/",
            json={
                "slug": "gate-rogue",
                "display_name": "Rogue IdP",
                "issuer": "https://rogue.example.com",
                "client_id": "specivo",
                "client_secret": "rogue-secret-value-0001",
            },
        )
    )


# ---------------------------------------------------------------------------
# Every gated route
# ---------------------------------------------------------------------------


async def test_reset_password_is_session_only(key_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session)
    before = target.password_hash
    _assert_refused(
        await key_client.post(f"/api/v1/admin/users/{target.id}/reset-password/", json={"password": NEW_PASSWORD})
    )
    await db_session.refresh(target)
    assert target.password_hash == before


async def test_create_user_with_a_password_is_session_only(key_client: AsyncClient) -> None:
    body = {"login": "gatepw", "email": "gate-pw@example.com", "display_name": "Gate", "password": NEW_PASSWORD}
    _assert_refused(await key_client.post("/api/v1/admin/users/", json=body))


async def test_key_cannot_create_a_person_and_take_it_over_by_forgot_password(
    key_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
) -> None:
    """No password, but an email the key holder controls: forgot-password would hand out a session."""
    from unittest.mock import patch

    from sqlalchemy import func, select

    body = {"login": "gatenopw", "email": "gate-nopw@example.com", "display_name": "Gate"}
    _assert_refused(await key_client.post("/api/v1/admin/users/", json=body))
    assert await db_session.scalar(select(func.count()).select_from(User).where(User.login == "gatenopw")) == 0

    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        resp = await client.post("/api/v1/auth/forgot-password/", json={"email": "gate-nopw@example.com"})
    assert resp.status_code == 202
    mock_delay.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "pending_verification"},
        {"is_admin": True},
        {"is_admin": True, "is_service_account": True},
    ],
    ids=["pending_person", "admin_person", "admin_service_account"],
)
async def test_create_person_or_admin_is_session_only(key_client: AsyncClient, extra: dict[str, Any]) -> None:
    body = {"login": "gateperson", "email": "gate-person@example.com", "display_name": "Gate", **extra}
    _assert_refused(await key_client.post("/api/v1/admin/users/", json=body))


async def test_create_service_account_stays_key_accessible(key_client: AsyncClient) -> None:
    """Automation provisions agent accounts with a key: no password, refused by password sign-in."""
    body = {"login": "gatebot", "email": "gate-bot@example.com", "display_name": "Gate bot", "is_service_account": True}
    resp = await key_client.post("/api/v1/admin/users/", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_service_account"] is True


async def test_create_person_works_with_a_session(admin_client: AsyncClient) -> None:
    body = {"login": "gatesession", "email": "gate-session@example.com", "display_name": "Gate"}
    resp = await admin_client.post("/api/v1/admin/users/", json=body)
    assert resp.status_code == 201, resp.text


async def test_unlock_is_session_only(key_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session, status="locked", locked_until=None)
    _assert_refused(await key_client.post(f"/api/v1/admin/users/{target.id}/unlock/"))
    await db_session.refresh(target)
    assert target.status == "locked"


async def test_reactivate_is_session_only(key_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session, status="deactivated")
    _assert_refused(await key_client.post(f"/api/v1/admin/users/{target.id}/reactivate/"))
    await db_session.refresh(target)
    assert target.status == "deactivated"


async def test_api_key_for_a_user_is_session_only(key_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session)
    _assert_refused(await key_client.post(f"/api/v1/admin/users/{target.id}/api-keys/", json={"name": "minted"}))
    keys = await ApiKeyService().list_keys(session=db_session, user_id=target.id)
    assert keys == []


#: Every admin route that needs a session, as (module, method, path). The kill
#: switch and credential broker routers are mounted by the enterprise edition
#: only, so they are checked here by their dependencies rather than over HTTP.
SESSION_ONLY_ROUTES: list[tuple[str, str, str]] = [
    ("specivo.api.v1.admin.users", "POST", "/admin/users/{user_id}/reset-password/"),
    ("specivo.api.v1.admin.users", "POST", "/admin/users/{user_id}/unlock/"),
    ("specivo.api.v1.admin.users", "POST", "/admin/users/{user_id}/reactivate/"),
    ("specivo.api.v1.admin.users", "POST", "/admin/users/{user_id}/api-keys/"),
    ("specivo.api.v1.admin.kill_switch", "POST", "/admin/unkill/agent/{user_id}/"),
    ("specivo.api.v1.admin.credentials", "POST", "/admin/credentials/issue/"),
    ("specivo.api.v1.admin.sso", "POST", "/admin/sso/providers/"),
    ("specivo.api.v1.admin.sso", "PATCH", "/admin/sso/providers/{provider_id}/"),
    ("specivo.api.v1.admin.sso", "DELETE", "/admin/sso/providers/{provider_id}/"),
    ("specivo.api.v1.admin.sso", "PUT", "/admin/sso/providers/{provider_id}/group-mappings/"),
    ("specivo.api.v1.admin.sso", "POST", "/admin/sso/providers/{provider_id}/group-mappings/"),
    ("specivo.api.v1.admin.sso", "DELETE", "/admin/sso/providers/{provider_id}/group-mappings/{mapping_id}/"),
    ("specivo.api.v1.admin.sso", "PATCH", "/admin/settings/authentication/"),
]


def _dependency_calls(dependant: Any) -> set[Any]:
    calls = {dependant.call}
    for sub in dependant.dependencies:
        calls |= _dependency_calls(sub)
    return calls


@pytest.mark.parametrize(("module", "method", "path"), SESSION_ONLY_ROUTES)
def test_session_only_routes_depend_on_the_gate(module: str, method: str, path: str) -> None:
    import importlib

    from fastapi.routing import APIRoute

    from specivo.api.v1.admin import require_admin_session_api

    router = importlib.import_module(module).router
    [route] = [r for r in router.routes if isinstance(r, APIRoute) and r.path == path and method in r.methods]
    assert require_admin_session_api in _dependency_calls(route.dependant)


async def test_gate_refuses_api_key_requests_only() -> None:
    from types import SimpleNamespace

    from specivo.api.v1.admin import require_admin_session_api
    from specivo.core.exceptions import AppError

    admin = SimpleNamespace(is_admin=True)
    by_session = SimpleNamespace(state=SimpleNamespace())
    assert require_admin_session_api(by_session, admin) is admin  # type: ignore[arg-type]
    by_key = SimpleNamespace(state=SimpleNamespace(api_key_id=7))
    with pytest.raises(AppError) as exc:
        require_admin_session_api(by_key, admin)  # type: ignore[arg-type]
    assert exc.value.code == "api_key_not_allowed"
    assert exc.value.status_code == 403


async def test_web_reactivate_form_refuses_a_key(key_client: AsyncClient, db_session: AsyncSession) -> None:
    """The web form is no side door: a Bearer key carries no session cookie and so no CSRF check."""
    target = await _user(db_session, status="deactivated")
    resp = await key_client.post(f"/admin/users/{target.id}/reactivate/", follow_redirects=False)
    assert resp.status_code == 403, resp.text
    await db_session.refresh(target)
    assert target.status == "deactivated"


# ---------------------------------------------------------------------------
# Still key-accessible: ordinary administration and incident response
# ---------------------------------------------------------------------------


async def test_ordinary_admin_routes_still_work_with_a_key(key_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session)
    assert (await key_client.get("/api/v1/admin/users/")).status_code == 200
    assert (await key_client.get(f"/api/v1/admin/users/{target.id}/api-keys/")).status_code == 200
    assert (await key_client.get("/api/v1/admin/sso/providers/")).status_code == 200
    # Taking access away stays automatable.
    resp = await key_client.post(f"/api/v1/admin/users/{target.id}/lock/", json={})
    assert resp.status_code == 200, resp.text
    resp = await key_client.post(f"/api/v1/admin/users/{target.id}/deactivate/")
    assert resp.status_code == 200, resp.text


async def test_the_same_routes_work_with_a_session(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await _user(db_session)
    resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/reset-password/", json={"password": NEW_PASSWORD})
    assert resp.status_code == 200, resp.text
    resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/api-keys/", json={"name": "minted"})
    assert resp.status_code == 201, resp.text
    assert TEST_PASSWORD  # the fixture admin signed in with it
