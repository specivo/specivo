"""CSRF exemptions under /api/v1/auth/, checked against the routing table.

The CSRF middleware exempts a short list of exact auth endpoints, never the
``/api/v1/auth/`` prefix. The properties below hold for every route the app
actually serves, not just the ones somebody remembered to test:

- every exempt path is a mutating route the app serves, so an entry cannot
  silently stop matching after a rename;
- every other mutating route under ``/api/v1/auth/`` refuses a
  cookie-authenticated request that carries no CSRF token, so a route added
  there later is protected by default;
- change-password, the cookie-authenticated endpoint that started this,
  refuses a forged request and leaves the password alone.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.middleware import _CSRF_EXEMPT_PATHS, CSRFMiddleware
from specivo.models.user import User
from specivo.services.auth_utils import verify_password
from specivo.testing.conftest_base import _test_app
from specivo.testing.routes import iter_api_routes
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.integration]

_AUTH_PREFIX = "/api/v1/auth/"
_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}
_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _mutating_auth_routes() -> set[tuple[str, str]]:
    """Return ``(method, path)`` for every mutating route under /api/v1/auth/."""
    return {
        (method, route.path)
        for route in iter_api_routes(_test_app)
        if route.path.startswith(_AUTH_PREFIX)
        for method in route.methods & _MUTATING
    }


def _get_csrf_cookie(response) -> str:
    for key, value in response.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


def _access_cookie(response) -> str:
    for key, value in response.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("access_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


# ---------------------------------------------------------------------------
# The exempt list against the routing table
# ---------------------------------------------------------------------------


def test_exempt_list_is_the_set_that_was_reviewed() -> None:
    """Spelled out so widening the exemption shows up as a change to this file too."""
    assert set(_CSRF_EXEMPT_PATHS) == {
        "/api/v1/auth/login/",
        "/api/v1/auth/refresh/",
        "/api/v1/auth/forgot-password/",
        "/api/v1/auth/reset-password/",
    }


def test_every_exempt_path_is_a_mutating_route() -> None:
    served = {path for _method, path in _mutating_auth_routes()}
    missing = set(_CSRF_EXEMPT_PATHS) - served
    assert not missing, f"CSRF exemptions match no mutating route: {sorted(missing)}"


def test_some_mutating_auth_routes_are_not_exempt() -> None:
    """Guards the next test against passing vacuously."""
    protected = {(m, p) for m, p in _mutating_auth_routes() if p not in _CSRF_EXEMPT_PATHS}
    assert ("POST", "/api/v1/auth/change-password/") in protected
    assert ("POST", "/api/v1/auth/logout/") in protected


async def test_every_other_mutating_auth_route_requires_csrf_with_cookie_auth(
    unauth_client: AsyncClient,
) -> None:
    """A cookie-authenticated request without a token is refused before the handler runs."""
    unauth_client.cookies.set("access_token", "cookie-session")
    for method, path in sorted(_mutating_auth_routes()):
        if path in _CSRF_EXEMPT_PATHS:
            continue
        url = _PATH_PARAM.sub("1", path)
        resp = await unauth_client.request(method, url, json={})
        assert resp.status_code == 403, f"{method} {path} accepted a request without a CSRF token"
        assert "CSRF" in resp.json()["detail"], f"{method} {path}: {resp.text}"


async def test_exempt_paths_accept_cookie_requests_without_csrf(client: AsyncClient) -> None:
    """The exempt endpoints still work for a browser carrying a stale session cookie."""
    bodies = {
        "/api/v1/auth/login/": {"login": "nobody", "password": "wrong"},
        "/api/v1/auth/refresh/": {"refresh_token": "not-a-token"},
        "/api/v1/auth/forgot-password/": {"email": "nobody@example.com"},
        "/api/v1/auth/reset-password/": {"token": "fake", "new_password": "Str0ngP@ss!"},
    }
    assert set(bodies) == set(_CSRF_EXEMPT_PATHS)
    client.cookies.set("access_token", "stale-session")
    for path, body in bodies.items():
        resp = await client.post(path, json=body)
        assert resp.status_code != 403, f"{path} was refused: {resp.text}"


# ---------------------------------------------------------------------------
# Matching rules
# ---------------------------------------------------------------------------


def _middleware() -> CSRFMiddleware:
    async def _noop(scope, receive, send):  # pragma: no cover - never called
        pass

    return CSRFMiddleware(_noop)


_COOKIE_HEADERS = {b"cookie": b"access_token=cookie-session"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/some-endpoint-added-later/",
        "/api/v1/auth/login/extra/",
        "/api/v1/auth/",
        "/api/v1/auth/sessions/1/",
    ],
)
def test_unlisted_auth_paths_need_csrf(path: str) -> None:
    assert _middleware()._needs_csrf(path, _COOKIE_HEADERS)


@pytest.mark.parametrize(
    "path",
    ["/api/v1/auth/login/", "/api/v1/auth/login", "/mcp/", "/mcp/messages/"],
)
def test_listed_paths_and_mounts_are_exempt(path: str) -> None:
    assert not _middleware()._needs_csrf(path, _COOKIE_HEADERS)


# ---------------------------------------------------------------------------
# Regression: change-password without a CSRF token
# ---------------------------------------------------------------------------


async def test_change_password_without_csrf_token_is_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    """A forged cookie-authenticated change-password is refused and changes nothing."""
    user = UserFactory.build(login="csrf_chpw_user", status="active")
    db_session.add(user)
    await db_session.commit()

    get_resp = await client.get("/api/v1/health/")
    csrf_token = _get_csrf_cookie(get_resp)
    assert csrf_token

    login = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert login.status_code == 200
    access_token = _access_cookie(login)
    assert access_token
    client.cookies.clear()
    client.cookies.set("access_token", access_token)
    client.cookies.set("csrf_token", csrf_token)

    body = {"current_password": TEST_PASSWORD, "new_password": "An0ther-Str0ng-Pass!"}
    resp = await client.post("/api/v1/auth/change-password/", json=body)
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]

    await db_session.refresh(user)
    stored = await db_session.get(User, user.id)
    assert stored is not None and stored.password_hash is not None
    assert verify_password(TEST_PASSWORD, stored.password_hash)

    # The same request with the token goes through.
    resp = await client.post(
        "/api/v1/auth/change-password/",
        json=body,
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
