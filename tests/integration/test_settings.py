"""Integration tests for admin settings.

(settings) requirements:
- Get settings (admin only)
- Update settings (admin only)
- Non-admin gets 403
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.user import User
from tests.factories.user import AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="settings_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="settings_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_token(regular_user: User, client: AsyncClient) -> str:
    return await _login(client, regular_user.login)


# ---------------------------------------------------------------------------
# Tests: GET /admin/settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_admin(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """Admin can GET /admin/settings and receives a dict."""
    resp = await client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_get_settings_non_admin_forbidden(
    client: AsyncClient,
    db_session: AsyncSession,
    regular_user: User,
    regular_token: str,
) -> None:
    """Non-admin gets 403 when accessing settings."""
    resp = await client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_get_settings_unauthenticated(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Unauthenticated request gets 401."""
    resp = await client.get("/api/v1/admin/settings")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Tests: PATCH /admin/settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_settings_admin(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """Admin can PATCH /admin/settings to upsert settings."""
    resp = await client.patch(
        "/api/v1/admin/settings",
        json={"app.name": "My Tracker", "theme": "dark"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["app.name"] == "My Tracker"
    assert data["theme"] == "dark"


@pytest.mark.asyncio
async def test_update_settings_is_upsert(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """PATCH updates existing keys and adds new keys."""
    # First set
    await client.patch(
        "/api/v1/admin/settings",
        json={"key1": "value1", "key2": "value2"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Update one key and add another
    resp = await client.patch(
        "/api/v1/admin/settings",
        json={"key1": "updated", "key3": "new"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["key1"] == "updated"
    assert data["key2"] == "value2"  # unchanged
    assert data["key3"] == "new"


@pytest.mark.asyncio
async def test_update_settings_clear_value(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """Passing null as a value clears the setting."""
    await client.patch(
        "/api/v1/admin/settings",
        json={"clearable": "initial"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.patch(
        "/api/v1/admin/settings",
        json={"clearable": None},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["clearable"] is None


@pytest.mark.asyncio
async def test_update_settings_non_admin_forbidden(
    client: AsyncClient,
    db_session: AsyncSession,
    regular_user: User,
    regular_token: str,
) -> None:
    """Non-admin gets 403 when trying to update settings."""
    resp = await client.patch(
        "/api/v1/admin/settings",
        json={"key": "value"},
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_get_returns_previously_set_values(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
) -> None:
    """Settings set via PATCH are returned by GET."""
    await client.patch(
        "/api/v1/admin/settings",
        json={"persist_test": "persisted_value"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("persist_test") == "persisted_value"
