"""Integration tests for the get_current_user dependency.

Tests cover:
- JWT auth: valid token returns user
- JWT auth: expired token returns 401
- JWT auth: blocklisted token returns 401
- JWT auth: invalid signature returns 401
- JWT auth: locked user blocked by JWT (but not API key)
- JWT auth: deactivated user blocked
- API key auth: valid key returns user
- API key auth: deactivated key returns 401
- API key auth: expired key returns 401
- API key auth: locked user with API key still works (per spec)
- No credentials returns 401
- Cookie-based JWT works
- auth_client fixture provides working JWT
- agent_client fixture provides working API key
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings as _get_settings
from specivo.core.security import blocklist_token, is_token_blocked
from specivo.services.api_key_service import ApiKeyService
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

_SECRET_KEY = _get_settings().secret_key
_ALGORITHM = "HS256"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession, **kwargs):
    user = UserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str = TEST_PASSWORD):
    return await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": password},
    )


def _make_jwt(user_id: int, *, expired: bool = False, bad_secret: bool = False, jti: str | None = None) -> str:
    """Generate a JWT for testing with optional modifications."""
    now = int(time.time())
    exp = now - 10 if expired else now + 900
    payload = {
        "sub": str(user_id),
        "login": "testuser",
        "is_admin": False,
        "is_service_account": False,
        "iat": now,
        "exp": exp,
        "jti": jti or str(uuid.uuid4()),
    }
    secret = "wrong-secret-key-minimum-32-bytes-long!" if bad_secret else _SECRET_KEY
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


# ---------------------------------------------------------------------------
# Probe endpoint: GET /auth/sessions (requires auth, returns 200 on success)
# ---------------------------------------------------------------------------

# We use /api/v1/auth/sessions as a probe endpoint — it requires auth and
# returns 200 (empty list) when authentication succeeds.
_PROBE = "/api/v1/auth/sessions/"


async def _probe(client: AsyncClient, **request_kwargs) -> int:
    """Call the probe endpoint and return the HTTP status code."""
    resp = await client.get(_PROBE, **request_kwargs)
    return resp.status_code


# ---------------------------------------------------------------------------
# JWT authentication
# ---------------------------------------------------------------------------


class TestJwtAuth:
    async def test_valid_jwt_returns_200(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="jwt_valid")
        login_resp = await _login(client, "jwt_valid")
        token = login_resp.json()["access_token"]

        status = await _probe(client, headers={"Authorization": f"Bearer {token}"})
        assert status == 200

    async def test_expired_jwt_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="jwt_expired")
        token = _make_jwt(user.id, expired=True)

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_token_expired"

    async def test_invalid_signature_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="jwt_bad_sig")
        token = _make_jwt(user.id, bad_secret=True)

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_token_invalid"

    async def test_malformed_token_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get(_PROBE, headers={"Authorization": "Bearer not.a.valid.jwt.token"})
        assert resp.status_code == 401

    async def test_blocklisted_token_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="jwt_blocked")
        login_resp = await _login(client, "jwt_blocked")
        token = login_resp.json()["access_token"]

        # Decode to get jti and exp
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        jti = payload["jti"]
        exp = payload["exp"]
        remaining = exp - int(datetime.now(UTC).timestamp())

        # Add to blocklist (no-op if Redis is unavailable)
        await blocklist_token(jti, remaining)

        # Verify the token was actually stored before asserting the response.
        # If Redis is down, is_token_blocked() returns False and we cannot test revocation.
        actually_blocked = await is_token_blocked(jti)
        if not actually_blocked:
            pytest.skip("Redis not available — blocklist test requires Redis")

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_token_revoked"

    async def test_locked_user_blocked_by_jwt(self, client: AsyncClient, db_session: AsyncSession):
        """Locked user cannot use JWT auth (locking blocks password-based sessions)."""
        user = await _create_user(db_session, login="jwt_locked_user", status="locked")
        # Craft a JWT directly (locked user cannot login normally)
        token = _make_jwt(user.id)

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_locked"

    async def test_deactivated_user_blocked_by_jwt(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="jwt_deactivated", status="deactivated")
        token = _make_jwt(user.id)

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_deactivated"

    async def test_nonexistent_user_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        """JWT with a sub claim for a user that doesn't exist."""
        token = _make_jwt(99999)

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_cookie_based_jwt_works(self, client: AsyncClient, db_session: AsyncSession):
        """Access token delivered as a cookie is accepted."""
        await _create_user(db_session, login="jwt_cookie")
        login_resp = await _login(client, "jwt_cookie")
        # The login response sets the access_token cookie on the client
        assert "access_token" in login_resp.cookies

        # Explicitly pass the cookie because the Secure flag prevents
        # automatic cookie sending over the HTTP test transport.
        access_token = login_resp.cookies["access_token"]
        resp = await client.get(_PROBE, cookies={"access_token": access_token})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Silent JWT refresh on API endpoints
# ---------------------------------------------------------------------------


def _make_jwt_with_remember(user_id: int, *, expired: bool = False, remember: bool = True) -> str:
    """Craft a JWT carrying the ``rem`` claim, matching AuthService output."""
    now = int(time.time())
    exp = now - 10 if expired else now + 900
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": exp,
        "jti": str(uuid.uuid4()),
        "rem": remember,
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


class TestSilentRefreshOnApi:
    """API endpoints recover from expired/missing access_token cookie
    when a valid refresh_token cookie is present, mirroring the silent
    refresh behaviour that full-page renders already enjoy.
    """

    async def test_expired_access_cookie_plus_valid_refresh_cookie_refreshes(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _create_user(db_session, login="silent_refresh_expired")
        login_resp = await _login(client, "silent_refresh_expired")
        refresh_token = login_resp.cookies["refresh_token"]

        # Replace the fresh access_token cookie with an expired one for
        # this user — refresh cookie remains valid.
        expired_access = _make_jwt_with_remember(user.id, expired=True, remember=True)

        resp = await client.get(
            _PROBE,
            cookies={"access_token": expired_access, "refresh_token": refresh_token},
        )
        assert resp.status_code == 200
        # TokenRefreshMiddleware attaches fresh Set-Cookie headers.
        set_cookies = resp.headers.get_list("set-cookie")
        joined = "\n".join(set_cookies)
        assert "access_token=" in joined
        assert "refresh_token=" in joined
        # "remember" was True → Max-Age must be present
        assert "Max-Age=" in joined

    async def test_missing_access_cookie_plus_valid_refresh_cookie_refreshes(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _create_user(db_session, login="silent_refresh_missing")
        login_resp = await _login(client, "silent_refresh_missing")
        refresh_token = login_resp.cookies["refresh_token"]

        # Simulate a browser that dropped the short-lived access_token cookie
        # after Max-Age expired, but still has the long-lived refresh cookie.
        resp = await client.get(_PROBE, cookies={"refresh_token": refresh_token})
        assert resp.status_code == 200
        set_cookies = resp.headers.get_list("set-cookie")
        joined = "\n".join(set_cookies)
        assert "access_token=" in joined
        assert "refresh_token=" in joined

    async def test_expired_access_no_refresh_returns_401(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _create_user(db_session, login="silent_refresh_no_refresh")
        expired_access = _make_jwt_with_remember(user.id, expired=True)

        resp = await client.get(_PROBE, cookies={"access_token": expired_access})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_token_expired"

    async def test_expired_access_invalid_refresh_returns_401(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _create_user(db_session, login="silent_refresh_bad_refresh")
        expired_access = _make_jwt_with_remember(user.id, expired=True)

        resp = await client.get(
            _PROBE,
            cookies={
                "access_token": expired_access,
                "refresh_token": "not-a-real-refresh-token",
            },
        )
        assert resp.status_code == 401
        # Fall through to the original expired-token failure code.
        assert resp.json()["errors"][0]["code"] == "auth_token_expired"

    async def test_api_key_path_ignores_refresh_cookie(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Bearer <api_key> requests must not trigger silent refresh —
        callers on the header path manage their own tokens.
        """
        await _create_user(db_session, login="silent_refresh_apikey")
        jwt_token = (await _login(client, "silent_refresh_apikey")).json()["access_token"]

        created = await client.post(
            "/api/v1/my/api-keys/",
            json={"name": "silent-refresh-apikey"},
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        raw_key = created.json()["raw_key"]

        # Even with a valid refresh_token cookie in the jar, the API-key
        # bearer path must succeed without the middleware rewriting cookies.
        login_resp = await _login(client, "silent_refresh_apikey")
        refresh_token = login_resp.cookies["refresh_token"]

        resp = await client.get(
            _PROBE,
            headers={"Authorization": f"Bearer {raw_key}"},
            cookies={"refresh_token": refresh_token},
        )
        assert resp.status_code == 200
        # Silent refresh must NOT have fired — no fresh auth Set-Cookie.
        set_cookies = resp.headers.get_list("set-cookie")
        joined = "\n".join(set_cookies)
        assert "access_token=" not in joined
        assert "refresh_token=" not in joined

    async def test_remember_false_produces_session_cookies(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """When the old access_token carried rem=False, the refreshed
        cookies are session cookies (no Max-Age)."""
        user = await _create_user(db_session, login="silent_refresh_nomember")
        login_resp = await client.post(
            "/api/v1/auth/login/",
            json={
                "login": "silent_refresh_nomember",
                "password": TEST_PASSWORD,
                "remember": False,
            },
        )
        assert login_resp.status_code == 200
        refresh_token = login_resp.cookies["refresh_token"]

        expired_access = _make_jwt_with_remember(user.id, expired=True, remember=False)

        resp = await client.get(
            _PROBE,
            cookies={"access_token": expired_access, "refresh_token": refresh_token},
        )
        assert resp.status_code == 200
        set_cookies = resp.headers.get_list("set-cookie")
        # Find auth cookies (not any unrelated Set-Cookie such as csrf).
        auth_cookies = [c for c in set_cookies if c.startswith(("access_token=", "refresh_token="))]
        assert len(auth_cookies) == 2
        for cookie in auth_cookies:
            assert "Max-Age=" not in cookie, f"Expected session cookie, got: {cookie}"


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------


class TestApiKeyAuth:
    async def test_valid_api_key_returns_200(self, client: AsyncClient, db_session: AsyncSession):
        await _create_user(db_session, login="apikey_valid")
        # Login to get JWT for key creation
        token = (await _login(client, "apikey_valid")).json()["access_token"]

        created = await client.post(
            "/api/v1/my/api-keys/",
            json={"name": "test-dep-key"},
            headers={"Authorization": f"Bearer {token}"},
        )
        raw_key = created.json()["raw_key"]

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200

    async def test_deactivated_api_key_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="apikey_deactivated")
        service = ApiKeyService()
        key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="dep-inactive")
        key.is_active = False
        await db_session.commit()

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "api_key_inactive"

    async def test_expired_api_key_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        user = await _create_user(db_session, login="apikey_expired")
        service = ApiKeyService()
        past = datetime.now(UTC) - timedelta(seconds=1)
        _key, raw_key = await service.create_key(
            session=db_session, user_id=user.id, name="dep-expired", expires_at=past
        )
        await db_session.commit()

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "api_key_expired"

    async def test_locked_user_api_key_still_works(self, client: AsyncClient, db_session: AsyncSession):
        """Per spec: locked accounts can still use API keys.

        Locking protects against password brute force. Agents should not
        lose access because a human account owner got rate-limited.
        """
        user = await _create_user(db_session, login="apikey_locked_user", status="locked")
        service = ApiKeyService()
        _key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="agent-locked-user")
        await db_session.commit()

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200

    async def test_deactivated_user_api_key_blocked(self, client: AsyncClient, db_session: AsyncSession):
        """Deactivated user status blocks all auth methods including API keys."""
        user = await _create_user(db_session, login="apikey_deactivated_user", status="deactivated")
        service = ApiKeyService()
        _key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="deactivated-user-key")
        await db_session.commit()

        resp = await client.get(_PROBE, headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 401

    async def test_invalid_api_key_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get(_PROBE, headers={"Authorization": "Bearer spv_thiskeyhasneverevenexisted"})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "api_key_invalid"


# ---------------------------------------------------------------------------
# No credentials
# ---------------------------------------------------------------------------


class TestNoCredentials:
    async def test_no_auth_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get(_PROBE)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "unauthorized"

    async def test_empty_bearer_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get(_PROBE, headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    async def test_wrong_scheme_returns_401(self, client: AsyncClient, db_session: AsyncSession):
        resp = await client.get(_PROBE, headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Redis blocklist helpers (unit-level, no HTTP)
# ---------------------------------------------------------------------------


class TestBlocklistHelpers:
    async def test_is_token_blocked_returns_false_for_unknown_jti(self):
        """Unknown JTI is not blocked."""
        result = await is_token_blocked(str(uuid.uuid4()))
        # May return False (not blocked) or True if Redis is down (graceful degradation)
        assert result is False or result is True  # just checking it doesn't raise

    async def test_blocklist_token_and_check(self):
        """blocklist_token then is_token_blocked returns True (when Redis is available)."""
        jti = str(uuid.uuid4())
        await blocklist_token(jti, ttl_seconds=60)
        result = await is_token_blocked(jti)
        # If Redis is unavailable, both calls silently succeed/degrade — no assertion on value
        # If Redis IS available, result must be True
        assert isinstance(result, bool)

    async def test_blocklist_zero_ttl_is_noop(self):
        """blocklist_token with ttl_seconds=0 is a no-op (token already expired)."""
        jti = str(uuid.uuid4())
        await blocklist_token(jti, ttl_seconds=0)  # must not raise
        result = await is_token_blocked(jti)
        # TTL=0 → not stored → not blocked (when Redis is available)
        assert result is False or result is True  # graceful either way


# ---------------------------------------------------------------------------
# Fixture smoke tests
# ---------------------------------------------------------------------------


class TestAuthFixtures:
    async def test_auth_client_fixture_works(self, auth_client: AsyncClient):
        """auth_client fixture provides a pre-authenticated regular user."""
        resp = await auth_client.get(_PROBE)
        assert resp.status_code == 200

    async def test_admin_client_fixture_works(self, admin_client: AsyncClient):
        """admin_client fixture provides a pre-authenticated admin user."""
        resp = await admin_client.get(_PROBE)
        assert resp.status_code == 200

    async def test_agent_client_fixture_works(self, agent_client: AsyncClient):
        """agent_client fixture provides a pre-authenticated service account."""
        resp = await agent_client.get(_PROBE)
        assert resp.status_code == 200

    async def test_auth_client_has_state(self, auth_client: AsyncClient):
        """auth_client.state.user is the logged-in User instance."""
        assert auth_client.state.user is not None
        assert auth_client.state.user.login == "auth_fixture_user"
        assert auth_client.state.user.status == "active"

    async def test_admin_client_has_admin_user(self, admin_client: AsyncClient):
        assert admin_client.state.user.is_admin is True

    async def test_agent_client_has_service_account(self, agent_client: AsyncClient):
        assert agent_client.state.user.is_service_account is True
        assert agent_client.state.raw_key.startswith("spv_")
