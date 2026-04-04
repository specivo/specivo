"""Integration tests for JWT auth endpoints.

Tests cover:
- Login (success, wrong password, unknown user, email login, case-insensitive)
- Account lockout after 5 failures
- Token refresh (success, rotation, replay detection, expired)
- Logout (single session revocation)
- Logout-all (all sessions revoked)
- Session listing and targeted revocation
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession, **kwargs):
    """Persist a UserFactory instance and commit so API endpoints can see it."""
    user = UserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str) -> dict:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    return resp


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    async def test_successful_login_returns_tokens(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="alice")
        resp = await _login(client, "alice", TEST_PASSWORD)

        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900

    async def test_successful_login_sets_httponly_cookies(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="cookie_user")
        resp = await _login(client, "cookie_user", TEST_PASSWORD)

        assert resp.status_code == 200
        cookies = resp.cookies
        assert "access_token" in cookies
        assert "refresh_token" in cookies

    async def test_wrong_password_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="bob")
        resp = await _login(client, "bob", "wrongpassword")

        assert resp.status_code == 401
        errors = resp.json()["errors"]
        assert errors[0]["code"] == "auth_invalid_credentials"

    async def test_nonexistent_user_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await _login(client, "nobody_here", TEST_PASSWORD)

        assert resp.status_code == 401
        errors = resp.json()["errors"]
        assert errors[0]["code"] == "auth_invalid_credentials"
        # Same message as wrong password — no user enumeration
        assert "Invalid login or password" in errors[0]["message"]

    async def test_nonexistent_and_wrong_password_same_error(self, client: AsyncClient, db_session: AsyncSession):
        """Both unknown-user and wrong-password return identical error shape."""
        await _create_user(db_session, login="charlie")
        resp_wrong_pw = await _login(client, "charlie", "wrong")
        resp_unknown = await _login(client, "unknownxyz", "wrong")

        assert resp_wrong_pw.status_code == resp_unknown.status_code == 401
        assert resp_wrong_pw.json()["errors"][0]["code"] == resp_unknown.json()["errors"][0]["code"]
        assert resp_wrong_pw.json()["errors"][0]["message"] == resp_unknown.json()["errors"][0]["message"]

    async def test_login_with_email(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="dave", email="dave@example.com")
        resp = await _login(client, "dave@example.com", TEST_PASSWORD)

        assert resp.status_code == 200

    async def test_login_case_insensitive_username(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="eve")
        resp = await _login(client, "EVE", TEST_PASSWORD)

        assert resp.status_code == 200

    async def test_login_case_insensitive_email(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="frank", email="frank@example.com")
        resp = await _login(client, "FRANK@EXAMPLE.COM", TEST_PASSWORD)

        assert resp.status_code == 200

    async def test_login_updates_last_login_at(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="grace")
        assert user.last_login_at is None

        await _login(client, "grace", TEST_PASSWORD)
        await db_session.refresh(user)

        assert user.last_login_at is not None

    async def test_deactivated_account_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="henry", status="deactivated")
        resp = await _login(client, "henry", TEST_PASSWORD)

        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_deactivated"

    async def test_pending_verification_account_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="iris", status="pending_verification")
        resp = await _login(client, "iris", TEST_PASSWORD)

        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_email_not_verified"


# ---------------------------------------------------------------------------
# Brute-force lockout
# ---------------------------------------------------------------------------


class TestAccountLockout:
    async def test_account_locked_after_5_failures(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="locked_user")

        # 5 wrong-password attempts
        for _ in range(5):
            resp = await _login(client, "locked_user", "wrongpassword")
            assert resp.status_code == 401

        # 6th attempt — account should now be locked
        resp = await _login(client, "locked_user", TEST_PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_locked"

    async def test_locked_account_includes_locked_until_in_details(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="locked_details")

        for _ in range(5):
            await _login(client, "locked_details", "wrongpassword")

        resp = await _login(client, "locked_details", TEST_PASSWORD)
        assert resp.status_code == 401
        error = resp.json()["errors"][0]
        assert error["details"] is not None
        assert "locked_until" in error["details"]

    async def test_correct_password_after_lockout_still_rejected(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="still_locked")

        for _ in range(5):
            await _login(client, "still_locked", "wrong")

        # Even correct password fails while locked
        resp = await _login(client, "still_locked", TEST_PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_locked"

    async def test_pre_locked_account_returns_locked_error(self, client: AsyncClient, db_session: AsyncSession):
        """A user whose locked_until is in the future cannot log in."""
        locked_until = datetime.now(UTC) + timedelta(hours=1)
        await _create_user(
            db_session,
            login="prelocked",
            locked_until=locked_until,
            failed_login_count=5,
        )
        resp = await _login(client, "prelocked", TEST_PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_locked"


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    async def test_refresh_returns_new_tokens(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="refresh_user")
        login_resp = await _login(client, "refresh_user", TEST_PASSWORD)
        old_refresh = login_resp.json()["refresh_token"]
        old_access = login_resp.json()["access_token"]

        resp = await client.post("/api/v1/auth/refresh/", json={"refresh_token": old_refresh})
        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"] != old_access
        assert body["refresh_token"] != old_refresh

    async def test_old_refresh_token_rejected_after_rotation(self, client: AsyncClient, db_session: AsyncSession):
        """Replay detection: using an already-rotated token returns 401."""
        await _create_user(db_session, login="replay_user")
        login_resp = await _login(client, "replay_user", TEST_PASSWORD)
        old_refresh = login_resp.json()["refresh_token"]

        # First rotation — success
        resp1 = await client.post("/api/v1/auth/refresh/", json={"refresh_token": old_refresh})
        assert resp1.status_code == 200

        # Replay the old token — must fail
        resp2 = await client.post("/api/v1/auth/refresh/", json={"refresh_token": old_refresh})
        assert resp2.status_code == 401
        assert resp2.json()["errors"][0]["code"] == "auth_refresh_expired"

    async def test_refresh_with_garbage_token_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.post("/api/v1/auth/refresh/", json={"refresh_token": "not-a-valid-token-at-all"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_refresh_expired"

    async def test_refresh_with_expired_token_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        """Insert an already-expired refresh token directly into the DB."""
        user = await _create_user(db_session, login="expired_refresh")
        raw = "expiredtoken123"
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expired_record = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db_session.add(expired_record)
        await db_session.flush()

        resp = await client.post("/api/v1/auth/refresh/", json={"refresh_token": raw})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_refresh_expired"

    async def test_refresh_via_cookie(self, client: AsyncClient, db_session: AsyncSession):
        """Refresh token can be provided via cookie instead of request body."""
        await _create_user(db_session, login="cookie_refresh")
        login_resp = await _login(client, "cookie_refresh", TEST_PASSWORD)
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie is not None

        # Send refresh request without body — token comes from cookie.
        # Explicitly pass the cookie because the Secure flag prevents
        # automatic cookie sending over the HTTP test transport.
        resp = await client.post(
            "/api/v1/auth/refresh/",
            cookies={"refresh_token": refresh_cookie},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    async def test_logout_revokes_specific_session(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="logout_user")
        login_resp = await _login(client, "logout_user", TEST_PASSWORD)
        refresh_token = login_resp.json()["refresh_token"]

        # Logout
        resp = await client.post("/api/v1/auth/logout/", json={"refresh_token": refresh_token})
        assert resp.status_code == 204

        # Attempt refresh with the revoked token — must fail
        resp2 = await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh_token})
        assert resp2.status_code == 401

    async def test_logout_clears_cookies(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="cookie_logout")
        await _login(client, "cookie_logout", TEST_PASSWORD)

        resp = await client.post("/api/v1/auth/logout/", json={"refresh_token": "any"})
        assert resp.status_code == 204
        # Cookies should be cleared (max-age=0 or deleted)
        # httpx sets the value to empty string for deleted cookies
        assert resp.cookies.get("access_token", "") == "" or "access_token" not in resp.cookies

    async def test_logout_without_token_returns_204(self, client: AsyncClient, db_session: AsyncSession):
        """Logout with no token is a no-op — still returns 204."""
        resp = await client.post("/api/v1/auth/logout/")
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Logout-all (now requires real JWT auth)
# ---------------------------------------------------------------------------


class TestLogoutAll:
    async def test_logout_all_revokes_all_sessions(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="all_sessions")

        # Login twice from different "devices"
        resp1 = await _login(client, "all_sessions", TEST_PASSWORD)
        resp2 = await _login(client, "all_sessions", TEST_PASSWORD)
        rt1 = resp1.json()["refresh_token"]
        rt2 = resp2.json()["refresh_token"]
        access_token = resp1.json()["access_token"]

        # Logout all (using JWT auth)
        resp = await client.post(
            "/api/v1/auth/logout-all/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["revoked_count"] == 2

        # Both tokens now invalid
        r1 = await client.post("/api/v1/auth/refresh/", json={"refresh_token": rt1})
        r2 = await client.post("/api/v1/auth/refresh/", json={"refresh_token": rt2})
        assert r1.status_code == 401
        assert r2.status_code == 401

    async def test_logout_all_without_auth_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.post("/api/v1/auth/logout-all/")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Session listing and targeted revocation (now requires real JWT auth)
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_list_sessions_returns_active_tokens(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="session_lister")
        await _login(client, "session_lister", TEST_PASSWORD)
        resp2 = await _login(client, "session_lister", TEST_PASSWORD)
        access_token = resp2.json()["access_token"]

        resp = await client.get(
            "/api/v1/auth/sessions/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 2
        # Verify schema fields
        for s in sessions:
            assert "id" in s
            assert "created_at" in s
            assert "expires_at" in s

    async def test_list_sessions_excludes_expired(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="session_expired_list")
        # Insert an expired token directly
        token_hash = hashlib.sha256(b"expired").hexdigest()
        expired = RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        db_session.add(expired)
        await db_session.commit()

        # Log in to get a valid JWT
        login_resp = await _login(client, "session_expired_list", TEST_PASSWORD)
        access_token = login_resp.json()["access_token"]

        resp = await client.get(
            "/api/v1/auth/sessions/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200
        # Only the fresh session from login is visible; the pre-inserted expired one is excluded
        assert len(resp.json()) == 1

    async def test_list_sessions_without_auth_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get("/api/v1/auth/sessions/")
        assert resp.status_code == 401

    async def test_delete_session_revokes_specific_token(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="del_session")
        await _login(client, "del_session", TEST_PASSWORD)
        resp2 = await _login(client, "del_session", TEST_PASSWORD)
        access_token = resp2.json()["access_token"]

        # Get session list to find the first session's ID
        sessions_resp = await client.get(
            "/api/v1/auth/sessions/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        sessions = sessions_resp.json()
        assert len(sessions) == 2

        # Delete the first session
        session_id = sessions[0]["id"]
        del_resp = await client.delete(
            f"/api/v1/auth/sessions/{session_id}/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert del_resp.status_code == 204

        # Only one session remains
        list_resp = await client.get(
            "/api/v1/auth/sessions/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert len(list_resp.json()) == 1

    async def test_delete_session_wrong_user_returns_404(self, client: AsyncClient, db_session: AsyncSession):
        """User cannot revoke another user's session."""
        await _create_user(db_session, login="session_owner")
        await _create_user(db_session, login="session_intruder")

        login_a = await _login(client, "session_owner", TEST_PASSWORD)
        login_b = await _login(client, "session_intruder", TEST_PASSWORD)
        token_a = login_a.json()["access_token"]
        token_b = login_b.json()["access_token"]

        sessions_resp = await client.get(
            "/api/v1/auth/sessions/",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        session_id = sessions_resp.json()[0]["id"]

        # user_b tries to delete user_a's session
        resp = await client.delete(
            f"/api/v1/auth/sessions/{session_id}/",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404

    async def test_delete_session_without_auth_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.delete("/api/v1/auth/sessions/1/")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Remember Me — cookie lifetime behaviour
# ---------------------------------------------------------------------------


def _get_cookie_header(resp, name: str) -> str | None:
    """Return the raw Set-Cookie header string for the named cookie, or None."""
    headers = resp.headers.get_list("set-cookie")
    matches = [h for h in headers if h.startswith(f"{name}=")]
    return matches[0] if matches else None


class TestRememberMe:
    async def test_login_without_remember_sets_session_cookies(self, client: AsyncClient, db_session: AsyncSession):
        """remember=false must produce session cookies (no Max-Age attribute)."""
        await _create_user(db_session, login="no_remember")
        resp = await client.post(
            "/api/v1/auth/login/",
            json={"login": "no_remember", "password": TEST_PASSWORD, "remember": False},
        )
        assert resp.status_code == 200

        access_header = _get_cookie_header(resp, "access_token")
        refresh_header = _get_cookie_header(resp, "refresh_token")
        assert access_header is not None, "access_token cookie missing"
        assert refresh_header is not None, "refresh_token cookie missing"
        assert "Max-Age" not in access_header, f"access_token cookie must be a session cookie but got: {access_header}"
        assert "Max-Age" not in refresh_header, (
            f"refresh_token cookie must be a session cookie but got: {refresh_header}"
        )

    async def test_login_with_remember_sets_persistent_cookies(self, client: AsyncClient, db_session: AsyncSession):
        """remember=true must produce persistent cookies with correct Max-Age values."""
        await _create_user(db_session, login="with_remember")
        resp = await client.post(
            "/api/v1/auth/login/",
            json={"login": "with_remember", "password": TEST_PASSWORD, "remember": True},
        )
        assert resp.status_code == 200

        access_header = _get_cookie_header(resp, "access_token")
        refresh_header = _get_cookie_header(resp, "refresh_token")
        assert access_header is not None, "access_token cookie missing"
        assert refresh_header is not None, "refresh_token cookie missing"
        assert "Max-Age=900" in access_header, f"access_token cookie must have Max-Age=900 but got: {access_header}"
        assert "Max-Age=2592000" in refresh_header, (
            f"refresh_token cookie must have Max-Age=2592000 but got: {refresh_header}"
        )

    async def test_login_without_remember_field_defaults_to_session_cookies(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Omitting remember entirely must default to session cookies (same as remember=false)."""
        await _create_user(db_session, login="default_remember")
        resp = await client.post(
            "/api/v1/auth/login/",
            json={"login": "default_remember", "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200

        access_header = _get_cookie_header(resp, "access_token")
        refresh_header = _get_cookie_header(resp, "refresh_token")
        assert access_header is not None, "access_token cookie missing"
        assert refresh_header is not None, "refresh_token cookie missing"
        assert "Max-Age" not in access_header, f"access_token cookie must be a session cookie but got: {access_header}"
        assert "Max-Age" not in refresh_header, (
            f"refresh_token cookie must be a session cookie but got: {refresh_header}"
        )

    async def test_refresh_preserves_remember_preference(self, client: AsyncClient, db_session: AsyncSession):
        """Refresh token rotation must carry forward the original remember preference."""
        await _create_user(db_session, login="remember_refresh")

        # --- remember=true: refresh should also produce persistent cookies ---
        login_resp = await client.post(
            "/api/v1/auth/login/",
            json={"login": "remember_refresh", "password": TEST_PASSWORD, "remember": True},
        )
        assert login_resp.status_code == 200
        refresh_token = login_resp.json()["refresh_token"]

        access_token = login_resp.json()["access_token"]
        refresh_resp = await client.post(
            "/api/v1/auth/refresh/",
            json={"refresh_token": refresh_token},
            cookies={"access_token": access_token},
        )
        assert refresh_resp.status_code == 200

        access_header = _get_cookie_header(refresh_resp, "access_token")
        refresh_header = _get_cookie_header(refresh_resp, "refresh_token")
        assert access_header is not None, "access_token cookie missing after remember=true refresh"
        assert refresh_header is not None, "refresh_token cookie missing after remember=true refresh"
        assert "Max-Age=900" in access_header, f"Refreshed access_token must remain persistent but got: {access_header}"
        assert "Max-Age=2592000" in refresh_header, (
            f"Refreshed refresh_token must remain persistent but got: {refresh_header}"
        )

        # --- remember=false: refresh should also produce session cookies ---
        await _create_user(db_session, login="no_remember_refresh")
        login_resp2 = await client.post(
            "/api/v1/auth/login/",
            json={"login": "no_remember_refresh", "password": TEST_PASSWORD, "remember": False},
        )
        assert login_resp2.status_code == 200
        access_token2 = login_resp2.json()["access_token"]
        refresh_token2 = login_resp2.json()["refresh_token"]

        refresh_resp2 = await client.post(
            "/api/v1/auth/refresh/",
            json={"refresh_token": refresh_token2},
            cookies={"access_token": access_token2},
        )
        assert refresh_resp2.status_code == 200

        access_header2 = _get_cookie_header(refresh_resp2, "access_token")
        refresh_header2 = _get_cookie_header(refresh_resp2, "refresh_token")
        assert access_header2 is not None, "access_token cookie missing after remember=false refresh"
        assert refresh_header2 is not None, "refresh_token cookie missing after remember=false refresh"
        assert "Max-Age" not in access_header2, (
            f"Refreshed access_token must remain a session cookie but got: {access_header2}"
        )
        assert "Max-Age" not in refresh_header2, (
            f"Refreshed refresh_token must remain a session cookie but got: {refresh_header2}"
        )
