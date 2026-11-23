"""Unit tests for password reset service logic.

Covers:
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
    # Explicit: an unset MagicMock attribute is truthy, which would make every
    # mocked user look like the anonymous user or another system account.
    user.is_anonymous = False
    user.is_system_principal = False
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
# reset_password_with_token
# ---------------------------------------------------------------------------


class TestResetPasswordWithToken:
    @pytest.fixture(autouse=True)
    def _revoke_sessions(self):
        """Session revocation is covered by an integration test; here it is a stub."""
        with patch(
            "specivo.services.auth_service._sessions.revoke_user_sessions", new=AsyncMock(return_value=0)
        ) as revoke:
            self.revoke = revoke
            yield

    async def test_revokes_every_session_of_the_user(self):
        """A reset ends every session of the account."""
        from specivo.services.auth_service import AuthService

        session = AsyncMock()
        user = _make_user()
        token_record = _make_reset_token(user_id=user.id)
        mock_token_result = MagicMock()
        mock_token_result.scalar_one_or_none.return_value = token_record
        mock_user_result = MagicMock()
        mock_user_result.scalar_one_or_none.return_value = user
        session.execute = AsyncMock(side_effect=[mock_token_result, mock_user_result])

        with patch("specivo.services.auth_service.hash_password", return_value="$2b$12$newhash"):
            await AuthService().reset_password_with_token(session, token="raw", new_password="NewP@ss123")

        self.revoke.assert_awaited_once_with(session, user.id, reason="password_reset", actor_id=user.id)

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
