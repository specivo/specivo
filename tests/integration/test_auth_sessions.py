"""Session endpoints revoke access tokens immediately.

Every endpoint that ends a session (``DELETE /auth/sessions/{id}``,
``/auth/logout-all``, ``/auth/logout`` and the other-sessions sweep of
``/auth/change-password``) goes through ``SessionService.revoke``, so an access
token of a revoked session is refused on the very next request, not 15 minutes
later when it expires.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.user import User
from specivo.services.auth_service import _hash_token
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_NEW_PASSWORD = "Another-Strong-Passw0rd!"


async def _create_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, email=f"{login}@example.com")
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


async def _get_sessions(client: AsyncClient, access: str):
    client.cookies.clear()
    return await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access}"})


def _error_code(resp) -> str:
    return resp.json()["errors"][0]["code"]


async def _row_id(db: AsyncSession, refresh: str) -> int:
    row = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash_token(refresh)))
    assert row is not None
    return row.id


class TestRevokeOneSession:
    async def test_delete_session_kills_that_browsers_access_token(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _create_user(db_session, "authsess_delete")
        victim_access, victim_refresh = await _sign_in(client, user.login)
        my_access, _my_refresh = await _sign_in(client, user.login)
        assert (await _get_sessions(client, victim_access)).status_code == 200

        session_id = await _row_id(db_session, victim_refresh)
        resp = await client.delete(
            f"/api/v1/auth/sessions/{session_id}/", headers={"Authorization": f"Bearer {my_access}"}
        )
        assert resp.status_code == 204

        after = await _get_sessions(client, victim_access)
        assert after.status_code == 401
        assert _error_code(after) == "auth_token_revoked"
        assert (await _get_sessions(client, my_access)).status_code == 200

    async def test_sessions_list_shows_the_auth_method(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _create_user(db_session, "authsess_list")
        access, _refresh = await _sign_in(client, user.login)

        resp = await _get_sessions(client, access)
        assert resp.status_code == 200
        [session] = resp.json()
        assert session["auth_method"] == "password"
        assert session["provider_name"] is None


class TestLogoutAll:
    async def test_logout_all_kills_every_access_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _create_user(db_session, "authsess_all")
        tokens = [await _sign_in(client, user.login) for _ in range(3)]

        resp = await client.post("/api/v1/auth/logout-all/", headers={"Authorization": f"Bearer {tokens[0][0]}"})
        assert resp.status_code == 200
        assert resp.json()["revoked_count"] == 3

        for access, refresh in tokens:
            after = await _get_sessions(client, access)
            assert after.status_code == 401
            assert _error_code(after) == "auth_token_revoked"
            replay = await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh})
            assert replay.status_code == 401


class TestApiLogout:
    async def test_logout_with_refresh_token_kills_the_access_token(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _create_user(db_session, "authsess_logout")
        access, refresh = await _sign_in(client, user.login)

        resp = await client.post("/api/v1/auth/logout/", json={"refresh_token": refresh})
        assert resp.status_code == 200

        after = await _get_sessions(client, access)
        assert after.status_code == 401
        assert _error_code(after) == "auth_token_revoked"

    async def test_logout_with_bearer_only_ends_that_session(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _create_user(db_session, "authsess_bearer")
        access, refresh = await _sign_in(client, user.login)
        other_access, _other_refresh = await _sign_in(client, user.login)

        resp = await client.post("/api/v1/auth/logout/", headers={"Authorization": f"Bearer {access}"})
        assert resp.status_code == 200

        assert (await _get_sessions(client, access)).status_code == 401
        replay = await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh})
        assert replay.status_code == 401
        assert (await _get_sessions(client, other_access)).status_code == 200


class TestPasswordChange:
    async def test_bearer_password_change_revokes_every_session(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Without a refresh cookie there is no session to keep.

        Every refresh token goes, the caller's included, and every other
        session's access tokens stop working at once. The caller's own access
        token is spared, so a client finishing a forced password change is
        not cut off mid-flow; it can no longer refresh.
        """
        user = await _create_user(db_session, "authsess_pwd")
        my_access, my_refresh = await _sign_in(client, user.login)
        others = [await _sign_in(client, user.login) for _ in range(2)]

        resp = await client.post(
            "/api/v1/auth/change-password/",
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            headers={"Authorization": f"Bearer {my_access}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked_sessions"] == 3

        for access, _refresh in others:
            after = await _get_sessions(client, access)
            assert after.status_code == 401
            assert _error_code(after) == "auth_token_revoked"
        assert (await _get_sessions(client, my_access)).status_code == 200
        replay = await client.post("/api/v1/auth/refresh/", json={"refresh_token": my_refresh})
        assert replay.status_code == 401

    async def test_password_change_from_a_browser_keeps_that_browser(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _create_user(db_session, "authsess_pwd_cookie")
        csrf_token = (await client.get("/api/v1/health/")).cookies["csrf_token"]
        my_access, my_refresh = await _sign_in(client, user.login)
        other_access, _other_refresh = await _sign_in(client, user.login)

        resp = await client.post(
            "/api/v1/auth/change-password/",
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            cookies={"access_token": my_access, "refresh_token": my_refresh, "csrf_token": csrf_token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked_sessions"] == 1

        after = await _get_sessions(client, other_access)
        assert after.status_code == 401
        assert _error_code(after) == "auth_token_revoked"
        assert (await _get_sessions(client, my_access)).status_code == 200
