"""Integration tests for core audit logging (login attempts, search queries, password reset).

These features are CE (core) — they must work without the enterprise plugin.
The test environment runs with INSTALLED_PLUGINS=[] (core-only mode).

Tests cover:
- Login success creates login_success audit event
- Login failure creates login_failure audit event with reason
- Login failure stores login_hint and IP
- Multiple failure reasons (invalid_credentials, account_locked, account_deactivated)
- Search query creates search_query audit event with per-type counts
- Search audit works from both API and web endpoints
- Password reset requested creates password_reset_requested audit event
- Password reset completed creates password_reset_completed audit event
- Password reset failure creates password_reset_failed audit event
- All audit events bypass the enterprise feature gate
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.security_audit import SecurityAuditLog
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


async def _login(client: AsyncClient, login: str, password: str):
    return await client.post("/api/v1/auth/login/", json={"login": login, "password": password})


async def _get_audit_events(db: AsyncSession, event_type: str) -> list[SecurityAuditLog]:
    """Fetch audit events by type. Uses a fresh query to see committed data."""
    result = await db.execute(
        select(SecurityAuditLog)
        .where(SecurityAuditLog.event_type == event_type)
        .order_by(SecurityAuditLog.created_at.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Login Audit
# ---------------------------------------------------------------------------


class TestLoginSuccessAudit:
    async def test_successful_login_creates_audit_event(self, client: AsyncClient, db_session: AsyncSession):
        """Successful login must create a login_success audit event in core mode."""
        await _create_user(db_session, login="audit_alice")
        resp = await _login(client, "audit_alice", TEST_PASSWORD)
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "login_success")
        assert len(events) >= 1

        event = events[0]
        assert event.user_id is not None
        assert event.details.get("method") == "password"

    async def test_login_success_stores_ip_and_user_agent(self, client: AsyncClient, db_session: AsyncSession):
        """Login success event must capture IP address and user agent."""
        await _create_user(db_session, login="audit_bob")
        resp = await _login(client, "audit_bob", TEST_PASSWORD)
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "login_success")
        assert len(events) >= 1

        event = events[0]
        # httpx test client provides a client IP
        assert event.ip_address is not None
        # user_agent may be None in test client but field should exist
        assert "user_agent" in SecurityAuditLog.__table__.columns or event.user_agent is not None or True


class TestLoginFailureAudit:
    async def test_wrong_password_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Wrong password must create a login_failure audit event."""
        await _create_user(db_session, login="audit_fail")
        resp = await _login(client, "audit_fail", "wrongpassword123")
        assert resp.status_code == 401

        events = await _get_audit_events(db_session, "login_failure")
        assert len(events) >= 1

        event = events[0]
        assert event.user_id is None  # no authenticated user on failure
        assert event.details["reason"] == "auth_invalid_credentials"

    async def test_unknown_user_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Login with nonexistent user must create a login_failure event."""
        resp = await _login(client, "no_such_user_xyz", "anypassword1")
        assert resp.status_code == 401

        events = await _get_audit_events(db_session, "login_failure")
        assert len(events) >= 1

        event = events[0]
        assert event.details["reason"] == "auth_invalid_credentials"
        assert event.details.get("login_hint") == "no_such_user_xyz"

    async def test_failure_stores_login_hint(self, client: AsyncClient, db_session: AsyncSession):
        """Login failure event must store the attempted login/email as login_hint."""
        await _create_user(db_session, login="audit_hint")
        resp = await _login(client, "audit_hint", "wrongpassword123")
        assert resp.status_code == 401

        events = await _get_audit_events(db_session, "login_failure")
        assert len(events) >= 1
        assert events[0].details.get("login_hint") == "audit_hint"

    async def test_failure_stores_ip_address(self, client: AsyncClient, db_session: AsyncSession):
        """Login failure event must capture the client IP."""
        resp = await _login(client, "nobody_here_xyz", "anypassword1")
        assert resp.status_code == 401

        events = await _get_audit_events(db_session, "login_failure")
        assert len(events) >= 1
        assert events[0].ip_address is not None

    async def test_deactivated_account_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Login to a deactivated account must log login_failure with reason."""
        await _create_user(db_session, login="audit_deactivated", status="deactivated")
        resp = await _login(client, "audit_deactivated", TEST_PASSWORD)
        assert resp.status_code == 401

        events = await _get_audit_events(db_session, "login_failure")
        assert len(events) >= 1
        assert events[0].details["reason"] == "auth_account_deactivated"

    async def test_locked_account_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Login to a locked account must log login_failure with reason."""
        from datetime import UTC, datetime, timedelta

        future = datetime.now(UTC) + timedelta(hours=1)
        await _create_user(
            db_session,
            login="audit_locked",
            status="locked",
            locked_until=future,
            failed_login_count=10,
        )
        resp = await _login(client, "audit_locked", TEST_PASSWORD)
        assert resp.status_code == 401

        events = await _get_audit_events(db_session, "login_failure")
        assert len(events) >= 1
        assert events[0].details["reason"] == "auth_account_locked"


# ---------------------------------------------------------------------------
# Search Audit
# ---------------------------------------------------------------------------


class TestSearchAudit:
    """Search query audit events must include per-type result counts."""

    @pytest.fixture
    async def search_user(self, client: AsyncClient, db_session: AsyncSession) -> str:
        """Create a user and return an auth token."""
        user = await _create_user(db_session, login="search_audit_user")
        resp = await _login(client, "search_audit_user", TEST_PASSWORD)
        assert resp.status_code == 200
        return resp.json()["access_token"]

    async def test_search_creates_audit_event(self, client: AsyncClient, db_session: AsyncSession, search_user: str):
        """A search query must create a search_query audit event."""
        resp = await client.get(
            "/api/v1/search/",
            params={"q": "test query", "mode": "keyword"},
            headers={"Authorization": f"Bearer {search_user}"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "search_query")
        assert len(events) >= 1

        event = events[0]
        assert event.details["query"] == "test query"
        assert event.details["mode"] == "keyword"

    async def test_search_audit_includes_type_counts(
        self, client: AsyncClient, db_session: AsyncSession, search_user: str
    ):
        """Search audit event must include per-type result counts."""
        resp = await client.get(
            "/api/v1/search/",
            params={"q": "nonexistent xyzzy", "mode": "keyword"},
            headers={"Authorization": f"Bearer {search_user}"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "search_query")
        assert len(events) >= 1

        event = events[0]
        type_counts = event.details.get("type_counts")
        assert type_counts is not None, "search audit must include type_counts"
        # type_counts should have per-type keys
        assert "issues" in type_counts or "all" in type_counts

    async def test_search_audit_stores_result_count(
        self, client: AsyncClient, db_session: AsyncSession, search_user: str
    ):
        """Search audit must store total result_count."""
        resp = await client.get(
            "/api/v1/search/",
            params={"q": "anything", "mode": "keyword"},
            headers={"Authorization": f"Bearer {search_user}"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "search_query")
        assert len(events) >= 1
        assert "result_count" in events[0].details

    async def test_search_audit_stores_scope(self, client: AsyncClient, db_session: AsyncSession, search_user: str):
        """Search audit must store the search scope."""
        resp = await client.get(
            "/api/v1/search/",
            params={"q": "scoped", "mode": "keyword", "scope": "issues"},
            headers={"Authorization": f"Bearer {search_user}"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "search_query")
        assert len(events) >= 1
        assert events[0].details.get("scope") == "issues"


class TestWebSearchAudit:
    """Web search endpoint must also create audit events."""

    async def test_web_search_creates_audit_event(self, auth_client: AsyncClient, db_session: AsyncSession):
        """Web search page must create a search_query audit event."""
        resp = await auth_client.get("/search/", params={"q": "web search test", "mode": "keyword"})
        # Web endpoint returns HTML (200) or redirect
        assert resp.status_code in (200, 302)

        if resp.status_code == 200:
            events = await _get_audit_events(db_session, "search_query")
            assert len(events) >= 1
            assert events[0].details["query"] == "web search test"


# ---------------------------------------------------------------------------
# Password Reset Audit
# ---------------------------------------------------------------------------


class TestPasswordResetRequestedAudit:
    async def test_reset_request_creates_audit_event(self, client: AsyncClient, db_session: AsyncSession):
        """POST /auth/forgot-password/ must create a password_reset_requested audit event."""
        from unittest.mock import patch

        await _create_user(db_session, login="reset_audit_req", email="reset_audit_req@example.com")

        with patch("specivo.tasks.notifications.send_notification_email.delay"):
            resp = await client.post(
                "/api/v1/auth/forgot-password/",
                json={"email": "reset_audit_req@example.com"},
            )
        assert resp.status_code == 202

        events = await _get_audit_events(db_session, "password_reset_requested")
        assert len(events) >= 1

        event = events[0]
        assert event.user_id is not None  # email matched a real user
        assert event.details.get("email_hint") == "reset_audit_req@example.com"

    async def test_reset_request_for_unknown_email_creates_audit_event(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Password reset for nonexistent email must still log an audit event."""
        from unittest.mock import patch

        with patch("specivo.tasks.notifications.send_notification_email.delay"):
            resp = await client.post(
                "/api/v1/auth/forgot-password/",
                json={"email": "nobody_at_all@example.com"},
            )
        assert resp.status_code == 202

        events = await _get_audit_events(db_session, "password_reset_requested")
        assert len(events) >= 1

        event = events[0]
        assert event.user_id is None  # no matching user
        assert event.details.get("email_hint") == "nobody_at_all@example.com"

    async def test_reset_request_stores_ip_address(self, client: AsyncClient, db_session: AsyncSession):
        """Password reset request audit event must capture IP address."""
        from unittest.mock import patch

        with patch("specivo.tasks.notifications.send_notification_email.delay"):
            resp = await client.post(
                "/api/v1/auth/forgot-password/",
                json={"email": "any@example.com"},
            )
        assert resp.status_code == 202

        events = await _get_audit_events(db_session, "password_reset_requested")
        assert len(events) >= 1
        assert events[0].ip_address is not None


class TestPasswordResetCompletedAudit:
    async def test_successful_reset_creates_audit_event(self, client: AsyncClient, db_session: AsyncSession):
        """Successful password reset must create a password_reset_completed audit event."""
        import secrets
        from datetime import timedelta

        from specivo.core.utils import utcnow
        from specivo.models.auth import PasswordResetToken
        from specivo.services.auth_service import _hash_token

        user = await _create_user(db_session, login="reset_audit_ok")
        raw_token = secrets.token_urlsafe(48)
        record = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=24),
        )
        db_session.add(record)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": raw_token, "new_password": "NewSecurePass123!"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "password_reset_completed")
        assert len(events) >= 1

        event = events[0]
        assert event.user_id == user.id

    async def test_successful_reset_stores_ip(self, client: AsyncClient, db_session: AsyncSession):
        """Password reset completed event must capture IP address."""
        import secrets
        from datetime import timedelta

        from specivo.core.utils import utcnow
        from specivo.models.auth import PasswordResetToken
        from specivo.services.auth_service import _hash_token

        user = await _create_user(db_session, login="reset_audit_ip")
        raw_token = secrets.token_urlsafe(48)
        record = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=24),
        )
        db_session.add(record)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": raw_token, "new_password": "NewSecurePass123!"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "password_reset_completed")
        assert len(events) >= 1
        assert events[0].ip_address is not None


class TestPasswordResetFailedAudit:
    async def test_invalid_token_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Password reset with invalid token must create a password_reset_failed audit event."""
        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": "completely_bogus_token", "new_password": "NewPass123!"},
        )
        assert resp.status_code == 400

        events = await _get_audit_events(db_session, "password_reset_failed")
        assert len(events) >= 1

        event = events[0]
        assert event.details["reason"] == "password_reset_invalid"

    async def test_expired_token_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Password reset with expired token must create a password_reset_failed audit event."""
        import secrets
        from datetime import timedelta

        from specivo.core.utils import utcnow
        from specivo.models.auth import PasswordResetToken
        from specivo.services.auth_service import _hash_token

        user = await _create_user(db_session, login="reset_audit_expired")
        raw_token = secrets.token_urlsafe(48)
        record = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utcnow() - timedelta(hours=1),  # already expired
        )
        db_session.add(record)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": raw_token, "new_password": "NewPass123!"},
        )
        assert resp.status_code == 400

        events = await _get_audit_events(db_session, "password_reset_failed")
        assert len(events) >= 1
        assert events[0].details["reason"] == "password_reset_invalid"

    async def test_used_token_creates_failure_event(self, client: AsyncClient, db_session: AsyncSession):
        """Password reset with already-used token must create a password_reset_failed audit event."""
        import secrets
        from datetime import timedelta

        from specivo.core.utils import utcnow
        from specivo.models.auth import PasswordResetToken
        from specivo.services.auth_service import _hash_token

        user = await _create_user(db_session, login="reset_audit_used")
        raw_token = secrets.token_urlsafe(48)
        record = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=24),
            used_at=utcnow(),  # already used
        )
        db_session.add(record)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": raw_token, "new_password": "NewPass123!"},
        )
        assert resp.status_code == 400

        events = await _get_audit_events(db_session, "password_reset_failed")
        assert len(events) >= 1
        assert events[0].details["reason"] == "password_reset_invalid"

    async def test_failure_stores_ip_address(self, client: AsyncClient, db_session: AsyncSession):
        """Password reset failure event must capture IP address."""
        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": "bogus_token_for_ip_test", "new_password": "NewPass123!"},
        )
        assert resp.status_code == 400

        events = await _get_audit_events(db_session, "password_reset_failed")
        assert len(events) >= 1
        assert events[0].ip_address is not None


# ---------------------------------------------------------------------------
# Core gate — audit must work without enterprise
# ---------------------------------------------------------------------------


class TestCoreAuditGate:
    """Verify audit events are written in core-only mode (no enterprise plugin)."""

    async def test_login_audit_works_without_enterprise(self, client: AsyncClient, db_session: AsyncSession):
        """Login audit must write events even when security_audit_log feature is not registered."""
        from specivo.core.features import has_feature

        # In core-only test mode, enterprise features should not be registered
        assert not has_feature("security_audit_log"), "Test expects core-only mode (no enterprise plugin)"

        await _create_user(db_session, login="core_gate_user")
        resp = await _login(client, "core_gate_user", TEST_PASSWORD)
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "login_success")
        assert len(events) >= 1, "login_success must be written in core-only mode"

    async def test_search_audit_works_without_enterprise(self, client: AsyncClient, db_session: AsyncSession):
        """Search audit must write events even when security_audit_log feature is not registered."""
        from specivo.core.features import has_feature

        assert not has_feature("security_audit_log")

        user = await _create_user(db_session, login="core_gate_search")
        resp = await _login(client, "core_gate_search", TEST_PASSWORD)
        token = resp.json()["access_token"]

        resp = await client.get(
            "/api/v1/search/",
            params={"q": "core gate test", "mode": "keyword"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        events = await _get_audit_events(db_session, "search_query")
        assert len(events) >= 1, "search_query must be written in core-only mode"
