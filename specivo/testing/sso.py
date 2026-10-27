"""Fixtures and helpers for SSO tests that need the database and the web app.

* ``sso_provider``: an enabled ``SsoProvider`` row (slug ``test-idp``)
  pointing at the ``mock_idp`` fixture.
* ``sso_login``: an :class:`SsoLoginDriver` that runs the browser side of a
  login (start, IdP, callback) against the ASGI app, or any single step of it.
* :func:`link_identity`: an identity row for a user at a provider.

The auth cookies are ``Secure`` (tests run with ``DEBUG=false``) and the test
clients talk plain ``http://test``, so httpx never sends them back by itself.
The driver therefore carries the ``spv_sso_tx`` cookie explicitly from the
start response to the callback request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.testing.oidc_mock import MockIdP

TX_COOKIE = "spv_sso_tx"


def build_sso_provider(idp: MockIdP, **overrides: Any) -> SsoProvider:
    """An unsaved, enabled provider row for ``idp``; ``overrides`` replace columns."""
    values: dict[str, Any] = {
        "slug": "test-idp",
        "display_name": "Test IdP",
        "enabled": True,
        "position": 0,
        "issuer": idp.issuer,
        "discovery_url": None,
        "client_id": idp.client_id,
        "token_endpoint_auth_method": idp.token_endpoint_auth_method,
        "client_secret_encrypted": (
            secret_box.encrypt(idp.client_secret, "sso-client-secret") if idp.client_secret else None
        ),
        "client_secret_env": None,
        "scopes": "openid profile email",
        "claim_config": {},
        "extra_auth_params": {},
        "session_max_age_hours": 24,
    }
    values.update(overrides)
    return SsoProvider(**values)


async def link_identity(
    db: AsyncSession,
    user: User,
    provider: SsoProvider,
    *,
    subject: str,
    linked_via: str = "admin",
) -> UserIdentity:
    """Persist an identity linking ``user`` to ``subject`` at ``provider``."""
    identity = UserIdentity(
        user_id=user.id,
        provider_id=provider.id,
        issuer=provider.issuer,
        subject=subject,
        linked_via=linked_via,
    )
    db.add(identity)
    await db.flush()
    return identity


def _path_and_query(url: str) -> str:
    split = urlsplit(url)
    return f"{split.path}?{split.query}" if split.query else split.path


@dataclass
class StartedLogin:
    """The start response, its authorize URL and the browser-binding cookie."""

    response: httpx.Response
    authorize_url: str
    tx_cookie: str | None


class SsoLoginDriver:
    """Plays the browser through an SSO login against the test app."""

    def __init__(self, idp: MockIdP, provider: SsoProvider) -> None:
        self.idp = idp
        self.provider = provider

    async def start(
        self,
        client: httpx.AsyncClient,
        *,
        slug: str | None = None,
        next_path: str | None = None,
        prefix: str = "",
    ) -> StartedLogin:
        """``GET /auth/oidc/{slug}/login/``; asserts the redirect to the IdP."""
        params = {"next": next_path} if next_path is not None else None
        response = await client.get(f"{prefix}/auth/oidc/{slug or self.provider.slug}/login/", params=params)
        assert response.status_code == 302, response.text
        return StartedLogin(
            response=response,
            authorize_url=response.headers["location"],
            tx_cookie=response.cookies.get(TX_COOKIE),
        )

    async def callback(
        self,
        client: httpx.AsyncClient,
        callback_url: str,
        *,
        tx_cookie: str | None,
        extra_cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        """``GET`` the callback URL (absolute or relative) with the given cookies."""
        cookies = dict(extra_cookies or {})
        if tx_cookie is not None:
            cookies[TX_COOKIE] = tx_cookie
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else None
        return await client.get(_path_and_query(callback_url), headers=headers)

    async def __call__(
        self,
        client: httpx.AsyncClient,
        *,
        claims: dict[str, Any] | None = None,
        next_path: str | None = None,
        id_token_variant: str | None = None,
        error: str | None = None,
        prefix: str = "",
    ) -> httpx.Response:
        """Run start, the IdP and the callback; return the callback response."""
        started = await self.start(client, next_path=next_path, prefix=prefix)
        callback_url = self.idp.authorize(
            started.authorize_url, claims=claims, id_token_variant=id_token_variant, error=error
        )
        return await self.callback(client, callback_url, tx_cookie=started.tx_cookie)


@pytest_asyncio.fixture
async def sso_provider(db_session: AsyncSession, mock_idp: MockIdP) -> AsyncIterator[SsoProvider]:
    """An enabled provider row (slug ``test-idp``) for the ``mock_idp`` fixture."""
    provider = build_sso_provider(mock_idp)
    db_session.add(provider)
    await db_session.flush()
    yield provider


@pytest.fixture
def sso_login(mock_idp: MockIdP, sso_provider: SsoProvider) -> SsoLoginDriver:
    """Run an SSO login: ``await sso_login(client, claims=...)`` returns the callback response."""
    return SsoLoginDriver(mock_idp, sso_provider)


__all__ = [
    "TX_COOKIE",
    "SsoLoginDriver",
    "StartedLogin",
    "build_sso_provider",
    "link_identity",
    "sso_login",
    "sso_provider",
]
