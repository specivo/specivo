"""An SSO session is renewed only while its IdP still accepts the session's refresh token.

Against the mock IdP: what a sign-in stores (and the probe that decides it),
renewal through ``POST /api/v1/auth/refresh/`` when the IdP accepts, refuses
(``invalid_grant``), rotates or cannot be reached, the per-session rate limit,
and the providers whose sessions are renewed as before (no refresh token, a
refresh token the IdP refuses at once, the check switched off).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.sso.discovery import invalidate_provider_cache
from specivo.services.sso.http import override_transport
from specivo.services.sso.tokens import idp_refresh_token_purpose
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import SsoLoginDriver, link_identity

pytestmark = pytest.mark.integration

REFRESH_PATH = "/api/v1/auth/refresh/"


async def _member(db: AsyncSession, provider: SsoProvider, idp: MockIdP) -> User:
    user = UserFactory.build()
    db.add(user)
    await db.flush()
    await link_identity(db, user, provider, subject=idp.default_sub)
    return user


async def _sign_in(client: AsyncClient, sso_login: SsoLoginDriver) -> str:
    """Sign in through the mock IdP; returns the Specivo refresh token."""
    response = await sso_login(client)
    assert response.status_code == 302, response.text
    token = response.cookies.get("refresh_token")
    assert token
    return token


async def _row(db: AsyncSession, user_id: int) -> RefreshToken | None:
    db.expire_all()
    return (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))).scalar_one_or_none()


async def _make_check_due(db: AsyncSession, user_id: int) -> None:
    """Pretend the session was last checked at the IdP an hour ago."""
    await db.execute(
        update(RefreshToken).where(RefreshToken.user_id == user_id).values(idp_checked_at=utcnow() - timedelta(hours=1))
    )
    await db.flush()


async def _refresh(client: AsyncClient, token: str):
    return await client.post(REFRESH_PATH, json={"refresh_token": token})


def _unseal(row: RefreshToken, idp: MockIdP) -> str:
    """The IdP refresh token of ``row``, sealed for ``idp``'s issuer, token endpoint and client."""
    assert row.idp_refresh_token_encrypted
    purpose = idp_refresh_token_purpose(idp.issuer, idp.token_endpoint, idp.client_id)
    return secret_box.decrypt(row.idp_refresh_token_encrypted, purpose)


class _Router:
    """One transport for several mock IdPs (by host) that records every request."""

    def __init__(self, *idps: MockIdP) -> None:
        self.idps = {httpx.URL(idp.issuer).host: idp for idp in idps}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        idp = self.idps.get(request.url.host)
        if idp is None:
            return httpx.Response(404, json={"error": "not_found"})
        return idp.transport.handle_request(request)

    def refresh_requests(self) -> list[str]:
        """URLs that were sent a refresh token."""
        return [
            str(r.url)
            for r in self.requests
            if r.method == "POST" and parse_qs(r.content.decode()).get("grant_type") == ["refresh_token"]
        ]


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


async def test_sign_in_keeps_the_idp_refresh_token_sealed(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id

    await _sign_in(client, sso_login)

    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted
    raw = _unseal(row, mock_idp)
    assert raw not in row.idp_refresh_token_encrypted
    assert row.idp_checked_at is not None and utcnow() - row.idp_checked_at < timedelta(minutes=1)
    # The token was tried once at sign-in, and works.
    assert mock_idp.grants["refresh_token"] == 1
    await db_session.refresh(sso_provider)
    assert sso_provider.session_recheck_status == "ok"
    assert sso_provider.session_recheck_status_at is not None


async def test_sign_in_without_refresh_token_is_not_rechecked(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    mock_idp.issue_refresh_tokens = False
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)

    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted is None
    await db_session.refresh(sso_provider)
    assert sso_provider.session_recheck_status == "no_refresh_token"

    # Renewed as before: the IdP is never asked.
    assert (await _refresh(client, token)).status_code == 200
    assert mock_idp.grants["refresh_token"] == 0


async def test_refresh_token_refused_at_sign_in_is_dropped(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    # An IdP that issues refresh tokens the client may not use (authentik
    # without the refresh_token grant): the sessions must not all end.
    mock_idp.refresh_grant_allowed = False
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)

    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted is None
    await db_session.refresh(sso_provider)
    assert sso_provider.session_recheck_status == "refresh_refused"

    assert (await _refresh(client, token)).status_code == 200


async def test_sign_in_with_the_check_off_stores_nothing(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    sso_provider.verify_session_at_idp = False
    await db_session.flush()
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)

    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted is None
    assert mock_idp.grants["refresh_token"] == 0
    await db_session.refresh(sso_provider)
    assert sso_provider.session_recheck_status is None

    mock_idp.revoke_refresh_tokens()
    await _make_check_due(db_session, user)
    assert (await _refresh(client, token)).status_code == 200


async def test_turning_the_check_off_stops_it_for_open_sessions(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    sso_provider.verify_session_at_idp = False
    await db_session.flush()
    mock_idp.revoke_refresh_tokens()
    await _make_check_due(db_session, user)

    assert (await _refresh(client, token)).status_code == 200


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------


async def test_renewal_asks_the_idp_and_records_the_check(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    await _make_check_due(db_session, user)
    before = mock_idp.grants["refresh_token"]

    response = await _refresh(client, token)

    assert response.status_code == 200, response.text
    assert mock_idp.grants["refresh_token"] == before + 1
    row = await _row(db_session, user)
    assert row is not None and row.idp_checked_at is not None
    assert utcnow() - row.idp_checked_at < timedelta(minutes=1)
    assert row.idp_refresh_token_encrypted


async def test_renewal_is_rate_limited_per_session(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    await _member(db_session, sso_provider, mock_idp)
    token = await _sign_in(client, sso_login)
    before = mock_idp.grants["refresh_token"]

    # Checked at sign-in, moments ago: the next renewals do not ask again.
    first = await _refresh(client, token)
    assert first.status_code == 200
    second = await _refresh(client, first.json()["refresh_token"])
    assert second.status_code == 200
    assert mock_idp.grants["refresh_token"] == before


async def test_recheck_interval_is_configurable(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSO_IDP_RECHECK_MINUTES", "0")
    get_settings.cache_clear()
    try:
        await _member(db_session, sso_provider, mock_idp)
        token = await _sign_in(client, sso_login)
        before = mock_idp.grants["refresh_token"]
        response = await _refresh(client, token)
        assert response.status_code == 200
        assert mock_idp.grants["refresh_token"] == before + 1
    finally:
        monkeypatch.delenv("SSO_IDP_RECHECK_MINUTES")
        get_settings.cache_clear()


async def test_invalid_grant_ends_the_session(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    login = await sso_login(client)
    token, access = login.cookies.get("refresh_token"), login.cookies.get("access_token")
    # The user is disabled at the IdP: it refuses their refresh tokens.
    mock_idp.revoke_refresh_tokens(mock_idp.default_sub)
    await _make_check_due(db_session, user)

    with caplog.at_level(logging.INFO):
        response = await _refresh(client, token)

    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "auth_refresh_expired"
    assert await _row(db_session, user) is None
    # The access tokens already issued are refused at once, not when they expire.
    sessions = await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access}"})
    assert sessions.status_code == 401
    # And the session cannot come back with the old refresh token.
    assert (await _refresh(client, token)).status_code == 401
    messages = [r.getMessage() for r in caplog.records]
    assert any("sso.session.rejected" in m and "reason=idp_refresh_rejected" in m for m in messages), messages
    revoked = [r for r in caplog.records if getattr(r, "event", None) == "sessions_revoked"]
    assert revoked and revoked[-1].reason == "idp_refresh_rejected"


async def test_rotated_idp_refresh_token_is_kept(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    mock_idp.rotate_refresh_tokens = True
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted
    at_sign_in = _unseal(row, mock_idp)

    await _make_check_due(db_session, user)
    first = await _refresh(client, token)
    assert first.status_code == 200
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted
    rotated = _unseal(row, mock_idp)
    assert rotated != at_sign_in
    assert mock_idp.live_refresh_tokens == 1

    # The rotated one is what the next check presents, and the IdP accepts it.
    await _make_check_due(db_session, user)
    second = await _refresh(client, first.json()["refresh_token"])
    assert second.status_code == 200


@pytest.mark.parametrize("fault", [{"timeout": True}, {"status": 503}])
async def test_unreachable_idp_allows_by_default(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
    fault: dict,
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    await _make_check_due(db_session, user)
    checked_before = (await _row(db_session, user)).idp_checked_at  # type: ignore[union-attr]
    mock_idp.set_fault("token", **fault)

    with caplog.at_level(logging.WARNING):
        response = await _refresh(client, token)

    assert response.status_code == 200
    row = await _row(db_session, user)
    assert row is not None and row.idp_checked_at == checked_before  # asked again next time
    assert any(
        "sso.session.idp_check_failed" in r.getMessage() and "action=allow" in r.getMessage() for r in caplog.records
    )

    # Once the IdP answers again, the next renewal asks it.
    mock_idp.clear_faults()
    before = mock_idp.grants["refresh_token"]
    assert (await _refresh(client, response.json()["refresh_token"])).status_code == 200
    assert mock_idp.grants["refresh_token"] == before + 1


@pytest.mark.parametrize("fault", [{"timeout": True}, {"status": 502}])
async def test_unreachable_idp_ends_the_session_with_deny(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    fault: dict,
) -> None:
    sso_provider.idp_check_failure = "deny"
    await db_session.flush()
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    await _make_check_due(db_session, user)
    mock_idp.set_fault("token", **fault)

    response = await _refresh(client, token)

    assert response.status_code == 401
    assert await _row(db_session, user) is None


async def test_other_idp_refusal_follows_the_failure_policy(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    # invalid_client says the configuration broke, not that the user left.
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    await _make_check_due(db_session, user)
    mock_idp.client_secret = "rotated-at-the-idp"

    assert (await _refresh(client, token)).status_code == 200


async def test_password_session_never_asks_the_idp(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    user = await _member(db_session, sso_provider, mock_idp)
    login = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text
    mock_idp.revoke_refresh_tokens()

    response = await _refresh(client, login.json()["refresh_token"])

    assert response.status_code == 200
    assert mock_idp.grants["refresh_token"] == 0


async def test_browser_is_signed_out_when_the_idp_refuses(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    """The silent refresh of a page load ends in the login page, not a renewed session."""
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    mock_idp.revoke_refresh_tokens()
    await _make_check_due(db_session, user)

    # An expired access cookie is the same as none: the page renews from the refresh cookie.
    page = await client.get("/my/profile/", headers={"Cookie": f"refresh_token={token}"})

    assert page.status_code in (302, 303)
    assert "/login/" in page.headers["location"]
    assert await _row(db_session, user) is None


# ---------------------------------------------------------------------------
# The stored token belongs to the endpoint that issued it
# ---------------------------------------------------------------------------


async def test_token_is_never_sent_to_a_new_issuer(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    # The provider row now names another IdP (edited around the API, so
    # nothing cleared the stored token).
    other = MockIdP(issuer="https://other-idp.example.net/realms/test")
    sso_provider.issuer = other.issuer
    sso_provider.discovery_cache = None
    await db_session.flush()
    await invalidate_provider_cache(sso_provider.id)
    await _make_check_due(db_session, user)
    router = _Router(mock_idp, other)

    with override_transport(router.transport):
        response = await _refresh(client, token)

    assert response.status_code == 200
    assert router.refresh_requests() == []
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted is None


async def test_token_is_never_sent_to_a_new_token_endpoint(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    # The IdP's discovery document now points the token endpoint elsewhere.
    moved = "https://tokens.example.net/oauth/token"
    mock_idp.discovery_overrides["token_endpoint"] = moved
    sso_provider.discovery_cache = None
    await db_session.flush()
    await invalidate_provider_cache(sso_provider.id)
    await _make_check_due(db_session, user)
    router = _Router(mock_idp)

    with override_transport(router.transport):
        response = await _refresh(client, token)

    assert response.status_code == 200
    assert router.refresh_requests() == []
    assert not [r for r in router.requests if r.url.host == "tokens.example.net"]
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted is None


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other-idp.example.net/realms/test"},
        {"discovery_url": "https://idp.example.com/realms/test/other/.well-known/openid-configuration"},
        {"client_id": "another-client"},
    ],
)
async def test_changing_the_endpoint_drops_stored_tokens(
    client: AsyncClient,
    admin_client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    change: dict,
) -> None:
    provider_id = sso_provider.id
    user = (await _member(db_session, sso_provider, mock_idp)).id
    await _sign_in(client, sso_login)
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted

    response = await admin_client.patch(f"/api/v1/admin/sso/providers/{provider_id}/", json=change)

    assert response.status_code == 200, response.text
    row = await _row(db_session, user)
    assert row is not None
    assert row.idp_refresh_token_encrypted is None and row.idp_checked_at is None


async def test_other_changes_keep_stored_tokens(
    client: AsyncClient,
    admin_client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
) -> None:
    user = (await _member(db_session, sso_provider, mock_idp)).id
    await _sign_in(client, sso_login)

    response = await admin_client.patch(
        f"/api/v1/admin/sso/providers/{sso_provider.id}/", json={"display_name": "Renamed"}
    )

    assert response.status_code == 200, response.text
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted


# ---------------------------------------------------------------------------
# No database lock or transaction while the IdP is asked
# ---------------------------------------------------------------------------


class _IdpSpy(httpx.AsyncBaseTransport):
    """Passes requests to ``idp`` and runs ``on_refresh`` before answering a refresh grant."""

    def __init__(self, idp: MockIdP, on_refresh) -> None:
        self.idp = idp
        self.on_refresh = on_refresh

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        if parse_qs(request.content.decode()).get("grant_type") == ["refresh_token"]:
            await self.on_refresh()
        return self.idp.transport.handle_request(request)


async def test_idp_is_asked_with_no_session_lock_and_no_open_transaction(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow IdP must not hold a pooled connection, the session lock or the claimed row."""
    from specivo.services.session_service import SessionService

    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    await _make_check_due(db_session, user)

    steps: list[str] = []
    lock_sessions = SessionService.lock_sessions
    commit = AsyncSession.commit

    async def _lock(self, db, session_ids):
        steps.append("lock")
        return await lock_sessions(self, db, session_ids)

    async def _commit(self):
        steps.append("commit")
        return await commit(self)

    async def _asked() -> None:
        steps.append("idp")

    monkeypatch.setattr(SessionService, "lock_sessions", _lock)
    monkeypatch.setattr(AsyncSession, "commit", _commit)
    with override_transport(_IdpSpy(mock_idp, _asked)):
        response = await _refresh(client, token)

    assert response.status_code == 200, response.text
    assert "idp" in steps, steps
    before_idp = steps[: steps.index("idp")]
    # The read transaction ended before the IdP was asked, and the session
    # was locked only afterwards.
    assert before_idp and before_idp[-1] == "commit", steps
    assert "lock" not in before_idp, steps
    assert "lock" in steps[steps.index("idp") :], steps


async def test_concurrent_renewal_does_not_present_the_idp_token_twice(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    """A second renewal of the same refresh token while the IdP is asked is refused, not checked again.

    With rotation, presenting the old IdP token again would be refused
    (``invalid_grant``) and could end a healthy session.
    """
    mock_idp.rotate_refresh_tokens = True
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    await _make_check_due(db_session, user)
    before = mock_idp.grants["refresh_token"]
    racing: list[int] = []

    async def _race() -> None:
        if not racing:
            racing.append((await _refresh(client, token)).status_code)

    with override_transport(_IdpSpy(mock_idp, _race)):
        response = await _refresh(client, token)

    assert response.status_code == 200, response.text
    assert racing == [401]
    assert mock_idp.grants["refresh_token"] == before + 1
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted
    # The session goes on with the rotated token.
    await _make_check_due(db_session, user)
    assert (await _refresh(client, response.json()["refresh_token"])).status_code == 200


async def test_idp_refresh_token_is_never_logged(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_idp.rotate_refresh_tokens = True
    user = (await _member(db_session, sso_provider, mock_idp)).id
    with caplog.at_level(logging.DEBUG):
        token = await _sign_in(client, sso_login)
        row = await _row(db_session, user)
        assert row is not None
        raw = _unseal(row, mock_idp)
        await _make_check_due(db_session, user)
        first = await _refresh(client, token)
        assert first.status_code == 200
        row = await _row(db_session, user)
        assert row is not None
        rotated = _unseal(row, mock_idp)
        mock_idp.revoke_refresh_tokens()
        await _make_check_due(db_session, user)
        assert (await _refresh(client, first.json()["refresh_token"])).status_code == 401

    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert raw not in logged
    assert rotated not in logged


async def test_idp_answer_is_ignored_when_the_stored_token_was_dropped_meanwhile(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, sso_login, mock_idp: MockIdP
) -> None:
    """The provider's client changed while the IdP was asked: its refusal does not end the session."""
    user = (await _member(db_session, sso_provider, mock_idp)).id
    token = await _sign_in(client, sso_login)
    mock_idp.revoke_refresh_tokens()
    await _make_check_due(db_session, user)

    async def _drop() -> None:
        await db_session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user)
            .values(idp_refresh_token_encrypted=None, idp_checked_at=None)
        )
        await db_session.flush()

    with override_transport(_IdpSpy(mock_idp, _drop)):
        response = await _refresh(client, token)

    assert response.status_code == 200, response.text
    row = await _row(db_session, user)
    assert row is not None and row.idp_refresh_token_encrypted is None and row.idp_checked_at is None
