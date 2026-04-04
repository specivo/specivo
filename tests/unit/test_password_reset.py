"""Unit tests for password reset service logic.

Covers:
- request_password_reset: token generation, email queuing, no enumeration
- reset_password_with_token: token validation, password update, token invalidation
- Token expiry handling
- Account status checks
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    user_id: int = 1,
    email: str = "user@example.com",
    login: str = "testuser",
    status: str = "active",
    display_name: str = "Test User",
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.email = email
    user.login = login
    user.status = status
    user.display_name = display_name
    user.password_hash = "$2b$12$fakehashfortest"
    user.failed_login_count = 0
    user.locked_until = None
    user.password_changed_at = None
    return user


def _make_reset_token(
    user_id: int = 1,
    *,
    expired: bool = False,
    used: bool = False,
) -> MagicMock:
    from specivo.core.utils import utcnow

    token = MagicMock()
    token.id = 1
    token.user_id = user_id
    token.token_hash = "fakehash"
    if expired:
        token.expires_at = utcnow() - timedelta(hours=1)
    else:
        token.expires_at = utcnow() + timedelta(hours=24)
    token.used_at = utcnow() if used else None
    return token


# ---------------------------------------------------------------------------
# request_password_reset
# ---------------------------------------------------------------------------


class TestRequestPasswordReset:
    async def test_queues_email_for_existing_user(self):
        """request_password_reset queues a reset email for a valid user."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("specivo.services.auth_service.send_notification_email") as mock_email,
            patch("specivo.services.auth_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                password_reset_token_expire_hours=24,
                secret_key="testsecret" * 4,
                app_url="http://localhost:8030",
            )
            mock_email.delay = MagicMock()

            await svc.request_password_reset(session, email="user@example.com")

            mock_email.delay.assert_called_once()
            call_args = mock_email.delay.call_args
            assert call_args.args[0] == "user@example.com"  # to_email
            assert "reset" in call_args.args[1].lower()  # subject contains "reset"

    async def test_no_error_for_nonexistent_email(self):
        """request_password_reset must not reveal whether email exists."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        # User not found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("specivo.services.auth_service.send_notification_email") as mock_email,
            patch("specivo.services.auth_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                password_reset_token_expire_hours=24,
                secret_key="testsecret" * 4,
                app_url="http://localhost:8030",
            )
            mock_email.delay = MagicMock()

            # Must NOT raise
            await svc.request_password_reset(session, email="nonexistent@example.com")

            # No email should be sent
            mock_email.delay.assert_not_called()

    async def test_no_email_for_deactivated_user(self):
        """request_password_reset does not send email for deactivated accounts."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user(status="deactivated")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("specivo.services.auth_service.send_notification_email") as mock_email,
            patch("specivo.services.auth_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                password_reset_token_expire_hours=24,
                secret_key="testsecret" * 4,
                app_url="http://localhost:8030",
            )
            mock_email.delay = MagicMock()

            await svc.request_password_reset(session, email="user@example.com")

            mock_email.delay.assert_not_called()

    async def test_creates_token_record(self):
        """request_password_reset creates a PasswordResetToken in the DB."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("specivo.services.auth_service.send_notification_email") as mock_email,
            patch("specivo.services.auth_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                password_reset_token_expire_hours=24,
                secret_key="testsecret" * 4,
                app_url="http://localhost:8030",
            )
            mock_email.delay = MagicMock()

            await svc.request_password_reset(session, email="user@example.com")

            session.add.assert_called_once()
            added_obj = session.add.call_args.args[0]
            # Should be a PasswordResetToken-like object
            assert hasattr(added_obj, "token_hash")
            assert hasattr(added_obj, "user_id")
            assert added_obj.user_id == user.id

    async def test_invalidates_existing_tokens(self):
        """request_password_reset invalidates any existing unused tokens for the user."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        session.execute = AsyncMock(return_value=mock_result)

        with (
            patch("specivo.services.auth_service.send_notification_email") as mock_email,
            patch("specivo.services.auth_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(
                password_reset_token_expire_hours=24,
                secret_key="testsecret" * 4,
                app_url="http://localhost:8030",
            )
            mock_email.delay = MagicMock()

            await svc.request_password_reset(session, email="user@example.com")

            # Should have executed a DELETE/UPDATE for old tokens + INSERT new + flush
            assert session.execute.call_count >= 2  # lookup + invalidate old tokens


# ---------------------------------------------------------------------------
# reset_password_with_token
# ---------------------------------------------------------------------------


class TestResetPasswordWithToken:
    async def test_resets_password_with_valid_token(self):
        """reset_password_with_token updates the user's password hash."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        token_record = _make_reset_token(user_id=user.id)

        # First execute: find token, second: find user
        mock_token_result = MagicMock()
        mock_token_result.scalar_one_or_none.return_value = token_record
        mock_user_result = MagicMock()
        mock_user_result.scalar_one_or_none.return_value = user

        session.execute = AsyncMock(side_effect=[mock_token_result, mock_user_result])

        with patch("specivo.services.auth_service.hash_password", return_value="$2b$12$newhash"):
            await svc.reset_password_with_token(session, token="raw_token_value", new_password="NewP@ss123")

        assert user.password_hash == "$2b$12$newhash"

    async def test_resets_lockout_counters(self):
        """reset_password_with_token clears failed_login_count and locked_until."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        user.failed_login_count = 10
        user.locked_until = MagicMock()  # some datetime
        token_record = _make_reset_token(user_id=user.id)

        mock_token_result = MagicMock()
        mock_token_result.scalar_one_or_none.return_value = token_record
        mock_user_result = MagicMock()
        mock_user_result.scalar_one_or_none.return_value = user

        session.execute = AsyncMock(side_effect=[mock_token_result, mock_user_result])

        with patch("specivo.services.auth_service.hash_password", return_value="$2b$12$newhash"):
            await svc.reset_password_with_token(session, token="raw_token", new_password="NewP@ss123")

        assert user.failed_login_count == 0
        assert user.locked_until is None

    async def test_marks_token_as_used(self):
        """reset_password_with_token sets used_at on the token record."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        token_record = _make_reset_token(user_id=user.id)

        mock_token_result = MagicMock()
        mock_token_result.scalar_one_or_none.return_value = token_record
        mock_user_result = MagicMock()
        mock_user_result.scalar_one_or_none.return_value = user

        session.execute = AsyncMock(side_effect=[mock_token_result, mock_user_result])

        with patch("specivo.services.auth_service.hash_password", return_value="$2b$12$newhash"):
            await svc.reset_password_with_token(session, token="raw_token", new_password="NewP@ss123")

        assert token_record.used_at is not None

    async def test_rejects_expired_token(self):
        """reset_password_with_token raises AppError for expired tokens."""
        from specivo.core.exceptions import AppError
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        token_record = _make_reset_token(expired=True)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token_record
        session.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(AppError) as exc_info:
            await svc.reset_password_with_token(session, token="raw_token", new_password="NewP@ss123")

        assert exc_info.value.status_code == 400
        assert "expired" in exc_info.value.code or "invalid" in exc_info.value.code

    async def test_rejects_already_used_token(self):
        """reset_password_with_token raises AppError for already-used tokens."""
        from specivo.core.exceptions import AppError
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        token_record = _make_reset_token(used=True)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token_record
        session.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(AppError) as exc_info:
            await svc.reset_password_with_token(session, token="raw_token", new_password="NewP@ss123")

        assert exc_info.value.status_code == 400

    async def test_rejects_invalid_token(self):
        """reset_password_with_token raises AppError when token not found."""
        from specivo.core.exceptions import AppError
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(AppError) as exc_info:
            await svc.reset_password_with_token(session, token="bogus_token", new_password="NewP@ss123")

        assert exc_info.value.status_code == 400

    async def test_updates_password_changed_at(self):
        """reset_password_with_token sets password_changed_at on user."""
        from specivo.services.auth_service import AuthService

        svc = AuthService()
        session = AsyncMock()

        user = _make_user()
        user.password_changed_at = None
        token_record = _make_reset_token(user_id=user.id)

        mock_token_result = MagicMock()
        mock_token_result.scalar_one_or_none.return_value = token_record
        mock_user_result = MagicMock()
        mock_user_result.scalar_one_or_none.return_value = user

        session.execute = AsyncMock(side_effect=[mock_token_result, mock_user_result])

        with patch("specivo.services.auth_service.hash_password", return_value="$2b$12$newhash"):
            await svc.reset_password_with_token(session, token="raw_token", new_password="NewP@ss123")

        assert user.password_changed_at is not None
