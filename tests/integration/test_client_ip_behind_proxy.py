"""The client address Specivo records and meters, behind a trusted reverse proxy.

The test client connects from ``127.0.0.1``. Each test makes that peer a
trusted proxy (as the bundled nginx is in the compose stack) and sends the
``X-Forwarded-For`` nginx would send, so the client is ``203.0.113.x``.
Sessions, the audit context and the per-IP buckets must all see that address,
not the proxy's.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.middleware import AuditBatchMiddleware
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import SsoLoginDriver, build_sso_provider, link_identity

pytestmark = pytest.mark.integration

CLIENT = "203.0.113.7"


@pytest.fixture
def behind_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust the test client's peer address, as compose trusts nginx."""
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["127.0.0.1/32"])


async def _session_ips(db: AsyncSession, user: User) -> list[str | None]:
    rows = await db.execute(select(RefreshToken.ip_address).where(RefreshToken.user_id == user.id))
    return list(rows.scalars())


async def test_password_login_records_the_client_behind_a_trusted_proxy(
    client: AsyncClient, db_session: AsyncSession, behind_proxy: None
) -> None:
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": f"198.51.100.1, {CLIENT}"},
    )

    assert resp.status_code == 200, resp.text
    assert await _session_ips(db_session, user) == [CLIENT]


async def test_password_login_ignores_the_header_from_an_untrusted_peer(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": CLIENT},
    )

    assert resp.status_code == 200, resp.text
    assert await _session_ips(db_session, user) == ["127.0.0.1"]


async def test_sso_login_records_the_client_behind_a_trusted_proxy(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    behind_proxy: None,
) -> None:
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    await link_identity(db_session, user, sso_provider, subject=mock_idp.default_sub)
    client.headers["X-Forwarded-For"] = CLIENT

    response = await sso_login(client)

    assert response.status_code == 302, response.text
    assert "sso_error" not in response.headers["location"], response.headers["location"]
    assert await _session_ips(db_session, user) == [CLIENT]


async def test_audit_context_gets_the_client_behind_a_trusted_proxy(behind_proxy: None) -> None:
    from specivo.services.security_audit_service import _audit_context

    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        context = _audit_context.get()
        seen["ip"] = context.ip_address if context else None

    scope = {
        "type": "http",
        "client": ("127.0.0.1", 4321),
        "headers": [(b"x-forwarded-for", CLIENT.encode())],
    }
    await AuditBatchMiddleware(app)(scope, None, None)  # type: ignore[arg-type]

    assert seen["ip"] == CLIENT


# ---------------------------------------------------------------------------
# Back-channel logout: one bucket per provider and sender
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_backchannel_providers(
    db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> tuple[SsoProvider, SsoProvider]:
    sso_provider.backchannel_logout_enabled = True
    other = build_sso_provider(mock_idp, slug="other-idp", display_name="Other IdP", position=1)
    other.backchannel_logout_enabled = True
    db_session.add(other)
    await db_session.flush()
    return sso_provider, other


def _url(provider: SsoProvider) -> str:
    return f"/auth/oidc/{provider.slug}/backchannel-logout/"


ATTACKER = {"X-Forwarded-For": "198.51.100.66"}
IDP = {"X-Forwarded-For": "203.0.113.200"}


async def _flood(client: AsyncClient, provider: SsoProvider) -> None:
    """One sender spends its whole budget on ``provider`` with junk."""
    for _ in range(60):
        assert (await client.post(_url(provider), data={}, headers=ATTACKER)).status_code == 400
    assert (await client.post(_url(provider), data={}, headers=ATTACKER)).status_code == 429


async def test_flood_from_one_sender_does_not_block_the_idp(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_login: SsoLoginDriver,
    two_backchannel_providers: tuple[SsoProvider, SsoProvider],
    mock_idp: MockIdP,
    behind_proxy: None,
) -> None:
    provider, _ = two_backchannel_providers
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    await link_identity(db_session, user, provider, subject=mock_idp.default_sub)
    login = await sso_login(client, claims={"sid": "idp-session-a"})
    assert login.status_code == 302, login.text
    client.cookies.clear()

    await _flood(client, provider)

    # The IdP, sending from its own address, still gets its valid token through.
    resp = await client.post(
        _url(provider), data={"logout_token": mock_idp.mint_logout_token(sid="idp-session-a")}, headers=IDP
    )
    assert resp.status_code == 200, resp.text


async def test_flood_on_one_provider_does_not_block_another(
    client: AsyncClient, two_backchannel_providers: tuple[SsoProvider, SsoProvider], behind_proxy: None
) -> None:
    first, second = two_backchannel_providers

    await _flood(client, first)

    assert (await client.post(_url(second), data={}, headers=ATTACKER)).status_code == 400


async def test_unknown_provider_does_not_spend_a_bucket(
    client: AsyncClient, two_backchannel_providers: tuple[SsoProvider, SsoProvider]
) -> None:
    first, _ = two_backchannel_providers
    for _ in range(61):
        assert (await client.post("/auth/oidc/no-such-idp/backchannel-logout/", data={})).status_code == 404
    assert (await client.post(_url(first), data={})).status_code == 400
