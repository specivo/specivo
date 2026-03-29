"""Integration tests for API key endpoints.

Tests cover:
- Create key returns raw_key + key_prefix
- Raw key is never returned again in list response
- Authenticate with valid key succeeds
- Authenticate with deactivated key fails
- Authenticate with expired key fails
- List keys shows only the requesting user's keys
- PATCH deactivates and reactivates a key
- DELETE removes the key permanently
- key_prefix matches the first 12 chars of the raw key
- Keys from other users are not accessible
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.services.api_key_service import ApiKeyService
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


async def _login_user(client: AsyncClient, login: str) -> str:
    """Login as *login* and return the JWT access token."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, f"Login failed for {login!r}: {resp.text}"
    return resp.json()["access_token"]


async def _create_key(
    client: AsyncClient,
    token: str,
    name: str = "test-key",
    **body_kwargs,
) -> dict:
    """POST /my/api-keys with JWT auth and return the response JSON."""
    body = {"name": name, **body_kwargs}
    resp = await client.post(
        "/api/v1/my/api-keys",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateApiKey:
    async def test_create_returns_raw_key_and_prefix(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="creator")
        token = await _login_user(client, "creator")
        data = await _create_key(client, token, name="ci-deploy")

        assert "raw_key" in data
        assert "key_prefix" in data
        assert data["name"] == "ci-deploy"
        assert data["id"] > 0

    async def test_raw_key_starts_with_spv_prefix(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="prefix_user")
        token = await _login_user(client, "prefix_user")
        data = await _create_key(client, token, name="build-agent")

        assert data["raw_key"].startswith("spv_")

    async def test_key_prefix_matches_first_12_chars_of_raw_key(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="prefix_check")
        token = await _login_user(client, "prefix_check")
        data = await _create_key(client, token, name="prefix-test")

        raw_key = data["raw_key"]
        assert data["key_prefix"] == raw_key[:12]

    @pytest.mark.pro
    async def test_create_key_with_scopes(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="scoped_user")
        token = await _login_user(client, "scoped_user")
        scopes = {"projects": ["ACME"], "permissions": ["issues:read"]}
        data = await _create_key(client, token, name="scoped-key", scopes=scopes)

        assert data["scopes"] == scopes

    async def test_create_key_with_expiry(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="expiry_user")
        token = await _login_user(client, "expiry_user")
        expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        data = await _create_key(client, token, name="expiring-key", expires_at=expires_at)

        assert data["expires_at"] is not None

    async def test_create_requires_auth(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.post("/api/v1/my/api-keys", json={"name": "no-auth"})
        assert resp.status_code == 401

    async def test_create_rejects_empty_name(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="empty_name_user")
        token = await _login_user(client, "empty_name_user")
        resp = await client.post(
            "/api/v1/my/api-keys",
            json={"name": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListApiKeys:
    async def test_list_returns_user_keys_only(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="list_a")
        await _create_user(db_session, login="list_b")
        token_a = await _login_user(client, "list_a")
        token_b = await _login_user(client, "list_b")

        await _create_key(client, token_a, name="key-a1")
        await _create_key(client, token_a, name="key-a2")
        await _create_key(client, token_b, name="key-b1")

        resp = await client.get(
            "/api/v1/my/api-keys",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 200
        keys = resp.json()
        assert len(keys) == 2
        names = {k["name"] for k in keys}
        assert names == {"key-a1", "key-a2"}

    async def test_list_does_not_include_raw_key(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="list_no_raw")
        token = await _login_user(client, "list_no_raw")
        await _create_key(client, token, name="secret-key")

        resp = await client.get(
            "/api/v1/my/api-keys",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        for key_data in resp.json():
            assert "raw_key" not in key_data
            assert "key_hash" not in key_data

    async def test_list_includes_expected_fields(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="list_fields")
        token = await _login_user(client, "list_fields")
        await _create_key(client, token, name="field-check")

        resp = await client.get(
            "/api/v1/my/api-keys",
            headers={"Authorization": f"Bearer {token}"},
        )
        key_data = resp.json()[0]
        assert "id" in key_data
        assert "name" in key_data
        assert "key_prefix" in key_data
        assert "is_active" in key_data
        assert "created_at" in key_data
        assert "last_used_at" in key_data

    async def test_list_requires_auth(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get("/api/v1/my/api-keys")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Authenticate (service-level — no HTTP endpoint for authenticate)
# ---------------------------------------------------------------------------


class TestApiKeyAuthentication:
    async def test_authenticate_valid_key_returns_user(self, db_session: AsyncSession):
        user = await _create_user(db_session, login="auth_valid")
        service = ApiKeyService()
        key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="valid-key")
        await db_session.commit()

        result_user, result_key = await service.authenticate(session=db_session, raw_key=raw_key)
        assert result_user.id == user.id

    async def test_authenticate_invalid_key_raises_401(self, db_session: AsyncSession):
        service = ApiKeyService()
        with pytest.raises(Exception) as exc_info:
            await service.authenticate(session=db_session, raw_key="spv_notarealkey123")
        assert "401" in str(exc_info.value.status_code) or exc_info.value.status_code == 401

    async def test_authenticate_deactivated_key_raises_401(self, db_session: AsyncSession):
        user = await _create_user(db_session, login="auth_deactivated")
        service = ApiKeyService()
        key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="inactive-key")
        key.is_active = False
        await db_session.commit()

        with pytest.raises(Exception) as exc_info:
            await service.authenticate(session=db_session, raw_key=raw_key)
        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "api_key_inactive"

    async def test_authenticate_expired_key_raises_401(self, db_session: AsyncSession):
        user = await _create_user(db_session, login="auth_expired")
        service = ApiKeyService()
        past = datetime.now(UTC) - timedelta(seconds=1)
        key, raw_key = await service.create_key(
            session=db_session, user_id=user.id, name="expired-key", expires_at=past
        )
        await db_session.commit()

        with pytest.raises(Exception) as exc_info:
            await service.authenticate(session=db_session, raw_key=raw_key)
        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "api_key_expired"

    async def test_authenticate_updates_last_used_at(self, db_session: AsyncSession):
        user = await _create_user(db_session, login="auth_last_used")
        service = ApiKeyService()
        key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="track-key")
        await db_session.commit()

        assert key.last_used_at is None
        await service.authenticate(session=db_session, raw_key=raw_key)
        await db_session.refresh(key)
        assert key.last_used_at is not None

    async def test_authenticate_deactivated_via_patch_raises_401(self, client: AsyncClient, db_session: AsyncSession):
        """End-to-end: create key via API, deactivate via PATCH, auth must fail."""
        await _create_user(db_session, login="e2e_deactivate")
        token = await _login_user(client, "e2e_deactivate")
        created = await _create_key(client, token, name="e2e-key")
        key_id = created["id"]
        raw_key = created["raw_key"]

        # Deactivate via PATCH
        patch_resp = await client.patch(
            f"/api/v1/my/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_active"] is False

        # Auth should now fail
        service = ApiKeyService()
        with pytest.raises(Exception) as exc_info:
            await service.authenticate(session=db_session, raw_key=raw_key)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Patch (deactivate / reactivate)
# ---------------------------------------------------------------------------


class TestPatchApiKey:
    async def test_deactivate_key(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="patch_deactivate")
        token = await _login_user(client, "patch_deactivate")
        created = await _create_key(client, token, name="active-key")
        key_id = created["id"]

        resp = await client.patch(
            f"/api/v1/my/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_reactivate_key(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="patch_reactivate")
        token = await _login_user(client, "patch_reactivate")
        created = await _create_key(client, token, name="will-reactivate")
        key_id = created["id"]

        # First deactivate
        await client.patch(
            f"/api/v1/my/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token}"},
        )

        # Then reactivate
        resp = await client.patch(
            f"/api/v1/my/api-keys/{key_id}",
            json={"is_active": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    async def test_patch_other_users_key_returns_404(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="patch_owner")
        await _create_user(db_session, login="patch_intruder")
        token_a = await _login_user(client, "patch_owner")
        token_b = await _login_user(client, "patch_intruder")
        created = await _create_key(client, token_a, name="owners-key")
        key_id = created["id"]

        resp = await client.patch(
            f"/api/v1/my/api-keys/{key_id}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404

    async def test_patch_requires_auth(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.patch("/api/v1/my/api-keys/1", json={"is_active": False})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteApiKey:
    async def test_delete_removes_key(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="delete_user")
        token = await _login_user(client, "delete_user")
        created = await _create_key(client, token, name="to-delete")
        key_id = created["id"]

        del_resp = await client.delete(
            f"/api/v1/my/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert del_resp.status_code == 204

        # Key should no longer appear in list
        list_resp = await client.get(
            "/api/v1/my/api-keys",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert list_resp.json() == []

    async def test_delete_other_users_key_returns_404(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="del_owner")
        await _create_user(db_session, login="del_intruder")
        token_a = await _login_user(client, "del_owner")
        token_b = await _login_user(client, "del_intruder")
        created = await _create_key(client, token_a, name="owners-key")
        key_id = created["id"]

        resp = await client.delete(
            f"/api/v1/my/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404

    async def test_delete_nonexistent_key_returns_404(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="del_nonexistent")
        token = await _login_user(client, "del_nonexistent")
        resp = await client.delete(
            "/api/v1/my/api-keys/99999",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    async def test_delete_requires_auth(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.delete("/api/v1/my/api-keys/1")
        assert resp.status_code == 401
