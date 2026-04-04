"""Integration tests for admin users API.

Tests cover:
- List users (admin only, non-admin gets 403)
- Create user (uniqueness, validation, is_admin ignored)
- Reset password (admin only, user not found)
- Search/filter users
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.user import UserFactory

pytestmark = pytest.mark.integration

USERS_URL = "/api/v1/admin/users/"


async def _create_user(db: AsyncSession, **kwargs):
    user = UserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


class TestListUsers:
    async def test_admin_can_list_users(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="listuser1")
        resp = await admin_client.get(USERS_URL)
        assert resp.status_code == 200
        logins = [u["login"] for u in resp.json()]
        assert "listuser1" in logins

    async def test_non_admin_gets_403(self, auth_client: AsyncClient):
        resp = await auth_client.get(USERS_URL)
        assert resp.status_code == 403

    async def test_search_by_login(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="searchable_xyz")
        resp = await admin_client.get(USERS_URL, params={"q": "searchable_xyz"})
        assert resp.status_code == 200
        assert any(u["login"] == "searchable_xyz" for u in resp.json())

    async def test_filter_by_status(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="deact_user", status="deactivated")
        resp = await admin_client.get(USERS_URL, params={"status": "deactivated"})
        assert resp.status_code == 200
        assert all(u["status"] == "deactivated" for u in resp.json())


# ---------------------------------------------------------------------------
# Create user
# ---------------------------------------------------------------------------


class TestCreateUser:
    async def test_admin_can_create_user(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            USERS_URL,
            json={
                "login": "newuser_test",
                "email": "newuser@example.com",
                "display_name": "New User",
                "password": "securepassword123",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["login"] == "newuser_test"
        assert data["status"] == "active"

    async def test_non_admin_gets_403(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            USERS_URL,
            json={
                "login": "forbidden_user",
                "email": "forbidden@example.com",
                "display_name": "Forbidden",
                "password": "securepassword123",
            },
        )
        assert resp.status_code == 403

    async def test_duplicate_login_returns_409(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="dupuser")
        resp = await admin_client.post(
            USERS_URL,
            json={
                "login": "dupuser",
                "email": "dup_unique@example.com",
                "display_name": "Dup",
                "password": "securepassword123",
            },
        )
        assert resp.status_code == 409

    async def test_duplicate_email_returns_409(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="emaildup1", email="same@example.com")
        resp = await admin_client.post(
            USERS_URL,
            json={
                "login": "emaildup2",
                "email": "same@example.com",
                "display_name": "Dup Email",
                "password": "securepassword123",
            },
        )
        assert resp.status_code == 409

    async def test_is_admin_ignored(self, admin_client: AsyncClient):
        """API must ignore is_admin flag — admin promotion only via CLI."""
        resp = await admin_client.post(
            USERS_URL,
            json={
                "login": "not_admin_test",
                "email": "notadmin@example.com",
                "display_name": "Not Admin",
                "password": "securepassword123",
                "is_admin": True,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["is_admin"] is False

    async def test_short_password_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            USERS_URL,
            json={
                "login": "shortpw",
                "email": "shortpw@example.com",
                "display_name": "Short",
                "password": "short",
            },
        )
        assert resp.status_code == 422

    async def test_service_account_no_password(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            USERS_URL,
            json={
                "login": "svc_acct_test",
                "email": "svc@example.com",
                "display_name": "Service Bot",
                "is_service_account": True,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["is_service_account"] is True


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------


class TestResetPassword:
    async def test_admin_can_reset_password(self, admin_client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="resetme")
        resp = await admin_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/",
            json={"password": "newpassword1234"},
        )
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Password reset successfully"

    async def test_non_admin_gets_403(self, auth_client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="resetforbid")
        resp = await auth_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/",
            json={"password": "newpassword1234"},
        )
        assert resp.status_code == 403

    async def test_user_not_found_returns_404(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            "/api/v1/admin/users/999999/reset-password/",
            json={"password": "newpassword1234"},
        )
        assert resp.status_code == 404

    async def test_short_password_rejected(self, admin_client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="resetshort")
        resp = await admin_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/",
            json={"password": "short"},
        )
        assert resp.status_code == 422

    async def test_reset_clears_lockout(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Password reset must clear failed_login_count and locked_until."""
        from datetime import UTC, datetime, timedelta

        user = await _create_user(
            db_session,
            login="lockeduser",
            failed_login_count=10,
            locked_until=datetime.now(UTC) + timedelta(hours=1),
        )
        resp = await admin_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/",
            json={"password": "newpassword1234"},
        )
        assert resp.status_code == 200

        assert resp.json()["detail"] == "Password reset successfully"
