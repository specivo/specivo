"""RP-initiated logout: signing out of an SSO session also ends it at the IdP.

``POST /logout/`` always revokes the Specivo session first. When the session
came from an enabled provider with ``rp_logout_enabled`` whose discovery
document advertises an ``end_session_endpoint``, the browser is then sent
there with ``id_token_hint``, ``client_id`` and the fixed
``post_logout_redirect_uri``; in every other case (password session, provider
off, IdP unreachable, ...) it lands on ``/login/?logged_out=1``. Signing out
never waits on, or fails because of, the IdP.

``POST /api/v1/auth/logout/`` does the same revocation and returns the
end-session URL in its JSON body instead of redirecting.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.redis import get_redis
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.sso.discovery import discovery_cache_key
from specivo.services.sso.provisioning import subject_hash
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import OMIT, MockIdP
from specivo.testing.sso import SsoLoginDriver, link_identity

pytestmark = pytest.mark.integration

LOGGED_OUT = "/login/?logged_out=1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _post_logout_uri() -> str:
    return get_settings().app_url.rstrip("/") + LOGGED_OUT


def _set_cookie_value(resp: Response, name: str) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


async def _csrf_token(client: AsyncClient) -> str:
    """Load a page so the CSRF middleware issues its cookie; keep it on *client*."""
    resp = await client.get("/login/")
    assert resp.status_code == 200
    token = _set_cookie_value(resp, "csrf_token")
    assert token
    client.cookies.delete("csrf_token")
    client.cookies.set("csrf_token", token)
    return token


async def _member(db: AsyncSession, provider: SsoProvider, idp: MockIdP) -> User:
    user = UserFactory.build()
    db.add(user)
    await db.flush()
    await link_identity(db, user, provider, subject=idp.default_sub)
    return user


async def _sso_sign_in(
    client: AsyncClient,
    db: AsyncSession,
    sso_login: SsoLoginDriver,
    provider: SsoProvider,
    idp: MockIdP,
) -> tuple[User, str, str]:
    """Sign in through the mock IdP; keep the auth cookies on *client*."""
    user = await _member(db, provider, idp)
    response = await sso_login(client)
    assert response.status_code == 302, response.text
    access = response.cookies.get("access_token")
    refresh = response.cookies.get("refresh_token")
    assert access and refresh
    client.cookies.set("access_token", access)
    client.cookies.set("refresh_token", refresh)
    return user, access, refresh


async def _password_sign_in(client: AsyncClient, db: AsyncSession) -> tuple[User, str, str]:
    user = UserFactory.build()
    db.add(user)
    await db.flush()
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    client.cookies.set("access_token", body["access_token"])
    client.cookies.set("refresh_token", body["refresh_token"])
    return user, body["access_token"], body["refresh_token"]


async def _stored_id_token(db: AsyncSession, user_id: int) -> str:
    row = (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))).scalar_one()
    assert row.id_token_encrypted
    return secret_box.decrypt(row.id_token_encrypted, "sso-id-token")


async def _web_logout(client: AsyncClient, *, params: dict[str, str] | None = None) -> Response:
    csrf = await _csrf_token(client)
    return await client.post(
        "/logout/",
        params=params,
        data={"csrf_token": csrf, "post_logout_redirect_uri": "https://evil.example.com/"},
        follow_redirects=False,
    )


async def _assert_revoked(client: AsyncClient, db: AsyncSession, user: User, access: str, refresh: str) -> None:
    client.cookies.clear()
    replay = await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh})
    assert replay.status_code == 401
    after = await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access}"})
    assert after.status_code == 401
    rows = (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalars().all()
    assert rows == []


async def _forget_discovery(db: AsyncSession, provider: SsoProvider) -> None:
    """Drop every cached copy of the discovery document, so the IdP is asked again."""
    await (await get_redis()).delete(discovery_cache_key(provider.id))
    provider.discovery_cache = None
    await db.flush()


# ---------------------------------------------------------------------------
# Web logout: SSO session ends at the IdP
# ---------------------------------------------------------------------------


async def test_sso_logout_redirects_to_the_end_session_endpoint(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    issued = await _stored_id_token(db_session, user.id)
    assert jwt.decode(issued, options={"verify_signature": False})["sub"] == mock_idp.default_sub

    resp = await _web_logout(client)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.split("?", 1)[0] == mock_idp.end_session_endpoint
    assert _params(location) == {
        "id_token_hint": [issued],
        "client_id": [mock_idp.client_id],
        "post_logout_redirect_uri": [_post_logout_uri()],
    }
    cleared = {
        value.split("=", 1)[0]
        for key, value in resp.headers.multi_items()
        if key.lower() == "set-cookie" and "max-age=0" in value.lower()
    }
    assert {"access_token", "refresh_token"} <= cleared
    await _assert_revoked(client, db_session, user, access, refresh)


async def test_the_idp_sends_the_browser_back_to_the_fixed_uri(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    resp = await _web_logout(client)

    async with httpx.AsyncClient(transport=mock_idp.transport) as idp_client:
        back = await idp_client.get(resp.headers["location"], follow_redirects=False)
    assert back.status_code == 302
    assert back.headers["location"] == _post_logout_uri()
    assert len(mock_idp.end_session_requests) == 1


async def test_client_supplied_post_logout_redirect_uri_is_ignored(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)

    resp = await _web_logout(
        client, params={"post_logout_redirect_uri": "https://evil.example.com/", "next": "https://evil.example.com/"}
    )

    location = resp.headers["location"]
    assert "evil.example.com" not in location
    assert _params(location)["post_logout_redirect_uri"] == [_post_logout_uri()]


async def test_sso_logout_writes_a_log_line(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        await _web_logout(client)

    lines = [r.getMessage() for r in caplog.records if r.name == "specivo.sso" and "sso.logout" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert f"provider={sso_provider.slug}" in line
    assert subject_hash(mock_idp.default_sub) in line
    assert mock_idp.default_sub not in line
    assert "idp_redirect=true" in line


# ---------------------------------------------------------------------------
# Web logout: local fallbacks
# ---------------------------------------------------------------------------


async def _assert_local_logout(
    client: AsyncClient, db: AsyncSession, idp: MockIdP, user: User, access: str, refresh: str
) -> None:
    resp = await _web_logout(client)
    assert resp.status_code == 302
    assert resp.headers["location"] == LOGGED_OUT
    await _assert_revoked(client, db, user, access, refresh)


async def test_rp_logout_disabled_signs_out_locally(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    sso_provider.rp_logout_enabled = False
    await db_session.flush()

    await _assert_local_logout(client, db_session, mock_idp, user, access, refresh)


async def test_disabled_provider_signs_out_locally(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    sso_provider.enabled = False
    await db_session.flush()

    await _assert_local_logout(client, db_session, mock_idp, user, access, refresh)


async def test_provider_without_end_session_endpoint_signs_out_locally(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    mock_idp.discovery_overrides["end_session_endpoint"] = OMIT
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)

    await _assert_local_logout(client, db_session, mock_idp, user, access, refresh)


async def test_unreachable_discovery_signs_out_locally(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    await _forget_discovery(db_session, sso_provider)
    mock_idp.set_fault("discovery", timeout=True)

    await _assert_local_logout(client, db_session, mock_idp, user, access, refresh)


async def test_discovery_server_error_signs_out_locally(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    await _forget_discovery(db_session, sso_provider)
    mock_idp.set_fault("discovery", status=503)

    await _assert_local_logout(client, db_session, mock_idp, user, access, refresh)


async def test_unreadable_stored_id_token_sends_client_id_only(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    """A SECRET_KEY rotation leaves the stored token unreadable: no hint, no 500."""
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    row.id_token_encrypted = secret_box.encrypt("eyJold.token.value", "sso-id-token", secret_key="x" * 48)
    await db_session.flush()

    resp = await _web_logout(client)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.split("?", 1)[0] == mock_idp.end_session_endpoint
    assert _params(location) == {
        "client_id": [mock_idp.client_id],
        "post_logout_redirect_uri": [_post_logout_uri()],
    }
    await _assert_revoked(client, db_session, user, access, refresh)


async def test_missing_stored_id_token_sends_client_id_only(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, _access, _refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    row.id_token_encrypted = None
    await db_session.flush()

    resp = await _web_logout(client)

    assert "id_token_hint" not in _params(resp.headers["location"])
    assert _params(resp.headers["location"])["client_id"] == [mock_idp.client_id]


async def test_sso_session_found_from_the_access_cookie_alone(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    client.cookies.delete("refresh_token")

    resp = await _web_logout(client)

    assert resp.headers["location"].split("?", 1)[0] == mock_idp.end_session_endpoint
    await _assert_revoked(client, db_session, user, access, refresh)


async def test_password_session_signs_out_locally(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _password_sign_in(client, db_session)

    await _assert_local_logout(client, db_session, mock_idp, user, access, refresh)
    assert mock_idp.calls["discovery"] == 0


async def test_logout_without_a_session_lands_on_signed_out_login(client: AsyncClient) -> None:
    resp = await client.post("/logout/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == LOGGED_OUT


# ---------------------------------------------------------------------------
# API logout returns the URL
# ---------------------------------------------------------------------------


async def test_api_logout_returns_the_end_session_url(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user, access, refresh = await _sso_sign_in(client, db_session, sso_login, sso_provider, mock_idp)
    issued = await _stored_id_token(db_session, user.id)
    client.cookies.clear()

    resp = await client.post(
        "/api/v1/auth/logout/",
        json={"refresh_token": refresh, "post_logout_redirect_uri": "https://evil.example.com/"},
        headers={"Authorization": f"Bearer {access}"},
    )

    assert resp.status_code == 200, resp.text
    url = resp.json()["end_session_url"]
    assert url.split("?", 1)[0] == mock_idp.end_session_endpoint
    assert _params(url) == {
        "id_token_hint": [issued],
        "client_id": [mock_idp.client_id],
        "post_logout_redirect_uri": [_post_logout_uri()],
    }
    assert "location" not in resp.headers
    await _assert_revoked(client, db_session, user, access, refresh)


async def test_api_logout_of_a_password_session_returns_null(client: AsyncClient, db_session: AsyncSession) -> None:
    user, access, refresh = await _password_sign_in(client, db_session)
    client.cookies.clear()

    resp = await client.post("/api/v1/auth/logout/", json={"refresh_token": refresh})

    assert resp.status_code == 200
    assert resp.json() == {"end_session_url": None}
    await _assert_revoked(client, db_session, user, access, refresh)


async def test_api_logout_without_a_session_returns_null(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/logout/")
    assert resp.status_code == 200
    assert resp.json() == {"end_session_url": None}


# ---------------------------------------------------------------------------
# Login page notice
# ---------------------------------------------------------------------------


async def test_login_page_shows_signed_out_notice(client: AsyncClient) -> None:
    resp = await client.get("/login/?logged_out=1")
    assert resp.status_code == 200
    assert "You have been signed out." in resp.text


async def test_login_page_without_the_flag_has_no_notice(client: AsyncClient) -> None:
    resp = await client.get("/login/")
    assert "You have been signed out." not in resp.text


async def test_signed_out_notice_is_translated(client: AsyncClient) -> None:
    resp = await client.get("/login/?logged_out=1", headers={"Accept-Language": "th"})
    assert resp.status_code == 200
    assert "คุณออกจากระบบแล้ว" in resp.text
    assert "You have been signed out." not in resp.text
