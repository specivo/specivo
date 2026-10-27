"""SSO browser login against the mock IdP: start, callback, session, refusals.

Every refusal ends on ``/login/?sso_error=<code>`` and leaves no session row
behind; the IdP's own error text is never shown. See the "Flows -> Login",
"Security checklist" and "Error UX" sections of the SSO design.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.constants import JWT_ALGORITHM
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.sso.flow import TX_TTL_SECONDS, redirect_uri_for, tx_key
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import DEFAULT_CLIENT_SECRET, ID_TOKEN_VARIANTS, MockIdP, s256
from specivo.testing.sso import TX_COOKIE, SsoLoginDriver, build_sso_provider, link_identity

pytestmark = pytest.mark.integration

LOGIN_PATH = "/auth/oidc/test-idp/login/"
CALLBACK_PATH = "/auth/oidc/test-idp/callback/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def _sso_error(response) -> str:
    """The ``sso_error`` code of a refusal redirect (asserts it is one)."""
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    split = urlsplit(location)
    assert split.path == "/login/", location
    return _params(location)["sso_error"]


def _set_cookies(response) -> dict[str, str]:
    """``Set-Cookie`` headers of a response, keyed by cookie name."""
    result: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0]
        result[name] = header
    return result


def _attrs(set_cookie: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for part in set_cookie.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        attrs[key.lower()] = value
    return attrs


async def _member(db: AsyncSession, provider: SsoProvider, idp: MockIdP, **user_fields: Any) -> User:
    user = UserFactory.build(**user_fields)
    db.add(user)
    await db.flush()
    await link_identity(db, user, provider, subject=idp.default_sub)
    return user


async def _session_rows(db: AsyncSession, user_id: int | None = None) -> list[RefreshToken]:
    stmt = select(RefreshToken)
    if user_id is not None:
        stmt = stmt.where(RefreshToken.user_id == user_id)
    return list((await db.execute(stmt)).scalars().all())


async def _sso_rows(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.auth_method == "sso")))


async def _redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


async def test_start_redirects_to_the_idp_with_pkce_state_and_nonce(
    client: AsyncClient, sso_provider: SsoProvider, sso_login: SsoLoginDriver, mock_idp: MockIdP
) -> None:
    started = await sso_login.start(client, next_path="/projects/ACME/")

    assert started.authorize_url.startswith(mock_idp.authorization_endpoint + "?")
    params = _params(started.authorize_url)
    assert params["response_type"] == "code"
    assert params["client_id"] == mock_idp.client_id
    assert params["redirect_uri"] == redirect_uri_for("test-idp")
    assert params["redirect_uri"] == get_settings().app_url.rstrip("/") + CALLBACK_PATH
    assert "openid" in params["scope"].split()
    assert params["code_challenge_method"] == "S256"
    assert params["state"] and params["nonce"]

    r = await _redis()
    try:
        key = tx_key(params["state"])
        record = json.loads(await r.get(key))
        ttl = await r.ttl(key)
    finally:
        await r.aclose()
    assert s256(record["code_verifier"]) == params["code_challenge"]
    assert record["nonce"] == params["nonce"]
    assert record["next"] == "/projects/ACME/"
    assert TX_TTL_SECONDS - 5 <= ttl <= TX_TTL_SECONDS

    cookie = _set_cookies(started.response)[TX_COOKIE]
    attrs = _attrs(cookie)
    assert "httponly" in attrs
    assert "secure" in attrs
    assert attrs["samesite"].lower() == "lax"
    assert attrs["path"] == "/auth/oidc/"
    assert attrs["max-age"] == str(TX_TTL_SECONDS)
    assert started.tx_cookie
    assert record["binding_hash"] == hashlib.sha256(started.tx_cookie.encode()).hexdigest()


async def test_start_drops_an_offsite_next(client: AsyncClient, sso_login: SsoLoginDriver) -> None:
    started = await sso_login.start(client, next_path="https://evil.example.com/")
    r = await _redis()
    try:
        record = json.loads(await r.get(tx_key(_params(started.authorize_url)["state"])))
    finally:
        await r.aclose()
    assert record["next"] == ""


@pytest.mark.parametrize("path", [LOGIN_PATH, CALLBACK_PATH])
async def test_unknown_provider_is_404(client: AsyncClient, path: str) -> None:
    response = await client.get(path.replace("test-idp", "no-such-idp"))
    assert response.status_code == 404


@pytest.mark.parametrize("path", [LOGIN_PATH, CALLBACK_PATH])
async def test_disabled_provider_is_404(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, path: str
) -> None:
    sso_provider.enabled = False
    await db_session.flush()
    response = await client.get(path, params={"state": "s", "code": "c"})
    assert response.status_code == 404


async def test_start_reports_an_unreachable_idp(
    client: AsyncClient, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    mock_idp.set_fault("discovery", status=503)
    response = await client.get(LOGIN_PATH)
    assert _sso_error(response) == "sso_idp_unreachable"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_login_signs_in_an_existing_identity(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    user = await _member(db_session, sso_provider, mock_idp)
    before = datetime.now(UTC)

    response = await sso_login(client, next_path="/projects/ACME/")

    assert response.status_code == 302
    assert response.headers["location"] == "/projects/ACME/"

    cookies = _set_cookies(response)
    assert "access_token" in cookies and "refresh_token" in cookies
    for name in ("access_token", "refresh_token"):
        attrs = _attrs(cookies[name])
        assert "httponly" in attrs and "secure" in attrs
        assert attrs["samesite"].lower() == "lax"
    # The transaction cookie is cleared.
    tx_attrs = _attrs(cookies[TX_COOKIE])
    assert tx_attrs["path"] == "/auth/oidc/"
    assert tx_attrs.get("max-age") == "0" or "expires" in tx_attrs

    rows = await _session_rows(db_session, user.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.auth_method == "sso"
    identity = (await db_session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalar_one()
    assert row.user_identity_id == identity.id
    assert row.idp_sid == mock_idp.default_sid
    assert row.absolute_expires_at is not None
    expected_cap = before + timedelta(hours=24)
    assert abs((row.absolute_expires_at - expected_cap).total_seconds()) < 60
    assert row.expires_at <= row.absolute_expires_at

    # The ID token is stored encrypted, for id_token_hint at logout.
    assert row.id_token_encrypted and not row.id_token_encrypted.startswith("eyJ")
    id_token = secret_box.decrypt(row.id_token_encrypted, "sso-id-token")
    assert jwt.decode(id_token, options={"verify_signature": False})["sub"] == mock_idp.default_sub

    # The access token names the session.
    access = response.cookies.get("access_token")
    payload = jwt.decode(access, get_settings().secret_key, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == str(user.id)
    assert payload["sid"] == str(row.session_id)
    assert payload["rem"] is True
    assert hashlib.sha256(response.cookies.get("refresh_token").encode()).hexdigest() == row.token_hash

    await db_session.refresh(user)
    await db_session.refresh(identity)
    assert user.last_login_at is not None and user.last_login_at >= before - timedelta(seconds=5)
    assert identity.last_login_at is not None and identity.last_login_at >= before - timedelta(seconds=5)
    assert identity.email_at_last_login == "member@example.com"


async def test_access_token_from_sso_authenticates(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    response = await sso_login(client)
    access = response.cookies.get("access_token")
    sessions = await client.get("/api/v1/auth/sessions/", cookies={"access_token": access})
    assert sessions.status_code == 200, sessions.text
    [session] = sessions.json()
    assert session["auth_method"] == "sso"
    assert session["provider_name"] == "Test IdP"


async def test_login_without_next_lands_on_the_dashboard(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    response = await sso_login(client)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "hostile", ["https://evil.example.com/", "//evil.example.com/", "/\\evil.example.com", "javascript:alert(1)"]
)
async def test_open_redirect_in_next_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    hostile: str,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    response = await sso_login(client, next_path=hostile)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


async def test_session_cap_follows_the_provider(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    sso_provider.session_max_age_hours = 2
    await db_session.flush()
    user = await _member(db_session, sso_provider, mock_idp)
    before = datetime.now(UTC)

    response = await sso_login(client)

    row = (await _session_rows(db_session, user.id))[0]
    assert row.absolute_expires_at is not None
    assert abs((row.absolute_expires_at - (before + timedelta(hours=2))).total_seconds()) < 60
    assert row.expires_at <= row.absolute_expires_at
    # The refresh cookie does not outlive the session cap.
    refresh_attrs = _attrs(_set_cookies(response)["refresh_token"])
    assert int(refresh_attrs["max-age"]) <= 2 * 3600


async def test_callback_issues_new_tokens_over_an_existing_session(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    """Session fixation: a planted session cookie is replaced, never adopted.

    The GET callback carries session cookies and no CSRF header, and the CSRF
    middleware lets it through.
    """
    other = UserFactory.build()
    db_session.add(other)
    await db_session.flush()
    login = await client.post("/api/v1/auth/login/", json={"login": other.login, "password": TEST_PASSWORD})
    planted_access = login.cookies.get("access_token")
    planted_refresh = login.cookies.get("refresh_token")

    user = await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client, next_path="/wiki/")
    callback_url = mock_idp.authorize(started.authorize_url)
    response = await sso_login.callback(
        client,
        callback_url,
        tx_cookie=started.tx_cookie,
        extra_cookies={"access_token": planted_access, "refresh_token": planted_refresh},
    )

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/wiki/"
    new_access = response.cookies.get("access_token")
    new_refresh = response.cookies.get("refresh_token")
    assert new_access and new_access != planted_access
    assert new_refresh and new_refresh != planted_refresh
    payload = jwt.decode(new_access, get_settings().secret_key, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == str(user.id)


# ---------------------------------------------------------------------------
# State, binding and mix-up
# ---------------------------------------------------------------------------


async def test_state_cannot_be_replayed(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client)
    callback_url = mock_idp.authorize(started.authorize_url)

    first = await sso_login.callback(client, callback_url, tx_cookie=started.tx_cookie)
    assert first.status_code == 302 and urlsplit(first.headers["location"]).path != "/login/"

    second = await sso_login.callback(client, callback_url, tx_cookie=started.tx_cookie)
    assert _sso_error(second) == "sso_state_invalid"
    assert await _sso_rows(db_session) == 1


@pytest.mark.parametrize("cookie", [None, "a-cookie-from-another-browser"])
async def test_state_from_another_browser_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    cookie: str | None,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client)
    callback_url = mock_idp.authorize(started.authorize_url)

    response = await sso_login.callback(client, callback_url, tx_cookie=cookie)
    assert _sso_error(response) == "sso_state_invalid"
    assert await _sso_rows(db_session) == 0
    # No token request was made for a transaction that failed the binding.
    assert mock_idp.calls["token"] == 0


async def test_expired_transaction_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client)
    callback_url = mock_idp.authorize(started.authorize_url)

    # What the TTL does after ten minutes.
    r = await _redis()
    try:
        await r.delete(tx_key(_params(started.authorize_url)["state"]))
    finally:
        await r.aclose()

    response = await sso_login.callback(client, callback_url, tx_cookie=started.tx_cookie)
    assert _sso_error(response) == "sso_state_invalid"
    assert await _sso_rows(db_session) == 0


async def test_callback_without_state_is_refused(client: AsyncClient, sso_provider: SsoProvider) -> None:
    response = await client.get(CALLBACK_PATH, params={"code": "abc"})
    assert _sso_error(response) == "sso_state_invalid"


async def test_callback_on_another_provider_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    """Mix-up: a transaction started at provider A cannot complete at provider B."""
    other = build_sso_provider(mock_idp, slug="other-idp", display_name="Other IdP")
    db_session.add(other)
    await db_session.flush()
    await _member(db_session, sso_provider, mock_idp)

    started = await sso_login.start(client)
    callback_url = mock_idp.authorize(started.authorize_url)
    split = urlsplit(callback_url)
    other_callback = f"/auth/oidc/other-idp/callback/?{split.query}"

    response = await sso_login.callback(client, other_callback, tx_cookie=started.tx_cookie)
    assert _sso_error(response) == "sso_state_invalid"
    assert mock_idp.calls["token"] == 0
    assert await _sso_rows(db_session) == 0


@pytest.mark.parametrize("iss", ["https://attacker.example.net/realms/test", None])
async def test_iss_parameter_must_match_the_issuer(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    iss: str | None,
) -> None:
    """RFC 9207: the IdP advertises the iss parameter, so it must be present and exact."""
    await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client)
    params = _params(mock_idp.authorize(started.authorize_url))
    if iss is None:
        params.pop("iss")
    else:
        params["iss"] = iss

    response = await sso_login.callback(client, f"{CALLBACK_PATH}?{urlencode(params)}", tx_cookie=started.tx_cookie)
    assert _sso_error(response) == "sso_token_invalid"
    assert mock_idp.calls["token"] == 0
    assert await _sso_rows(db_session) == 0


async def test_iss_parameter_is_optional_when_not_advertised(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    from specivo.testing.oidc_mock import OMIT

    mock_idp.discovery_overrides["authorization_response_iss_parameter_supported"] = OMIT
    await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client)
    params = _params(mock_idp.authorize(started.authorize_url))
    params.pop("iss")

    response = await sso_login.callback(client, f"{CALLBACK_PATH}?{urlencode(params)}", tx_cookie=started.tx_cookie)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


# ---------------------------------------------------------------------------
# IdP and token failures
# ---------------------------------------------------------------------------


async def test_idp_error_is_reported_without_its_description(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    started = await sso_login.start(client)
    params = _params(mock_idp.authorize(started.authorize_url, error="access_denied"))
    params["error_description"] = "<script>alert('idp')</script> call +1-555-0100"

    response = await sso_login.callback(client, f"{CALLBACK_PATH}?{urlencode(params)}", tx_cookie=started.tx_cookie)
    assert _sso_error(response) == "sso_idp_error"
    assert "script" not in response.headers["location"]
    assert "555" not in response.text

    page = await client.get(response.headers["location"])
    assert page.status_code == 200
    assert "The identity provider refused the sign-in." in page.text
    assert "alert(" not in page.text
    assert "555-0100" not in page.text
    assert await _sso_rows(db_session) == 0


async def test_token_endpoint_failure_is_idp_unreachable(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    mock_idp.set_fault("token", status=500)
    response = await sso_login(client)
    assert _sso_error(response) == "sso_idp_unreachable"
    assert await _sso_rows(db_session) == 0


async def test_refused_code_is_idp_error(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    """The IdP answers invalid_grant (here: a code it never issued)."""
    await _member(db_session, sso_provider, mock_idp)
    started = await sso_login.start(client)
    params = _params(mock_idp.authorize(started.authorize_url))
    params["code"] = "a-code-the-idp-never-issued"
    response = await sso_login.callback(client, f"{CALLBACK_PATH}?{urlencode(params)}", tx_cookie=started.tx_cookie)
    assert _sso_error(response) == "sso_idp_error"
    assert await _sso_rows(db_session) == 0


_BROKEN_ID_TOKENS = [name for name in ID_TOKEN_VARIANTS if name != "groups_overage"]


@pytest.mark.parametrize("variant", _BROKEN_ID_TOKENS)
async def test_invalid_id_token_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    variant: str,
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    response = await sso_login(client, id_token_variant=variant)
    assert _sso_error(response) == "sso_token_invalid"
    assert await _sso_rows(db_session) == 0


async def test_userinfo_with_another_sub_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    sso_provider.claim_config = {"use_userinfo": True}
    await db_session.flush()
    await _member(db_session, sso_provider, mock_idp)
    mock_idp.userinfo_overrides["sub"] = "someone-else"
    response = await sso_login(client)
    assert _sso_error(response) == "sso_token_invalid"
    assert mock_idp.calls["userinfo"] == 1


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


async def test_unknown_identity_is_not_provisioned(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
) -> None:
    # A local user with the same email is never matched by email.
    local = UserFactory.build(email="member@example.com")
    db_session.add(local)
    await db_session.flush()

    response = await sso_login(client)
    assert _sso_error(response) == "sso_account_not_provisioned"
    assert await _sso_rows(db_session) == 0
    assert await db_session.scalar(select(func.count()).select_from(UserIdentity)) == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "deactivated"},
        {"status": "locked", "locked_until": None},
        {"status": "pending_verification"},
    ],
    ids=["deactivated", "admin_locked", "pending_verification"],
)
async def test_inactive_account_is_disabled(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    fields: dict[str, Any],
) -> None:
    await _member(db_session, sso_provider, mock_idp, **fields)
    response = await sso_login(client)
    assert _sso_error(response) == "sso_account_disabled"
    assert await _sso_rows(db_session) == 0


async def test_service_account_is_disabled(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    # The identity trigger refuses a service account at insert time; this
    # covers an account turned into a service account afterwards.
    user = await _member(db_session, sso_provider, mock_idp)
    user.is_service_account = True
    await db_session.flush()

    response = await sso_login(client)
    assert _sso_error(response) == "sso_account_disabled"
    assert await _sso_rows(db_session) == 0


async def test_password_lockout_does_not_block_sso(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    await _member(
        db_session,
        sso_provider,
        mock_idp,
        status="locked",
        locked_until=datetime.now(UTC) + timedelta(minutes=15),
        failed_login_count=5,
    )
    response = await sso_login(client)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert await _sso_rows(db_session) == 1


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("sso_state_invalid", "Your sign-in expired or was started in another browser. Please try again."),
        ("sso_idp_error", "The identity provider refused the sign-in."),
        ("sso_idp_unreachable", "The identity provider is unavailable. Please try again later."),
        ("sso_account_disabled", "This account is disabled. Contact your administrator."),
        ("sso_unavailable", "Single sign-on is temporarily unavailable. Please try again later."),
    ],
)
async def test_login_page_shows_the_message_for_a_code(client: AsyncClient, code: str, message: str) -> None:
    page = await client.get("/login/", params={"sso_error": code})
    assert page.status_code == 200
    assert message in page.text


async def test_login_page_never_reflects_an_unknown_code(client: AsyncClient) -> None:
    page = await client.get("/login/", params={"sso_error": "<b>pwned</b>"})
    assert page.status_code == 200
    assert "pwned" not in page.text
    assert "Single sign-on failed. Please try again." in page.text


async def test_login_page_translates_the_message(client: AsyncClient) -> None:
    english = (await client.get("/login/", params={"sso_error": "sso_idp_error"})).text
    thai = (await client.get("/login/", params={"sso_error": "sso_idp_error"}, headers={"Accept-Language": "th"})).text
    assert "The identity provider refused the sign-in." in english
    assert "The identity provider refused the sign-in." not in thai


# ---------------------------------------------------------------------------
# Logs and Redis
# ---------------------------------------------------------------------------


async def test_no_secrets_reach_the_logs(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await _member(db_session, sso_provider, mock_idp)
    caplog.set_level(logging.DEBUG)

    started = await sso_login.start(client)
    state = _params(started.authorize_url)["state"]
    r = await _redis()
    try:
        record = json.loads(await r.get(tx_key(state)))
    finally:
        await r.aclose()
    callback_url = mock_idp.authorize(started.authorize_url)
    code = _params(callback_url)["code"]
    response = await sso_login.callback(client, callback_url, tx_cookie=started.tx_cookie)
    assert response.status_code == 302 and response.headers["location"] == "/"

    row = (await _session_rows(db_session, user.id))[0]
    id_token = secret_box.decrypt(row.id_token_encrypted or "", "sso-id-token")
    secrets_seen = {
        "state": state,
        "nonce": record["nonce"],
        "code": code,
        "verifier": record["code_verifier"],
        "binding": started.tx_cookie or "",
        "client_secret": DEFAULT_CLIENT_SECRET,
        "id_token": id_token,
        "access_token": response.cookies.get("access_token") or "",
        "refresh_token": response.cookies.get("refresh_token") or "",
    }
    # The test client's own httpx log lines show the callback URL it requested
    # (code and state); they are not the application's logs.
    text = "\n".join(
        record.getMessage()
        for record in caplog.records
        if not (record.name == "httpx" and "http://test/" in record.getMessage())
    )
    for name, value in secrets_seen.items():
        assert value, name
        assert value not in text, f"{name} leaked into the logs"
    assert "eyJ" not in text
    assert "sso.login.success" in text


@pytest.fixture
async def redis_down(monkeypatch: pytest.MonkeyPatch):
    """Point the shared Redis client at a port nothing listens on."""
    import specivo.core.redis as core_redis

    dead = aioredis.from_url("redis://127.0.0.1:9/0", decode_responses=True, socket_connect_timeout=0.2)
    monkeypatch.setattr(core_redis, "_redis", dead)
    monkeypatch.setattr(core_redis, "_redis_loop", asyncio.get_running_loop())
    yield
    await dead.aclose()


async def test_redis_down_fails_closed_and_password_login_still_works(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
    redis_down: None,
) -> None:
    response = await client.get(LOGIN_PATH)
    assert _sso_error(response) == "sso_unavailable"
    # No binding token is handed out; at most a stale one is cleared.
    assert "max-age=0" in _set_cookies(response).get(TX_COOKIE, "max-age=0").lower()

    callback = await client.get(CALLBACK_PATH, params={"state": "s" * 43, "code": "c"})
    assert _sso_error(callback) == "sso_unavailable"

    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    login = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text


# ---------------------------------------------------------------------------
# Stealth prefix
# ---------------------------------------------------------------------------


async def test_flow_works_under_a_stealth_prefix(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    _test_connection,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    from specivo.core.database import get_db, set_engine
    from specivo.testing.conftest_base import _create_test_app, _make_test_get_db

    monkeypatch.setenv("STEALTH_PREFIX", "/hidden")
    get_settings.cache_clear()
    try:
        app = _create_test_app()
        set_engine(db_engine)
        app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)
        user = await _member(db_session, sso_provider, mock_idp)
        driver = SsoLoginDriver(mock_idp, sso_provider)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            started = await driver.start(ac, prefix="/hidden", next_path="/hidden/projects/")
            params = _params(started.authorize_url)
            assert params["redirect_uri"] == get_settings().app_url.rstrip("/") + "/hidden" + CALLBACK_PATH
            assert _attrs(_set_cookies(started.response)[TX_COOKIE])["path"] == "/hidden/auth/oidc/"

            response = await driver.callback(ac, mock_idp.authorize(started.authorize_url), tx_cookie=started.tx_cookie)
            assert response.status_code == 302, response.text
            assert response.headers["location"] == "/hidden/projects/"
            assert (await _session_rows(db_session, user.id))[0].auth_method == "sso"

            refused = await driver.callback(ac, "/hidden" + CALLBACK_PATH + "?state=x&code=y", tx_cookie=None)
            assert refused.status_code == 302
            assert urlsplit(refused.headers["location"]).path == "/hidden/login/"
    finally:
        monkeypatch.delenv("STEALTH_PREFIX")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


@pytest.mark.serial
async def test_start_is_rate_limited_per_ip(client: AsyncClient, sso_provider: SsoProvider) -> None:
    for _ in range(20):
        assert (await client.get(LOGIN_PATH)).status_code == 302
    assert (await client.get(LOGIN_PATH)).status_code == 429


@pytest.mark.serial
async def test_callback_is_rate_limited_per_ip(client: AsyncClient, sso_provider: SsoProvider) -> None:
    for _ in range(20):
        assert (await client.get(CALLBACK_PATH, params={"state": "x"})).status_code == 302
    assert (await client.get(CALLBACK_PATH, params={"state": "x"})).status_code == 429
