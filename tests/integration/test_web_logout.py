"""Web sign-out revokes the session on the server, not just the cookies.

``POST /logout/`` must:

- delete the refresh-token row behind the ``refresh_token`` cookie, so a copied
  cookie can no longer mint access tokens;
- blocklist the current access token's ``jti`` for its remaining lifetime;
- be CSRF-protected, so another page cannot sign the user out.

``GET /logout/`` (a bookmark, an old link) only renders a "Sign out?"
confirmation form and changes nothing.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.user import User
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _sign_in(client: AsyncClient, login: str) -> tuple[str, str]:
    """Sign in the way the web login form does and keep the cookies on *client*.

    Returns ``(access_token, refresh_token)``.
    """
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    client.cookies.set("access_token", body["access_token"])
    client.cookies.set("refresh_token", body["refresh_token"])
    return body["access_token"], body["refresh_token"]


def _set_cookie_value(resp, name: str) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


def _keep_csrf_cookie(client: AsyncClient, resp) -> str:
    """Store the CSRF cookie *resp* set as a plain cookie on *client*.

    Outside debug mode the cookie is ``Secure``, which httpx will not send back
    over the plain-http test transport, so it is copied over by hand.
    """
    token = _set_cookie_value(resp, "csrf_token")
    if token:
        client.cookies.delete("csrf_token")
        client.cookies.set("csrf_token", token)
    return token


async def _csrf_token(client: AsyncClient) -> str:
    """Load a page so the CSRF middleware issues its cookie, and return it."""
    resp = await client.get("/login/")
    assert resp.status_code == 200
    token = _keep_csrf_cookie(client, resp)
    assert token
    return token


async def _session_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id))
    return int(result.scalar_one())


async def _refresh_with(client: AsyncClient, refresh_token: str):
    client.cookies.clear()
    return await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh_token})


async def _call_api_with(client: AsyncClient, access_token: str):
    client.cookies.clear()
    return await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access_token}"})


# ---------------------------------------------------------------------------
# POST /logout/ revokes the session
# ---------------------------------------------------------------------------


class TestPostLogoutRevokes:
    async def test_old_refresh_cookie_can_no_longer_refresh(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_refresh")
        _access, refresh = await _sign_in(client, user.login)
        csrf = await _csrf_token(client)

        resp = await client.post("/logout/", data={"csrf_token": csrf}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers["location"] == "/login/?logged_out=1"

        replay = await _refresh_with(client, refresh)
        assert replay.status_code == 401
        assert replay.json()["errors"][0]["code"] == "auth_refresh_expired"

    async def test_old_access_token_is_rejected(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_access")
        access, _refresh = await _sign_in(client, user.login)
        csrf = await _csrf_token(client)

        # Sanity: the token works before signing out.
        before = await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access}"})
        assert before.status_code == 200

        resp = await client.post("/logout/", data={"csrf_token": csrf}, follow_redirects=False)
        assert resp.status_code in (302, 303)

        after = await _call_api_with(client, access)
        assert after.status_code == 401
        assert after.json()["errors"][0]["code"] == "auth_token_revoked"

    async def test_session_row_is_deleted(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_row")
        await _sign_in(client, user.login)
        csrf = await _csrf_token(client)
        assert await _session_count(db_session, user.id) == 1

        await client.post("/logout/", data={"csrf_token": csrf}, follow_redirects=False)

        assert await _session_count(db_session, user.id) == 0

    async def test_cookies_are_cleared(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_cookies")
        await _sign_in(client, user.login)
        csrf = await _csrf_token(client)

        resp = await client.post("/logout/", data={"csrf_token": csrf}, follow_redirects=False)

        cleared = {
            value.split("=", 1)[0]
            for key, value in resp.headers.multi_items()
            if key.lower() == "set-cookie" and ("Max-Age=0" in value or "max-age=0" in value.lower())
        }
        assert {"access_token", "refresh_token"} <= cleared

    async def test_other_sessions_of_the_user_survive(self, client: AsyncClient, db_session: AsyncSession):
        """Signing out one browser is not "sign out everywhere"."""
        user = await _create_user(db_session, "weblogout_other")
        _a1, _r1 = await _sign_in(client, user.login)
        other_access, other_refresh = await _sign_in(client, user.login)
        # The browser that signs out holds the first session's cookies.
        client.cookies.set("access_token", _a1)
        client.cookies.set("refresh_token", _r1)
        csrf = await _csrf_token(client)

        await client.post("/logout/", data={"csrf_token": csrf}, follow_redirects=False)

        assert await _session_count(db_session, user.id) == 1
        ok = await _call_api_with(client, other_access)
        assert ok.status_code == 200
        ok = await _refresh_with(client, other_refresh)
        assert ok.status_code == 200

    async def test_works_with_only_the_refresh_cookie(self, client: AsyncClient, db_session: AsyncSession):
        """A remember-me browser whose short-lived access cookie already expired."""
        user = await _create_user(db_session, "weblogout_refresh_only")
        _access, refresh = await _sign_in(client, user.login)
        csrf = await _csrf_token(client)
        client.cookies.delete("access_token")

        resp = await client.post("/logout/", data={"csrf_token": csrf}, follow_redirects=False)
        assert resp.status_code in (302, 303)

        replay = await _refresh_with(client, refresh)
        assert replay.status_code == 401

    async def test_without_any_session_just_redirects(self, client: AsyncClient):
        resp = await client.post("/logout/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers["location"] == "/login/?logged_out=1"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


class TestPostLogoutCsrf:
    async def test_post_without_csrf_token_is_refused(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_nocsrf")
        access, refresh = await _sign_in(client, user.login)
        await _csrf_token(client)

        resp = await client.post("/logout/", follow_redirects=False)
        assert resp.status_code == 403

        # Nothing was revoked.
        assert await _session_count(db_session, user.id) == 1
        assert (await _call_api_with(client, access)).status_code == 200

    async def test_post_with_wrong_csrf_token_is_refused(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_badcsrf")
        await _sign_in(client, user.login)
        await _csrf_token(client)

        resp = await client.post("/logout/", data={"csrf_token": "forged.token"}, follow_redirects=False)
        assert resp.status_code == 403
        assert await _session_count(db_session, user.id) == 1

    async def test_refresh_cookie_alone_still_needs_csrf(self, client: AsyncClient, db_session: AsyncSession):
        """The refresh cookie signs the browser in silently, so it is a session cookie too."""
        user = await _create_user(db_session, "weblogout_refresh_nocsrf")
        await _sign_in(client, user.login)
        client.cookies.delete("access_token")

        resp = await client.post("/logout/", follow_redirects=False)
        assert resp.status_code == 403
        assert await _session_count(db_session, user.id) == 1


# ---------------------------------------------------------------------------
# GET /logout/ only asks
# ---------------------------------------------------------------------------


class TestGetLogoutConfirms:
    async def test_get_renders_confirmation_and_revokes_nothing(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_get")
        access, refresh = await _sign_in(client, user.login)

        resp = await client.get("/logout/", follow_redirects=False)
        assert resp.status_code == 200
        assert "Sign out?" in resp.text
        assert 'action="/logout/"' in resp.text
        assert 'method="post"' in resp.text
        assert 'name="csrf_token"' in resp.text
        # No cookie is cleared by a GET.
        for key, value in resp.headers.multi_items():
            if key.lower() == "set-cookie":
                assert not value.startswith(("access_token=", "refresh_token="))

        assert await _session_count(db_session, user.id) == 1
        assert (await _call_api_with(client, access)).status_code == 200
        assert (await _refresh_with(client, refresh)).status_code == 200

    async def test_confirmation_form_carries_the_cookie_csrf_token(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_get_token")
        await _sign_in(client, user.login)
        csrf = await _csrf_token(client)

        resp = await client.get("/logout/")
        assert f'value="{csrf}"' in resp.text

    async def test_confirmation_form_submits(self, client: AsyncClient, db_session: AsyncSession):
        """A first visit has no CSRF cookie yet: the token in the form is the one the response sets."""
        user = await _create_user(db_session, "weblogout_get_submit")
        _access, refresh = await _sign_in(client, user.login)
        client.cookies.delete("csrf_token")

        page = await client.get("/logout/")
        assert page.status_code == 200
        cookie_token = _keep_csrf_cookie(client, page)
        assert cookie_token
        assert f'value="{cookie_token}"' in page.text

        resp = await client.post("/logout/", data={"csrf_token": cookie_token}, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert (await _refresh_with(client, refresh)).status_code == 401

    async def test_get_without_session_redirects_to_login(self, client: AsyncClient):
        resp = await client.get("/logout/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login/"


# ---------------------------------------------------------------------------
# Sign-out controls are forms, not links
# ---------------------------------------------------------------------------


class TestSignOutControls:
    async def test_header_sign_out_is_a_post_form(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_header")
        await _sign_in(client, user.login)

        resp = await client.get("/my/profile/")
        assert resp.status_code == 200
        assert 'href="/logout/"' not in resp.text
        assert 'action="/logout/"' in resp.text

    async def test_login_page_widget_sign_out_is_a_post_form(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, "weblogout_loginpage")
        await _sign_in(client, user.login)

        resp = await client.get("/login/")
        assert resp.status_code == 200
        assert 'href="/logout/"' not in resp.text
        assert 'action="/logout/"' in resp.text


async def test_confirmation_page_is_translated(client: AsyncClient, db_session: AsyncSession):
    user = UserFactory.build(login="weblogout_thai", language="th")
    db_session.add(user)
    await db_session.commit()
    await _sign_in(client, user.login)

    resp = await client.get("/logout/")
    assert resp.status_code == 200
    assert "ออกจากระบบหรือไม่?" in resp.text
