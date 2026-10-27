"""Back-channel logout: the IdP ends Specivo sessions server to server.

``POST /auth/oidc/{slug}/backchannel-logout/`` takes a form-encoded
``logout_token`` (OpenID Connect Back-Channel Logout 1.0). A valid token
revokes the sessions it names: by ``sid`` (one IdP session), or every session
of the identity when only ``sub`` is given. The access tokens of a revoked
session stop working on the very next request.

Answers: 200 on success, also when nothing matched; 400
``{"error": "invalid_request"}`` for any invalid, replayed or missing token;
404 when the provider is unknown, disabled or has back-channel logout off;
503 when the replay cache (Redis) or the IdP keys are unavailable, so the IdP
may retry. ``Cache-Control: no-store`` on every answer.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.sso.provisioning import subject_hash
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import LOGOUT_TOKEN_VARIANTS, OMIT, MockIdP
from specivo.testing.sso import SsoLoginDriver, link_identity

pytestmark = pytest.mark.integration

URL = "/auth/oidc/test-idp/backchannel-logout/"
INVALID = {"error": "invalid_request"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def backchannel_provider(db_session: AsyncSession, sso_provider: SsoProvider) -> SsoProvider:
    sso_provider.backchannel_logout_enabled = True
    await db_session.flush()
    return sso_provider


async def _member(db: AsyncSession, provider: SsoProvider, idp: MockIdP) -> User:
    user = UserFactory.build()
    db.add(user)
    await db.flush()
    await link_identity(db, user, provider, subject=idp.default_sub)
    return user


async def _sso_session(client: AsyncClient, sso_login: SsoLoginDriver, *, sid: str) -> str:
    """Sign in through the mock IdP with IdP session ``sid``; return the access token."""
    response = await sso_login(client, claims={"sid": sid})
    assert response.status_code == 302, response.text
    assert "sso_error" not in response.headers["location"], response.headers["location"]
    access = response.cookies.get("access_token")
    assert access
    client.cookies.clear()
    return access


async def _password_session(client: AsyncClient, user: User) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return resp.json()["access_token"]


async def _status(client: AsyncClient, access: str) -> Response:
    return await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access}"})


async def _assert_alive(client: AsyncClient, access: str) -> None:
    resp = await _status(client, access)
    assert resp.status_code == 200, resp.text


async def _assert_revoked(client: AsyncClient, access: str) -> None:
    resp = await _status(client, access)
    assert resp.status_code == 401, resp.text
    assert resp.json()["errors"][0]["code"] == "auth_token_revoked"


async def _post(client: AsyncClient, token: str | None, *, url: str = URL, **kwargs: Any) -> Response:
    data = {"logout_token": token} if token is not None else {}
    return await client.post(url, data=data, **kwargs)


async def _row_count(db: AsyncSession, user: User) -> int:
    return await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user.id)) or 0


def _assert_invalid(resp: Response) -> None:
    assert resp.status_code == 400, resp.text
    assert resp.json() == INVALID
    assert resp.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def test_sid_and_sub_revoke_only_that_idp_session(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    user = await _member(db_session, backchannel_provider, mock_idp)
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    other_sso = await _sso_session(client, sso_login, sid="idp-session-b")
    password = await _password_session(client, user)

    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))

    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    await _assert_revoked(client, target)
    await _assert_alive(client, other_sso)
    await _assert_alive(client, password)
    assert await _row_count(db_session, user) == 2


async def test_sub_only_revokes_every_session_of_the_identity(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    user = await _member(db_session, backchannel_provider, mock_idp)
    first = await _sso_session(client, sso_login, sid="idp-session-a")
    second = await _sso_session(client, sso_login, sid="idp-session-b")
    password = await _password_session(client, user)

    resp = await _post(client, mock_idp.mint_logout_token(sid=OMIT))

    assert resp.status_code == 200, resp.text
    await _assert_revoked(client, first)
    await _assert_revoked(client, second)
    await _assert_alive(client, password)


async def test_sid_only_revokes_that_idp_session_at_this_provider(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, backchannel_provider, mock_idp)
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    other = await _sso_session(client, sso_login, sid="idp-session-b")

    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a", sub=OMIT))

    assert resp.status_code == 200, resp.text
    await _assert_revoked(client, target)
    await _assert_alive(client, other)


async def test_sid_of_another_identity_is_not_touched(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    """``sub`` scopes ``sid``: a token naming another subject leaves this session alone."""
    await _member(db_session, backchannel_provider, mock_idp)
    access = await _sso_session(client, sso_login, sid="idp-session-a")

    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a", sub="someone-else"))

    assert resp.status_code == 200, resp.text
    await _assert_alive(client, access)


async def test_unknown_subject_is_200_and_revokes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, backchannel_provider, mock_idp)
    access = await _sso_session(client, sso_login, sid="idp-session-a")

    resp = await _post(client, mock_idp.mint_logout_token(sub="unknown-subject", sid=OMIT))

    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    await _assert_alive(client, access)


async def test_works_with_a_session_cookie_and_no_csrf_token(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    """The route is CSRF-exempt: the signed logout token is the proof."""
    await _member(db_session, backchannel_provider, mock_idp)
    access = await _sso_session(client, sso_login, sid="idp-session-a")

    client.cookies.set("access_token", "stale-session")
    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))
    client.cookies.clear()

    assert resp.status_code == 200, resp.text
    await _assert_revoked(client, access)


# ---------------------------------------------------------------------------
# Invalid tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", LOGOUT_TOKEN_VARIANTS)
async def test_every_broken_token_is_refused(
    variant: str,
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, backchannel_provider, mock_idp)
    access = await _sso_session(client, sso_login, sid=mock_idp.default_sid)

    resp = await _post(client, mock_idp.logout_token_variant(variant))

    _assert_invalid(resp)
    await _assert_alive(client, access)


async def test_replayed_token_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    await _member(db_session, backchannel_provider, mock_idp)
    token = mock_idp.mint_logout_token(sid="idp-session-a")

    assert (await _post(client, token)).status_code == 200
    # A new session from the same IdP session must not be cut off by a replay.
    access = await _sso_session(client, sso_login, sid="idp-session-a")
    _assert_invalid(await _post(client, token))
    await _assert_alive(client, access)


async def test_token_from_another_issuer_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    """Same signing key, same client id, another issuer: still refused."""
    await _member(db_session, backchannel_provider, mock_idp)
    access = await _sso_session(client, sso_login, sid="idp-session-a")
    other = MockIdP(issuer="https://other-idp.example.com/realms/other", client_id=mock_idp.client_id)

    resp = await _post(client, other.mint_logout_token(sid="idp-session-a", sub=mock_idp.default_sub))

    _assert_invalid(resp)
    await _assert_alive(client, access)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"logout_token": ""},
        {"something_else": "x"},
    ],
)
async def test_missing_token_is_refused(
    body: dict[str, str], client: AsyncClient, backchannel_provider: SsoProvider
) -> None:
    _assert_invalid(await client.post(URL, data=body))


async def test_repeated_token_parameter_is_refused(
    client: AsyncClient, backchannel_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    token = mock_idp.mint_logout_token()
    resp = await client.post(
        URL,
        content=f"logout_token={token}&logout_token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    _assert_invalid(resp)


async def test_json_body_is_refused(client: AsyncClient, backchannel_provider: SsoProvider, mock_idp: MockIdP) -> None:
    _assert_invalid(await client.post(URL, json={"logout_token": mock_idp.mint_logout_token()}))


async def test_oversized_body_is_refused(client: AsyncClient, backchannel_provider: SsoProvider) -> None:
    resp = await client.post(
        URL,
        content="logout_token=" + "a" * 20_000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    _assert_invalid(resp)


# ---------------------------------------------------------------------------
# Provider gating
# ---------------------------------------------------------------------------


async def test_flag_off_is_404(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    assert sso_provider.backchannel_logout_enabled is False
    resp = await _post(client, mock_idp.mint_logout_token())
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"


async def test_disabled_provider_is_404(
    client: AsyncClient, db_session: AsyncSession, backchannel_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    backchannel_provider.enabled = False
    await db_session.flush()
    resp = await _post(client, mock_idp.mint_logout_token())
    assert resp.status_code == 404


async def test_unknown_provider_is_404(
    client: AsyncClient, backchannel_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    resp = await _post(client, mock_idp.mint_logout_token(), url="/auth/oidc/no-such-idp/backchannel-logout/")
    assert resp.status_code == 404


async def test_rate_limited_to_60_per_minute_per_ip(client: AsyncClient, backchannel_provider: SsoProvider) -> None:
    statuses = [(await client.post(URL, data={})).status_code for _ in range(61)]
    assert statuses[:60] == [400] * 60
    assert statuses[60] == 429


async def test_get_is_not_allowed(client: AsyncClient, backchannel_provider: SsoProvider) -> None:
    assert (await client.get(URL)).status_code == 405


# ---------------------------------------------------------------------------
# Failures on our side
# ---------------------------------------------------------------------------


async def test_replay_cache_down_is_503_and_revokes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the replay cache the token is not acted on; 503 lets the IdP retry."""
    from specivo.services.sso import logout as logout_module

    await _member(db_session, backchannel_provider, mock_idp)
    access = await _sso_session(client, sso_login, sid="idp-session-a")

    async def broken_claim(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr(logout_module, "_claim_jti", broken_claim)
    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"error": "temporarily_unavailable"}
    assert resp.headers["cache-control"] == "no-store"
    await _assert_alive(client, access)


async def test_idp_keys_unreachable_is_503(
    client: AsyncClient,
    db_session: AsyncSession,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    from specivo.core.redis import get_redis
    from specivo.services.sso.discovery import discovery_cache_key

    token = mock_idp.mint_logout_token()
    redis = await get_redis()
    await redis.delete(discovery_cache_key(backchannel_provider.id))
    backchannel_provider.discovery_cache = None
    await db_session.flush()
    mock_idp.set_fault("discovery", status=500)

    resp = await _post(client, token)

    assert resp.status_code == 503, resp.text
    assert resp.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


async def test_log_line_has_hashed_subject_and_no_token_contents(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    backchannel_provider: SsoProvider,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _member(db_session, backchannel_provider, mock_idp)
    await _sso_session(client, sso_login, sid="idp-session-secret-sid")
    token = mock_idp.mint_logout_token(sid="idp-session-secret-sid")
    invalid = mock_idp.logout_token_variant("with_nonce")

    with caplog.at_level(logging.DEBUG):
        assert (await _post(client, token)).status_code == 200
        assert (await _post(client, invalid)).status_code == 400

    messages = [r.getMessage() for r in caplog.records]
    lines = [m for m in messages if m.startswith("sso.backchannel_logout ")]
    assert lines == [
        f"sso.backchannel_logout provider=test-idp sub={subject_hash(mock_idp.default_sub)} by=sid revoked=1"
    ]
    everything = "\n".join(messages)
    for secret in (token, invalid, mock_idp.default_sub, "idp-session-secret-sid"):
        assert secret not in everything
    for part in token.split("."):
        assert part not in everything
