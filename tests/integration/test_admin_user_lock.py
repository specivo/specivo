"""Integration tests for admin user lock/unlock endpoints.

Covers:
- Lock user success (sets status=locked, locked_until optional)
- Unlock user success (sets status=active, clears locked_until)
- Cannot lock self (400)
- Non-admin cannot lock (403)
- Lock nonexistent user (404)
- Lock already-locked user (idempotent, still 200)
- Unlock already-active user (idempotent, still 200)
- Lock with optional locked_until datetime
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.user import User
from tests.factories.user import UserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession, **kwargs) -> User:
    """Persist a UserFactory instance and return it."""
    user = UserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _get_user(db: AsyncSession, user_id: int) -> User | None:
    """Re-fetch a user, forcing a DB round-trip to see API-made changes.

    Uses populate_existing=True to overwrite the identity map cache
    with fresh data from the database.
    """
    stmt = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Lock endpoint tests
# ---------------------------------------------------------------------------


class TestLockUser:
    async def test_lock_user_success(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Admin can lock an active user account."""
        target = await _create_user(db_session, login="lockme")
        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/lock/")
        assert resp.status_code == 200

        data = resp.json()
        assert data["status"] == "locked"

        # Verify DB state
        db_user = await _get_user(db_session, target.id)
        assert db_user is not None
        assert db_user.status == "locked"

    async def test_lock_user_with_locked_until(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Admin can lock a user with an optional locked_until datetime."""
        target = await _create_user(db_session, login="lockuntil")
        future = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        resp = await admin_client.post(
            f"/api/v1/admin/users/{target.id}/lock/",
            json={"locked_until": future},
        )
        assert resp.status_code == 200

        db_user = await _get_user(db_session, target.id)
        assert db_user is not None
        assert db_user.status == "locked"
        assert db_user.locked_until is not None

    async def test_lock_already_locked_user_is_idempotent(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Locking an already-locked user returns 200 (idempotent)."""
        target = await _create_user(db_session, login="alreadylocked", status="locked")
        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/lock/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "locked"

    async def test_lock_nonexistent_user_returns_404(self, admin_client: AsyncClient):
        """Locking a nonexistent user returns 404."""
        resp = await admin_client.post("/api/v1/admin/users/999999/lock/")
        assert resp.status_code == 404

    async def test_cannot_lock_self(self, admin_client: AsyncClient):
        """Admin cannot lock their own account (400)."""
        # admin_client.state.user holds the authenticated admin user
        admin_user = admin_client.state.user
        resp = await admin_client.post(f"/api/v1/admin/users/{admin_user.id}/lock/")
        assert resp.status_code == 400

    async def test_non_admin_cannot_lock(self, auth_client: AsyncClient, db_session: AsyncSession):
        """Non-admin user gets 403 when trying to lock."""
        target = await _create_user(db_session, login="victim")
        resp = await auth_client.post(f"/api/v1/admin/users/{target.id}/lock/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Unlock endpoint tests
# ---------------------------------------------------------------------------


class TestUnlockUser:
    async def test_unlock_user_success(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Admin can unlock a locked user account."""
        future = datetime.now(UTC) + timedelta(hours=1)
        target = await _create_user(db_session, login="unlockme", status="locked", locked_until=future)
        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/unlock/")
        assert resp.status_code == 200

        data = resp.json()
        assert data["status"] == "active"

        # Verify DB state
        db_user = await _get_user(db_session, target.id)
        assert db_user is not None
        assert db_user.status == "active"
        assert db_user.locked_until is None

    async def test_unlock_already_active_user_is_idempotent(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Unlocking an already-active user returns 200 (idempotent)."""
        target = await _create_user(db_session, login="alreadyactive")
        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/unlock/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    async def test_unlock_clears_locked_until(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Unlock must clear the locked_until timestamp."""
        future = datetime.now(UTC) + timedelta(hours=24)
        target = await _create_user(db_session, login="clearlock", status="locked", locked_until=future)
        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/unlock/")
        assert resp.status_code == 200

        db_user = await _get_user(db_session, target.id)
        assert db_user is not None
        assert db_user.locked_until is None

    async def test_unlock_nonexistent_user_returns_404(self, admin_client: AsyncClient):
        """Unlocking a nonexistent user returns 404."""
        resp = await admin_client.post("/api/v1/admin/users/999999/unlock/")
        assert resp.status_code == 404

    async def test_cannot_unlock_self(self, admin_client: AsyncClient):
        """Admin cannot unlock their own account (400)."""
        admin_user = admin_client.state.user
        resp = await admin_client.post(f"/api/v1/admin/users/{admin_user.id}/unlock/")
        assert resp.status_code == 400

    async def test_non_admin_cannot_unlock(self, auth_client: AsyncClient, db_session: AsyncSession):
        """Non-admin user gets 403 when trying to unlock."""
        target = await _create_user(db_session, login="victim2", status="locked")
        resp = await auth_client.post(f"/api/v1/admin/users/{target.id}/unlock/")
        assert resp.status_code == 403

    async def test_unlock_resets_failed_login_count(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Unlock must also reset the failed login counter."""
        target = await _create_user(
            db_session,
            login="bruteforced",
            status="locked",
            failed_login_count=10,
            locked_until=datetime.now(UTC) + timedelta(hours=1),
        )
        resp = await admin_client.post(f"/api/v1/admin/users/{target.id}/unlock/")
        assert resp.status_code == 200

        db_user = await _get_user(db_session, target.id)
        assert db_user is not None
        assert db_user.failed_login_count == 0
