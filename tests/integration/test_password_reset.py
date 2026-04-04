"""Integration tests for password reset endpoints.

Covers:
- POST /auth/forgot-password/ — always returns 202
- POST /auth/reset-password/ — resets password with valid token
- Reject expired/invalid/used tokens
- User can login with new password after reset
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import PasswordResetToken
from specivo.models.user import User
from specivo.services.auth_service import _hash_token
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str) -> int:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    return resp.status_code


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def reset_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="resetuser", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Tests: forgot-password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forgot_password_returns_202_for_existing_email(
    client: AsyncClient,
    reset_user: User,
) -> None:
    """POST /auth/forgot-password/ returns 202 for a valid email."""
    with patch("specivo.tasks.notifications.send_notification_email.delay"):
        resp = await client.post(
            "/api/v1/auth/forgot-password/",
            json={"email": reset_user.email},
        )
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_forgot_password_returns_202_for_nonexistent_email(
    client: AsyncClient,
) -> None:
    """POST /auth/forgot-password/ returns 202 even for unknown email (no enumeration)."""
    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        resp = await client.post(
            "/api/v1/auth/forgot-password/",
            json={"email": "nonexistent@example.com"},
        )
    assert resp.status_code == 202
    mock_delay.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_queues_email(
    client: AsyncClient,
    reset_user: User,
) -> None:
    """POST /auth/forgot-password/ queues a password reset email via Celery."""
    with patch("specivo.tasks.notifications.send_notification_email.delay") as mock_delay:
        await client.post(
            "/api/v1/auth/forgot-password/",
            json={"email": reset_user.email},
        )
    assert mock_delay.called
    to_email = mock_delay.call_args.args[0]
    assert to_email == reset_user.email


# ---------------------------------------------------------------------------
# Tests: reset-password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_password_with_valid_token(
    client: AsyncClient,
    db_session: AsyncSession,
    reset_user: User,
) -> None:
    """POST /auth/reset-password/ resets password and user can login with new password."""
    import secrets

    from specivo.core.utils import utcnow

    raw_token = secrets.token_urlsafe(48)
    record = PasswordResetToken(
        user_id=reset_user.id,
        token_hash=_hash_token(raw_token),
        expires_at=utcnow() + timedelta(hours=24),
    )
    db_session.add(record)
    await db_session.commit()

    new_password = "NewSecurePass123!"
    resp = await client.post(
        "/api/v1/auth/reset-password/",
        json={"token": raw_token, "new_password": new_password},
    )
    assert resp.status_code == 200

    # Can login with new password
    login_status = await _login(client, reset_user.login, new_password)
    assert login_status == 200

    # Old password no longer works
    login_status = await _login(client, reset_user.login, TEST_PASSWORD)
    assert login_status == 401


@pytest.mark.asyncio
async def test_reset_password_rejects_invalid_token(
    client: AsyncClient,
) -> None:
    """POST /auth/reset-password/ returns 400 for an invalid token."""
    resp = await client.post(
        "/api/v1/auth/reset-password/",
        json={"token": "completely_bogus_token", "new_password": "NewPass123!"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_rejects_expired_token(
    client: AsyncClient,
    db_session: AsyncSession,
    reset_user: User,
) -> None:
    """POST /auth/reset-password/ returns 400 for an expired token."""
    import secrets

    from specivo.core.utils import utcnow

    raw_token = secrets.token_urlsafe(48)
    record = PasswordResetToken(
        user_id=reset_user.id,
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


@pytest.mark.asyncio
async def test_reset_password_rejects_already_used_token(
    client: AsyncClient,
    db_session: AsyncSession,
    reset_user: User,
) -> None:
    """POST /auth/reset-password/ returns 400 for an already-used token."""
    import secrets

    from specivo.core.utils import utcnow

    raw_token = secrets.token_urlsafe(48)
    record = PasswordResetToken(
        user_id=reset_user.id,
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
