"""Admin deactivation and reactivation of an account, through the API and the admin page.

- ``POST /api/v1/admin/users/{id}/deactivate/``: the account's access token
  stops working on the next request, its refresh token cannot be used, its API
  keys are switched off (``spv_`` auth -> 401). Not on yourself, not on the
  last active admin (the service refuses it; through the API the caller is
  always another active admin).
- ``POST /api/v1/admin/users/{id}/reactivate/``: the account is active again,
  its keys stay inactive and the response says how many.
- The admin user page has the same two actions as plain forms and says that
  reactivation does not switch the keys back on.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import ApiKey, RefreshToken
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _create(db: AsyncSession, factory: Any = UserFactory, **fields: Any) -> User:
    fields.setdefault("email", f"{fields['login']}@example.com")
    user = factory.build(**fields)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _sign_in(client: AsyncClient, login: str) -> tuple[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    body = resp.json()
    return body["access_token"], body["refresh_token"]


async def _get_me(client: AsyncClient, credential: str) -> Response:
    client.cookies.clear()
    return await client.get("/api/v1/my/api-keys/", headers={"Authorization": f"Bearer {credential}"})


async def _active_keys(db: AsyncSession, user_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True))
        )
    )


def _code(resp: Response) -> str:
    return resp.json()["errors"][0]["code"]


class TestDeactivateApi:
    async def test_deactivation_cuts_off_sessions_and_keys(
        self, admin_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        target = await _create(db_session, login="deact_target")
        access, refresh = await _sign_in(client, target.login)
        _key, raw = await ApiKeyService().create_key(db_session, user_id=target.id, name="cli")
        await db_session.commit()
        assert (await _get_me(client, access)).status_code == 200
        assert (await _get_me(client, raw)).status_code == 200

        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/deactivate/")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "deactivated"
        assert body["sessions_revoked"] == 1
        assert body["api_keys_deactivated"] == 1

        after = await _get_me(client, access)
        assert after.status_code == 401
        assert _code(after) in {"auth_token_revoked", "auth_account_deactivated"}
        refreshed = await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh})
        assert refreshed.status_code == 401
        key_after = await _get_me(client, raw)
        assert key_after.status_code == 401
        assert await _active_keys(db_session, target.id) == 0
        rows = await db_session.scalar(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == target.id)
        )
        assert rows == 0

    async def test_service_account_deactivation_switches_off_its_keys(
        self, admin_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        agent = await _create(db_session, ServiceAccountFactory, login="deact_agent", password_hash=None)
        _key, raw = await ApiKeyService().create_key(db_session, user_id=agent.id, name="agent")
        await db_session.commit()

        resp = await admin_client.post(f"/api/v1/admin/users/{agent.id}/deactivate/")

        assert resp.status_code == 200, resp.text
        assert resp.json()["api_keys_deactivated"] == 1
        assert (await _get_me(client, raw)).status_code == 401

    async def test_cannot_deactivate_yourself(self, admin_client: AsyncClient) -> None:
        me = admin_client.state.user  # type: ignore[attr-defined]
        resp = await admin_client.post(f"/api/v1/admin/users/{me.id}/deactivate/")
        assert resp.status_code == 400
        assert _code(resp) == "self_deactivate"

    async def test_non_admin_is_refused(self, auth_client: AsyncClient, db_session: AsyncSession) -> None:
        target = await _create(db_session, login="deact_nonadmin")
        resp = await auth_client.post(f"/api/v1/admin/users/{target.id}/deactivate/")
        assert resp.status_code == 403

    async def test_unknown_user_is_404(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.post("/api/v1/admin/users/2000000000/deactivate/")
        assert resp.status_code == 404


class TestReactivateApi:
    async def test_reactivation_keeps_keys_inactive(
        self, admin_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        target = await _create(db_session, login="react_target")
        _key, raw = await ApiKeyService().create_key(db_session, user_id=target.id, name="cli")
        await db_session.commit()
        assert (await admin_client.post(f"/api/v1/admin/users/{target.id}/deactivate/")).status_code == 200

        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/reactivate/")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "active"
        assert body["inactive_api_keys"] == 1
        key_resp = await _get_me(client, raw)
        assert key_resp.status_code == 401
        assert _code(key_resp) == "api_key_inactive"
        # The person signs in again.
        access, _refresh = await _sign_in(client, target.login)
        assert (await _get_me(client, access)).status_code == 200


# ---------------------------------------------------------------------------
# Admin page (plain forms)
# ---------------------------------------------------------------------------


def _set_cookie_value(resp: Response, name: str) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


class _AdminBrowser:
    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.cookies: dict[str, str] = {}

    async def sign_in(self, login: str) -> None:
        resp = await self.client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text
        self.client.cookies.clear()
        self.cookies["access_token"] = resp.json()["access_token"]
        page = await self.get("/login/")
        self.cookies["csrf_token"] = _set_cookie_value(page, "csrf_token")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        cookies = {**self.cookies, **(extra or {})}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    async def get(self, url: str, cookies: dict[str, str] | None = None) -> Response:
        return await self.client.get(url, headers=self._headers(cookies))

    async def post(self, url: str) -> Response:
        return await self.client.post(url, data={"csrf_token": self.cookies["csrf_token"]}, headers=self._headers())


async def _admin_browser(client: AsyncClient, db: AsyncSession, login: str, **fields: Any) -> _AdminBrowser:
    await _create(db, AdminUserFactory, login=login, **fields)
    browser = _AdminBrowser(client)
    await browser.sign_in(login)
    return browser


class TestAdminPage:
    async def test_deactivate_and_reactivate_forms(self, client: AsyncClient, db_session: AsyncSession) -> None:
        browser = await _admin_browser(client, db_session, "deact_page_admin")
        target = await _create(db_session, login="deact_page_target")
        await ApiKeyService().create_key(db_session, user_id=target.id, name="cli")
        await db_session.commit()

        page = await browser.get(f"/admin/users/{target.id}/")
        assert page.status_code == 200
        assert f'action="/admin/users/{target.id}/deactivate/"' in page.text

        done = await browser.post(f"/admin/users/{target.id}/deactivate/")
        assert done.status_code == 303
        assert done.headers["location"].startswith(f"/admin/users/{target.id}/")
        await db_session.refresh(target)
        assert target.status == "deactivated"
        assert await _active_keys(db_session, target.id) == 0

        page = await browser.get(done.headers["location"])
        assert "signed out" in page.text
        assert f'action="/admin/users/{target.id}/reactivate/"' in page.text
        assert "API keys stay inactive" in page.text

        again = await browser.post(f"/admin/users/{target.id}/reactivate/")
        assert again.status_code == 303
        await db_session.refresh(target)
        assert target.status == "active"
        assert await _active_keys(db_session, target.id) == 0
        page = await browser.get(again.headers["location"])
        assert "API keys stay inactive" in page.text

    async def test_no_deactivate_button_on_your_own_page(self, client: AsyncClient, db_session: AsyncSession) -> None:
        browser = await _admin_browser(client, db_session, "deact_page_self")
        me = await db_session.scalar(select(User).where(User.login == "deact_page_self"))
        assert me is not None

        page = await browser.get(f"/admin/users/{me.id}/")
        assert f'action="/admin/users/{me.id}/deactivate/"' not in page.text

        refused = await browser.post(f"/admin/users/{me.id}/deactivate/")
        assert refused.status_code == 303
        await db_session.refresh(me)
        assert me.status == "active"

    async def test_page_messages_in_thai(self, client: AsyncClient, db_session: AsyncSession) -> None:
        browser = await _admin_browser(client, db_session, "deact_page_thai", language="th")
        target = await _create(db_session, login="deact_page_thai_target", status="deactivated")

        page = await browser.get(f"/admin/users/{target.id}/", cookies={"specivo_lang": "th"})

        assert page.status_code == 200
        assert "API keys stay inactive" not in page.text
        assert f'action="/admin/users/{target.id}/reactivate/"' in page.text
