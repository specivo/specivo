"""What a back-channel logout ends: the provider's ``backchannel_action``.

* ``revoke_session`` (default): the sessions of the IdP session the token
  names (``test_sso_backchannel_logout.py`` covers it in depth);
* ``revoke_all_sessions``: every session of the identity, whatever ``sid``;
* ``deprovision``: for an account that signs in only through SSO, every
  session of the user and their API keys (MCP keys included) switched off,
  with a ``USER_DEPROVISIONED`` record (reason ``sso_backchannel``). The
  account stays active: a later SSO sign-in works and the keys stay off. An
  account with a password is treated as ``revoke_all_sessions``.

Repeated tokens for the same IdP session (authentik sends one per session,
sometimes twice, each with its own ``jti``) change nothing the second time.
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import ApiKey
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import AuthService
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import OMIT, MockIdP
from specivo.testing.sso import SsoLoginDriver, build_sso_provider, link_identity

pytestmark = pytest.mark.integration

URL = "/auth/oidc/test-idp/backchannel-logout/"
PROVIDERS_URL = "/api/v1/admin/sso/providers/"


@pytest.fixture
async def provider(db_session: AsyncSession, sso_provider: SsoProvider) -> SsoProvider:
    sso_provider.backchannel_logout_enabled = True
    await db_session.flush()
    return sso_provider


async def _set_action(db: AsyncSession, provider: SsoProvider, action: str) -> None:
    provider.backchannel_action = action
    await db.flush()


async def _member(db: AsyncSession, provider: SsoProvider, idp: MockIdP, *, password: bool) -> User:
    user = UserFactory.build() if password else UserFactory.build(password_hash=None)
    db.add(user)
    await db.flush()
    await link_identity(db, user, provider, subject=idp.default_sub)
    return user


async def _sso_session(client: AsyncClient, sso_login: SsoLoginDriver, *, sid: str) -> str:
    response = await sso_login(client, claims={"sid": sid})
    assert response.status_code == 302, response.text
    assert "sso_error" not in response.headers["location"], response.headers["location"]
    access = response.cookies.get("access_token")
    client.cookies.clear()
    assert access
    return access


async def _password_session(client: AsyncClient, user: User) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return resp.json()["access_token"]


async def _status(client: AsyncClient, bearer: str) -> int:
    return (await client.get("/api/v1/projects/", headers={"Authorization": f"Bearer {bearer}"})).status_code


async def _post(client: AsyncClient, token: str) -> Response:
    return await client.post(URL, data={"logout_token": token})


async def _keys(db: AsyncSession, user_id: int) -> list[bool]:
    rows = await db.execute(select(ApiKey.is_active).where(ApiKey.user_id == user_id).order_by(ApiKey.id))
    return list(rows.scalars().all())


async def _two_keys(db: AsyncSession, user: User) -> tuple[str, str]:
    _, plain = await ApiKeyService().create_key(db, user.id, "ci")
    _, mcp = await ApiKeyService().create_key(db, user.id, "agent", scopes=["mcp"])  # type: ignore[arg-type]
    return plain, mcp


def _deprovisioned(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("sso.user.deprovisioned")]


# ---------------------------------------------------------------------------
# revoke_all_sessions
# ---------------------------------------------------------------------------


async def test_revoke_all_sessions_ends_every_session_of_the_identity(
    client: AsyncClient, db_session: AsyncSession, sso_login: SsoLoginDriver, provider: SsoProvider, mock_idp: MockIdP
) -> None:
    await _set_action(db_session, provider, "revoke_all_sessions")
    user = await _member(db_session, provider, mock_idp, password=True)
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    other = await _sso_session(client, sso_login, sid="idp-session-b")
    password = await _password_session(client, user)
    plain, _ = await _two_keys(db_session, user)

    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))

    assert resp.status_code == 200, resp.text
    assert await _status(client, target) == 401
    assert await _status(client, other) == 401
    # Not the password session, and never the API keys.
    assert await _status(client, password) == 200
    assert await _status(client, plain) == 200


async def test_revoke_all_sessions_with_sid_only(
    client: AsyncClient, db_session: AsyncSession, sso_login: SsoLoginDriver, provider: SsoProvider, mock_idp: MockIdP
) -> None:
    await _set_action(db_session, provider, "revoke_all_sessions")
    await _member(db_session, provider, mock_idp, password=True)
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    other = await _sso_session(client, sso_login, sid="idp-session-b")

    resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a", sub=OMIT))

    assert resp.status_code == 200, resp.text
    assert await _status(client, target) == 401
    assert await _status(client, other) == 401


async def test_revoke_session_is_still_the_default(
    client: AsyncClient, db_session: AsyncSession, sso_login: SsoLoginDriver, provider: SsoProvider, mock_idp: MockIdP
) -> None:
    assert provider.backchannel_action == "revoke_session"
    await _member(db_session, provider, mock_idp, password=False)
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    other = await _sso_session(client, sso_login, sid="idp-session-b")

    assert (await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))).status_code == 200
    assert await _status(client, target) == 401
    assert await _status(client, other) == 200


# ---------------------------------------------------------------------------
# deprovision
# ---------------------------------------------------------------------------


async def test_deprovision_suspends_the_keys_of_an_sso_only_account(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    provider: SsoProvider,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _set_action(db_session, provider, "deprovision")
    user = await _member(db_session, provider, mock_idp, password=False)
    user_id = user.id
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    other = await _sso_session(client, sso_login, sid="idp-session-b")
    plain, mcp = await _two_keys(db_session, user)
    assert await _status(client, plain) == 200

    with caplog.at_level(logging.INFO):
        resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))

    assert resp.status_code == 200, resp.text
    assert await _status(client, target) == 401
    assert await _status(client, other) == 401
    assert await _keys(db_session, user_id) == [False, False]
    assert await _status(client, plain) == 401
    assert await _status(client, mcp) == 401
    # Not deactivated.
    status = await db_session.scalar(select(User.status).where(User.id == user_id))
    assert status == "active"
    [record] = _deprovisioned(caplog)
    assert "reason=sso_backchannel" in record.getMessage()
    assert record.audit["reason"] == "sso_backchannel"  # type: ignore[attr-defined]
    assert record.audit["api_keys_deactivated"] == 2  # type: ignore[attr-defined]
    assert record.audit["user_id"] == user_id  # type: ignore[attr-defined]

    # A later SSO sign-in works; the keys stay off.
    again = await _sso_session(client, sso_login, sid="idp-session-c")
    assert await _status(client, again) == 200
    assert await _keys(db_session, user_id) == [False, False]


async def test_deprovision_never_touches_an_account_with_a_password(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    provider: SsoProvider,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _set_action(db_session, provider, "deprovision")
    user = await _member(db_session, provider, mock_idp, password=True)
    user_id = user.id
    first = await _sso_session(client, sso_login, sid="idp-session-a")
    second = await _sso_session(client, sso_login, sid="idp-session-b")
    password = await _password_session(client, user)
    plain, mcp = await _two_keys(db_session, user)

    with caplog.at_level(logging.INFO):
        resp = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))

    assert resp.status_code == 200, resp.text
    # Treated as revoke_all_sessions.
    assert await _status(client, first) == 401
    assert await _status(client, second) == 401
    assert await _status(client, password) == 200
    assert await _keys(db_session, user_id) == [True, True]
    assert await _status(client, mcp) == 200
    assert _deprovisioned(caplog) == []


async def test_repeated_tokens_for_the_same_session_are_harmless(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    provider: SsoProvider,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _set_action(db_session, provider, "deprovision")
    user = await _member(db_session, provider, mock_idp, password=False)
    user_id = user.id
    await _sso_session(client, sso_login, sid="idp-session-a")
    await _two_keys(db_session, user)

    with caplog.at_level(logging.INFO):
        first = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))
        # Same IdP session, a new jti: what authentik sends on a second event.
        second = await _post(client, mock_idp.mint_logout_token(sid="idp-session-a"))

    assert (first.status_code, second.status_code) == (200, 200)
    assert await _keys(db_session, user_id) == [False, False]
    assert len(_deprovisioned(caplog)) == 1


async def test_deprovision_ends_sessions_from_other_providers_too(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    """Every session of the user ends, also one started through another provider."""
    await _set_action(db_session, provider, "deprovision")
    user = await _member(db_session, provider, mock_idp, password=False)
    target = await _sso_session(client, sso_login, sid="idp-session-a")
    elsewhere = build_sso_provider(MockIdP(issuer="https://other-idp.example.net/realms/test"), slug="other-idp")
    db_session.add(elsewhere)
    await db_session.flush()
    identity = await link_identity(db_session, user, elsewhere, subject="other-subject")
    other = await AuthService().create_sso_session(
        db_session, user=user, identity=identity, provider=elsewhere, id_token="not-a-real-token", idp_sid=None
    )

    assert (await _post(client, mock_idp.mint_logout_token(sid=OMIT))).status_code == 200
    assert await _status(client, target) == 401
    assert await _status(client, other.access_token) == 401


async def test_deprovision_never_reaches_the_same_subject_at_another_provider(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    provider: SsoProvider,
    mock_idp: MockIdP,
) -> None:
    """A logout token maps its ``sub`` at its own provider only, never at another with the same ``sub``."""
    await _set_action(db_session, provider, "deprovision")
    elsewhere = build_sso_provider(MockIdP(issuer="https://other-idp.example.net/realms/test"), slug="other-idp")
    db_session.add(elsewhere)
    await db_session.flush()
    bystander = UserFactory.build(password_hash=None)
    db_session.add(bystander)
    await db_session.flush()
    identity = await link_identity(db_session, bystander, elsewhere, subject=mock_idp.default_sub)
    session = await AuthService().create_sso_session(
        db_session,
        user=bystander,
        identity=identity,
        provider=elsewhere,
        id_token="not-a-real-token",
        idp_sid="idp-session-x",
    )
    plain, mcp = await _two_keys(db_session, bystander)

    # The token names default_sub, which has no identity at this provider.
    assert (await _post(client, mock_idp.mint_logout_token(sid=OMIT))).status_code == 200
    assert await _status(client, session.access_token) == 200
    assert await _keys(db_session, bystander.id) == [True, True]

    # A token for an IdP session that only exists at the other provider finds nothing here either.
    assert (await _post(client, mock_idp.mint_logout_token(sub=OMIT, sid="idp-session-x"))).status_code == 200
    assert await _status(client, plain) == 200
    assert await _status(client, session.access_token) == 200
    assert await _status(client, mcp) == 200


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["revoke_session", "revoke_all_sessions", "deprovision"])
async def test_admin_api_sets_the_action(admin_client: AsyncClient, provider: SsoProvider, action: str) -> None:
    resp = await admin_client.patch(f"{PROVIDERS_URL}{provider.id}/", json={"backchannel_action": action})
    assert resp.status_code == 200, resp.text
    assert resp.json()["backchannel_action"] == action
