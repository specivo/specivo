"""Integration tests for POST /api/v1/auth/change-password/.

Covers:
- Happy path — the new password authenticates, the old one no longer does
- Wrong current password is rejected and leaves the stored hash untouched
- New password below the configured minimum is rejected
- New password identical to the current one is rejected
- Unauthenticated callers get 401
- Service accounts (password_hash IS NULL) get a clear 400, not a crash
- Other sessions are revoked while the calling session keeps working
- password_changed_at is stamped
- Audit rows are written for both success and failure
- The preferences page renders a working form instead of the old placeholder

The rate limit on this endpoint is exercised in ``test_rate_limit.py``,
which runs serially because it depends on exact Redis counter state.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

_NEW_PASSWORD = "brand-new-passphrase"
_CHANGE_URL = "/api/v1/auth/change-password/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession, login: str, **kwargs) -> User:
    """Persist a user and commit so the API endpoints can see it."""
    user = UserFactory.build(login=login, status="active", **kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str):
    return await client.post("/api/v1/auth/login/", json={"login": login, "password": password})


async def _login_tokens(client: AsyncClient, login: str, password: str) -> dict:
    resp = await _login(client, login, password)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _change_password(
    client: AsyncClient,
    token: str,
    current_password: str,
    new_password: str,
):
    return await client.post(
        _CHANGE_URL,
        json={"current_password": current_password, "new_password": new_password},
        headers={"Authorization": f"Bearer {token}"},
    )


async def _audit_events(db: AsyncSession, event_type: str) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog)
        .where(SecurityAuditLog.event_type == event_type)
        .order_by(SecurityAuditLog.created_at.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestChangePasswordSuccess:
    async def test_new_password_authenticates_and_old_one_does_not(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """The password really changes: new one logs in, old one is refused."""
        await _create_user(db_session, "chpw_happy")
        tokens = await _login_tokens(client, "chpw_happy", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, _NEW_PASSWORD)
        assert resp.status_code == 200, resp.text

        assert (await _login(client, "chpw_happy", TEST_PASSWORD)).status_code == 401
        assert (await _login(client, "chpw_happy", _NEW_PASSWORD)).status_code == 200

    async def test_password_changed_at_is_stamped(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """A successful change sets password_changed_at and a fresh hash."""
        user = await _create_user(db_session, "chpw_stamp")
        assert user.password_changed_at is None
        old_hash = user.password_hash

        tokens = await _login_tokens(client, "chpw_stamp", TEST_PASSWORD)
        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, _NEW_PASSWORD)
        assert resp.status_code == 200, resp.text

        await db_session.refresh(user)
        assert user.password_changed_at is not None
        assert user.password_hash != old_hash

    async def test_failed_login_counter_is_cleared(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """A successful change clears the brute-force counters, as the reset path does."""
        user = await _create_user(db_session, "chpw_counter", failed_login_count=3)

        tokens = await _login_tokens(client, "chpw_counter", TEST_PASSWORD)
        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, _NEW_PASSWORD)
        assert resp.status_code == 200, resp.text

        await db_session.refresh(user)
        assert user.failed_login_count == 0
        assert user.locked_until is None


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


class TestChangePasswordRejections:
    async def test_wrong_current_password_returns_400_and_leaves_password_intact(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """A wrong current password is a 400 and must not modify the account."""
        user = await _create_user(db_session, "chpw_wrong")
        old_hash = user.password_hash
        tokens = await _login_tokens(client, "chpw_wrong", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], "not-my-password", _NEW_PASSWORD)
        assert resp.status_code == 400
        assert resp.json()["errors"][0]["code"] == "password_current_invalid"

        await db_session.refresh(user)
        assert user.password_hash == old_hash
        assert user.password_changed_at is None
        # The original password still works.
        assert (await _login(client, "chpw_wrong", TEST_PASSWORD)).status_code == 200

    async def test_new_password_below_minimum_returns_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """The shared password policy rejects a too-short new password."""
        user = await _create_user(db_session, "chpw_short")
        old_hash = user.password_hash
        tokens = await _login_tokens(client, "chpw_short", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, "short")
        assert resp.status_code == 422
        error = resp.json()["errors"][0]
        assert error["code"] == "validation_error"
        assert error["field"] == "new_password"

        await db_session.refresh(user)
        assert user.password_hash == old_hash

    async def test_new_password_same_as_current_is_rejected(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """Re-submitting the current password is not a password change."""
        user = await _create_user(db_session, "chpw_same")
        tokens = await _login_tokens(client, "chpw_same", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, TEST_PASSWORD)
        assert resp.status_code == 400
        assert resp.json()["errors"][0]["code"] == "password_unchanged"

        await db_session.refresh(user)
        assert user.password_changed_at is None

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        """No credentials — no password change."""
        resp = await client.post(
            _CHANGE_URL,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
        )
        assert resp.status_code == 401

    async def test_service_account_returns_400(self, agent_client: AsyncClient):
        """A service account has password_hash IS NULL — clear 400, no crash."""
        resp = await agent_client.post(
            _CHANGE_URL,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
        )
        assert resp.status_code == 400
        assert resp.json()["errors"][0]["code"] == "password_change_unavailable"

    async def test_service_account_null_hash_never_matches_empty_input(
        self,
        agent_client: AsyncClient,
    ):
        """An empty current password must not be accepted against a NULL hash."""
        resp = await agent_client.post(
            _CHANGE_URL,
            json={"current_password": "", "new_password": _NEW_PASSWORD},
        )
        assert resp.status_code == 400
        assert resp.json()["errors"][0]["code"] == "password_change_unavailable"


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------


class TestChangePasswordSessions:
    async def test_other_sessions_revoked_current_session_survives(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """Other devices are signed out; the browser doing the change is not.

        The Secure cookie flag stops httpx from replaying cookies over the
        plain-HTTP test transport, so they are passed explicitly.
        """
        user = await _create_user(db_session, "chpw_sessions")

        other = await _login_tokens(client, "chpw_sessions", TEST_PASSWORD)
        caller = await _login_tokens(client, "chpw_sessions", TEST_PASSWORD)

        sessions = await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
        assert len(list(sessions.scalars().all())) == 2

        resp = await client.post(
            _CHANGE_URL,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            cookies={
                "access_token": caller["access_token"],
                "refresh_token": caller["refresh_token"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked_sessions"] == 1

        # The other session's refresh token is gone.
        revoked = await client.post("/api/v1/auth/refresh/", json={"refresh_token": other["refresh_token"]})
        assert revoked.status_code == 401

        # The calling session's refresh token still works.
        alive = await client.post("/api/v1/auth/refresh/", json={"refresh_token": caller["refresh_token"]})
        assert alive.status_code == 200

    async def test_bearer_caller_without_refresh_cookie_revokes_every_session(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """Without a refresh cookie there is no session to keep — revoke all of them."""
        user = await _create_user(db_session, "chpw_bearer")
        tokens = await _login_tokens(client, "chpw_bearer", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, _NEW_PASSWORD)
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked_sessions"] == 1

        remaining = await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
        assert list(remaining.scalars().all()) == []


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestChangePasswordAudit:
    async def test_success_writes_password_changed_event(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """A successful change is recorded, without any password material."""
        user = await _create_user(db_session, "chpw_audit_ok")
        tokens = await _login_tokens(client, "chpw_audit_ok", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], TEST_PASSWORD, _NEW_PASSWORD)
        assert resp.status_code == 200, resp.text

        events = await _audit_events(db_session, AuditEvent.PASSWORD_CHANGED)
        assert len(events) == 1
        event = events[0]
        assert event.user_id == user.id
        assert event.details["revoked_sessions"] == 1
        assert TEST_PASSWORD not in str(event.details)
        assert _NEW_PASSWORD not in str(event.details)

    async def test_failure_writes_password_change_failed_event(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """A rejected change survives the error-path rollback and records the reason."""
        user = await _create_user(db_session, "chpw_audit_fail")
        tokens = await _login_tokens(client, "chpw_audit_fail", TEST_PASSWORD)

        resp = await _change_password(client, tokens["access_token"], "not-my-password", _NEW_PASSWORD)
        assert resp.status_code == 400

        events = await _audit_events(db_session, AuditEvent.PASSWORD_CHANGE_FAILED)
        assert len(events) == 1
        event = events[0]
        assert event.user_id == user.id
        assert event.details["reason"] == "password_current_invalid"
        assert "not-my-password" not in str(event.details)


# ---------------------------------------------------------------------------
# Preferences page form
# ---------------------------------------------------------------------------


class TestPreferencesPasswordForm:
    async def test_preferences_page_renders_the_form(self, auth_client: AsyncClient):
        """The Security section offers a real form, not the old placeholder."""
        resp = await auth_client.get(
            "/my/preferences/",
            cookies={"access_token": auth_client.state.token},
        )
        assert resp.status_code == 200
        assert "Password change will be available in a future release" not in resp.text
        assert "changePasswordForm(" in resp.text
        assert 'id="current-password"' in resp.text
        assert 'id="new-password"' in resp.text
        assert 'id="confirm-password"' in resp.text

    async def test_x_data_json_is_single_quoted_and_intact(self, auth_client: AsyncClient):
        """ADR-0001 §3: a tojson payload inside a double-quoted x-data truncates silently."""
        resp = await auth_client.get(
            "/my/preferences/",
            cookies={"access_token": auth_client.state.token},
        )
        assert resp.status_code == 200
        start = resp.text.index("changePasswordForm(")
        block = resp.text[start : resp.text.index("})'", start)]
        # The minimum length arrives as a bare number and the interpolated
        # policy message survived the `%` / `| tojson` precedence trap.
        assert "minLength: 8" in block
        assert "Password must be at least 8 characters" in block
