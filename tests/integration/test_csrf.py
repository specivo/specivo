"""Integration tests for CSRF double-submit cookie protection.

Tests cover:
- CSRF cookie is set on GET responses
- Mutating requests without CSRF token are rejected (403)
- Mutating requests with valid CSRF token succeed
- CSRF token in form body (URL-encoded) is accepted
- Bearer auth bypasses CSRF (used by auth_client, admin_client)
- API key auth bypasses CSRF
- Exempt paths (login, MCP) bypass CSRF
- Invalid/mismatched tokens are rejected
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_csrf_cookie(response) -> str:
    """Extract csrf_token value from response Set-Cookie headers."""
    for key, value in response.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


async def _login_with_cookies(client: AsyncClient, db: AsyncSession) -> tuple[str, str]:
    """Create a user, login, and return (csrf_token, access_token_cookie).

    The client is configured to store cookies, so subsequent requests
    will send both the access_token and csrf_token cookies automatically.
    """
    user = UserFactory.build(login="csrf_test_user", status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # GET a page first to obtain CSRF cookie
    get_resp = await client.get("/api/v1/auth/login/", follow_redirects=False)
    csrf_token = _get_csrf_cookie(get_resp)

    # Login (exempt path — no CSRF needed)
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200
    return csrf_token, resp.json()["access_token"]


# ---------------------------------------------------------------------------
# CSRF Cookie Delivery
# ---------------------------------------------------------------------------


async def test_csrf_cookie_set_on_get(unauth_client: AsyncClient):
    """GET requests receive a csrf_token cookie."""
    resp = await unauth_client.get("/api/v1/health/")
    csrf = _get_csrf_cookie(resp)
    assert csrf, "Expected csrf_token cookie on GET response"
    assert "." in csrf, "CSRF token should be signed (nonce.sig format)"


async def test_csrf_cookie_not_regenerated_if_valid(unauth_client: AsyncClient):
    """A valid csrf_token cookie is not overwritten on subsequent GETs."""
    resp1 = await unauth_client.get("/api/v1/health/")
    token1 = _get_csrf_cookie(resp1)
    assert token1

    # Send the cookie back on the next GET
    resp2 = await unauth_client.get(
        "/api/v1/health/",
        cookies={"csrf_token": token1},
    )
    token2 = _get_csrf_cookie(resp2)
    # Should NOT set a new cookie (existing one is valid)
    assert not token2, "Should not regenerate CSRF cookie when existing one is valid"


# ---------------------------------------------------------------------------
# CSRF Rejection
# ---------------------------------------------------------------------------


async def test_mutating_request_without_csrf_rejected(client: AsyncClient, db_session: AsyncSession):
    """POST without CSRF token returns 403."""
    user = UserFactory.build(login="csrf_reject_user", status="active")
    db_session.add(user)
    await db_session.flush()

    # Login first (exempt, so this works)
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    # Now try a mutating request with cookie auth but no CSRF token
    # Use the cookie from login response
    cookies = {}
    for key, val in resp.headers.multi_items():
        if key.lower() == "set-cookie" and "access_token=" in val:
            cookies["access_token"] = val.split("access_token=")[1].split(";")[0]

    resp = await client.patch(
        "/api/v1/users/me/preferences/",
        json={"timezone": "UTC"},
        cookies=cookies,
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


async def test_mutating_request_with_wrong_token_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """POST with mismatched CSRF cookie and header returns 403."""
    user = UserFactory.build(login="csrf_mismatch_user", status="active")
    db_session.add(user)
    await db_session.flush()

    # Get a CSRF cookie
    get_resp = await client.get("/api/v1/health/")
    csrf_token = _get_csrf_cookie(get_resp)
    assert csrf_token

    # Login
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    cookies = {"csrf_token": csrf_token}
    for key, val in resp.headers.multi_items():
        if key.lower() == "set-cookie" and "access_token=" in val:
            cookies["access_token"] = val.split("access_token=")[1].split(";")[0]

    # Send WRONG token in header
    resp = await client.patch(
        "/api/v1/users/me/preferences/",
        json={"timezone": "UTC"},
        cookies=cookies,
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# CSRF Success
# ---------------------------------------------------------------------------


async def test_mutating_request_with_valid_csrf_succeeds(
    client: AsyncClient, db_session: AsyncSession
):
    """PATCH with matching CSRF cookie + header succeeds."""
    user = UserFactory.build(login="csrf_ok_user", status="active")
    db_session.add(user)
    await db_session.flush()

    # Get CSRF cookie
    get_resp = await client.get("/api/v1/health/")
    csrf_token = _get_csrf_cookie(get_resp)
    assert csrf_token

    # Login
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    cookies = {"csrf_token": csrf_token}
    for key, val in resp.headers.multi_items():
        if key.lower() == "set-cookie" and "access_token=" in val:
            cookies["access_token"] = val.split("access_token=")[1].split(";")[0]

    # Send matching CSRF token — should succeed
    resp = await client.patch(
        "/api/v1/users/me/preferences/",
        json={"timezone": "UTC"},
        cookies=cookies,
        headers={"X-CSRF-Token": csrf_token},
    )
    # 200 or 422 (validation) — but NOT 403
    assert resp.status_code != 403, f"CSRF should have passed: {resp.text}"


async def test_csrf_token_in_form_body_accepted(
    client: AsyncClient, db_session: AsyncSession
):
    """CSRF token in URL-encoded form body is accepted."""
    user = UserFactory.build(login="csrf_form_user", status="active")
    db_session.add(user)
    await db_session.flush()

    # Get CSRF cookie
    get_resp = await client.get("/api/v1/health/")
    csrf_token = _get_csrf_cookie(get_resp)

    # Login
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    cookies = {"csrf_token": csrf_token}
    for key, val in resp.headers.multi_items():
        if key.lower() == "set-cookie" and "access_token=" in val:
            cookies["access_token"] = val.split("access_token=")[1].split(";")[0]

    # Send token as form field (simulating HTML form submission)
    resp = await client.post(
        "/my/profile/",
        data={"display_name": "Test User", "csrf_token": csrf_token},
        cookies=cookies,
    )
    # Should not be 403 CSRF rejection
    assert resp.status_code != 403, f"CSRF form token should have been accepted: {resp.text}"


# ---------------------------------------------------------------------------
# CSRF Bypass for Non-Cookie Auth
# ---------------------------------------------------------------------------


async def test_bearer_auth_bypasses_csrf(auth_client: AsyncClient):
    """Requests with Authorization: Bearer header skip CSRF validation."""
    # auth_client has Bearer token set — no CSRF needed
    # Mutating request — should work without CSRF token
    resp = await auth_client.patch(
        "/api/v1/users/me/preferences/",
        json={"timezone": "UTC"},
    )
    # Should NOT be 403 CSRF — Bearer auth bypasses it
    assert resp.status_code != 403


async def test_api_key_header_bypasses_csrf(client: AsyncClient, db_session: AsyncSession):
    """Requests with X-API-Key header skip CSRF validation."""
    resp = await client.patch(
        "/api/v1/users/me/preferences/",
        json={"timezone": "UTC"},
        headers={"X-API-Key": "spv_fake_key_for_csrf_bypass_test"},
    )
    # Should NOT be 403 CSRF — it will fail with 401 (bad key) instead
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Exempt Paths
# ---------------------------------------------------------------------------


async def test_login_exempt_from_csrf(unauth_client: AsyncClient):
    """POST to /api/v1/auth/login/ works without CSRF token."""
    resp = await unauth_client.post(
        "/api/v1/auth/login/",
        json={"login": "nonexistent", "password": "wrong"},
    )
    # Should get 401 (bad credentials), not 403 (CSRF)
    assert resp.status_code != 403


async def test_forgot_password_exempt_from_csrf(unauth_client: AsyncClient):
    """POST to /api/v1/auth/forgot-password/ works without CSRF token."""
    resp = await unauth_client.post(
        "/api/v1/auth/forgot-password/",
        json={"email": "nobody@example.com"},
    )
    assert resp.status_code != 403


async def test_reset_password_exempt_from_csrf(client: AsyncClient):
    """POST to /api/v1/auth/reset-password/ works without CSRF token."""
    resp = await client.post(
        "/api/v1/auth/reset-password/",
        json={"token": "fake", "new_password": "Str0ngP@ss!"},
    )
    # Should get 400/422 (bad token), not 403 (CSRF)
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Secret stability: tokens must survive process restart and multi-worker deploys
# ---------------------------------------------------------------------------


def test_csrf_secret_derived_from_settings_survives_restart(monkeypatch):
    """Tokens minted by one middleware instance validate under another.

    Regression for the bug where CSRFMiddleware.__init__ called
    ``secrets.token_hex(32)`` to seed the HMAC secret, so every container
    restart (or worker fork) invalidated every outstanding csrf_token cookie,
    surfacing as ``403 {"detail": "CSRF validation failed"}`` on the first
    mutating request from any long-open browser tab.
    """
    from specivo.core.config import get_settings
    from specivo.core.middleware import CSRFMiddleware

    # Force a cache miss on get_settings so both instances observe the same
    # (already-loaded) settings regardless of test ordering.
    settings = get_settings()
    assert settings.secret_key, "test env must provide SECRET_KEY"

    async def _noop(scope, receive, send):  # pragma: no cover - never called
        pass

    mw_a = CSRFMiddleware(_noop)
    mw_b = CSRFMiddleware(_noop)

    token = mw_a._generate_token()
    assert mw_a._validate_token(token)
    assert mw_b._validate_token(token), (
        "token minted by one CSRFMiddleware instance must validate under "
        "another instance backed by the same settings.secret_key"
    )


def test_csrf_secret_rotates_when_settings_secret_key_changes(monkeypatch):
    """A token minted under one secret_key must not validate under another.

    Proves the secret is actually key-bound and not a hardcoded constant.
    """
    from specivo.core.config import get_settings
    from specivo.core.middleware import CSRFMiddleware

    async def _noop(scope, receive, send):  # pragma: no cover - never called
        pass

    settings = get_settings()
    original_key = settings.secret_key

    mw_a = CSRFMiddleware(_noop)
    token = mw_a._generate_token()
    assert mw_a._validate_token(token)

    try:
        # Mutate settings in place; CSRFMiddleware reads it during __init__.
        object.__setattr__(settings, "secret_key", "x" * 64)
        mw_b = CSRFMiddleware(_noop)
        assert not mw_b._validate_token(token), (
            "rotating settings.secret_key must invalidate tokens signed "
            "under the previous key"
        )
    finally:
        object.__setattr__(settings, "secret_key", original_key)
