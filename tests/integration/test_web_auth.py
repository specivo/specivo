"""Web auth page integration tests.

Verifies login page rendering, logout redirect, and API keys page
access control (auth required) and rendering.

Silent refresh tests verify that get_current_user_optional() in
specivo/web/deps.py transparently rotates an expired access_token using
the refresh_token cookie, avoiding a redirect to /login/.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"


def _make_expired_access_token(user_id: int) -> str:
    """Return a syntactically valid JWT whose exp is 1 minute in the past."""
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "jti": "test-jti-expired-silent-refresh",
        "iat": int((now - timedelta(minutes=16)).timestamp()),
        "exp": int((now - timedelta(minutes=1)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


async def _login_and_get_refresh_token(ac: AsyncClient, login: str, password: str) -> str:
    """Login via the API and extract the raw refresh_token from the response cookies."""
    resp = await ac.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    # httpx stores Set-Cookie values on the response; the cookie value is
    # available from the response cookies mapping keyed by name.
    refresh_token = resp.cookies.get(_REFRESH_COOKIE)
    assert refresh_token, "refresh_token cookie was not set after login"
    return refresh_token


# ---------------------------------------------------------------------------
# Existing tests (unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_login_page_renders(unauth_client: AsyncClient):
    """GET /login returns 200 and contains 'Sign in'."""
    resp = await unauth_client.get("/login/")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


@pytest.mark.integration
async def test_login_page_has_form(unauth_client: AsyncClient):
    """Login page contains an Alpine.js loginForm component."""
    resp = await unauth_client.get("/login/")
    assert resp.status_code == 200
    assert "x-data" in resp.text
    assert "loginForm" in resp.text


@pytest.mark.integration
async def test_logout_redirects(unauth_client: AsyncClient):
    """GET /logout returns 302 redirect to /login."""
    resp = await unauth_client.get("/logout/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login/"


@pytest.mark.integration
async def test_api_keys_requires_auth(unauth_client: AsyncClient):
    """GET /my/api-keys without auth redirects to /login."""
    resp = await unauth_client.get("/my/api-keys/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_api_keys_with_auth(auth_client: AsyncClient):
    """GET /my/api-keys with valid auth cookie returns 200 with API Keys content."""
    # The auth_client has a Bearer token in headers. The web page reads
    # cookies, so we need to login via API and use the cookie that's set.
    # auth_client already logged in via POST /api/v1/auth/login which sets
    # httpOnly cookies on the response. However, httpx AsyncClient with
    # ASGITransport doesn't persist cookies from previous responses by default.
    # We use the token directly via cookie instead.
    token = auth_client.state.token
    resp = await auth_client.get(
        "/my/api-keys/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "API Keys" in resp.text


# ---------------------------------------------------------------------------
# Silent refresh tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_expired_access_token_with_valid_refresh_token_stays_logged_in(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Expired access_token + valid refresh_token => 200, page renders, new cookie issued.

    This test FAILS until get_current_user_optional() implements silent
    token refresh in specivo/web/deps.py.
    """
    # Arrange: create a user and obtain a valid refresh_token via normal login.
    user = UserFactory.build(status="active")
    db_session.add(user)
    await db_session.flush()

    refresh_token = await _login_and_get_refresh_token(client, user.login, TEST_PASSWORD)
    expired_access = _make_expired_access_token(user.id)

    # Act: navigate to a protected web page with an expired access_token but a
    # live refresh_token.  Both cookies have path=/ so the browser sends them
    # on all requests.  The silent refresh in get_current_user_optional()
    # should detect the expired access token and use the refresh token to
    # obtain a new pair.
    resp = await client.get(
        "/",
        cookies={
            _ACCESS_COOKIE: expired_access,
            _REFRESH_COOKIE: refresh_token,
        },
        follow_redirects=False,
    )

    # Assert: the page should render without redirecting to /login/
    assert resp.status_code == 200, (
        f"Expected 200 after silent refresh, got {resp.status_code}. "
        "Implement silent refresh in get_current_user_optional()."
    )
    assert "Dashboard" in resp.text

    # A new access_token must be set in the response (silent refresh happened).
    new_access = resp.cookies.get(_ACCESS_COOKIE)
    assert new_access is not None, "Silent refresh must set a new access_token cookie"
    assert new_access != expired_access, "New access_token must differ from the expired one"


@pytest.mark.integration
async def test_expired_access_token_without_refresh_token_redirects_to_login(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Expired access_token with no refresh_token => redirect to /login/.

    This is the current (pre-feature) fallback behavior and must be preserved
    after the silent-refresh feature is implemented.
    """
    # Arrange: create a user and craft an expired access token.
    user = UserFactory.build(status="active")
    db_session.add(user)
    await db_session.flush()

    expired_access = _make_expired_access_token(user.id)

    # Act: send only the expired access_token — no refresh_token cookie.
    resp = await client.get(
        "/",
        cookies={_ACCESS_COOKIE: expired_access},
        follow_redirects=False,
    )

    # Assert: must redirect to /login/ (no refresh available).
    assert resp.status_code == 302, f"Expected 302 redirect, got {resp.status_code}"
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_expired_access_token_with_invalid_refresh_token_redirects_to_login(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Expired access_token + bogus/expired refresh_token => redirect to /login/.

    The silent refresh attempt should fail gracefully and fall back to the
    redirect rather than raising a 500.
    """
    # Arrange: create a user, craft an expired access token, use a garbage
    # refresh token that has never been stored in the database.
    user = UserFactory.build(status="active")
    db_session.add(user)
    await db_session.flush()

    expired_access = _make_expired_access_token(user.id)
    invalid_refresh = "this-refresh-token-does-not-exist-in-the-database"

    # Act: send both cookies, but the refresh token is invalid.
    resp = await client.get(
        "/",
        cookies={
            _ACCESS_COOKIE: expired_access,
            _REFRESH_COOKIE: invalid_refresh,
        },
        follow_redirects=False,
    )

    # Assert: refresh fails -> redirect to /login/, not a 500.
    assert resp.status_code == 302, f"Expected 302 redirect, got {resp.status_code}"
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_silent_refresh_sets_new_cookies(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """After a successful silent refresh both cookies are updated in the response.

    Verifies that:
    - access_token cookie is present and different from the expired one.
    - refresh_token cookie is present (token rotation — old token is consumed).

    This test FAILS until get_current_user_optional() implements silent
    token refresh in specivo/web/deps.py.
    """
    # Arrange
    user = UserFactory.build(status="active")
    db_session.add(user)
    await db_session.flush()

    refresh_token = await _login_and_get_refresh_token(client, user.login, TEST_PASSWORD)
    expired_access = _make_expired_access_token(user.id)

    # Act
    resp = await client.get(
        "/",
        cookies={
            _ACCESS_COOKIE: expired_access,
            _REFRESH_COOKIE: refresh_token,
        },
        follow_redirects=False,
    )

    # Assert: silent refresh succeeded
    assert resp.status_code == 200, (
        f"Expected 200 after silent refresh, got {resp.status_code}. "
        "Implement silent refresh in get_current_user_optional()."
    )

    # Both cookies must be refreshed.
    new_access = resp.cookies.get(_ACCESS_COOKIE)
    new_refresh = resp.cookies.get(_REFRESH_COOKIE)

    assert new_access is not None, "Response must set a new access_token cookie"
    assert new_access != expired_access, "New access_token must differ from the expired one"

    assert new_refresh is not None, (
        "Response must set a new refresh_token cookie (token rotation). "
        "The old refresh_token is consumed by the refresh operation."
    )
    assert new_refresh != refresh_token, "New refresh_token must differ from the original"
