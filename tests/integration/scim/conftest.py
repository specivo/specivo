"""Fixtures for the SCIM endpoint tests.

``scim_provider`` is an SSO provider with SCIM on and a fresh token;
``scim`` is a :class:`ScimClient` that sends that token. Request bodies
modelled on what Microsoft Entra ID and Okta document are in
``tests/fixtures/scim/`` and are read with :func:`fixture`, which fills
``{placeholders}``.

Logins, emails and slugs carry per-module prefixes because modules run in
parallel inside uncommitted transactions.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider
from specivo.services.scim.config_service import ScimConfigService

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "scim"
SCIM_JSON = "application/scim+json"


def fixture(name: str, **values: str) -> Any:
    """The JSON request ``name`` with ``{key}`` placeholders replaced."""
    text = (FIXTURES / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return json.loads(text)


class ScimClient:
    """Calls ``/scim/v2/{slug}`` with a bearer token."""

    def __init__(self, client: AsyncClient, slug: str, token: str | None) -> None:
        self.client = client
        self.slug = slug
        self.token = token

    def url(self, path: str) -> str:
        return f"/scim/v2/{self.slug}{path}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": SCIM_JSON}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    async def get(self, path: str, **params: Any) -> Response:
        return await self.client.get(self.url(path), params=params or None, headers=self._headers())

    async def send(
        self, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None
    ) -> Response:
        content = json.dumps(body).encode() if body is not None else None
        merged = {"Content-Type": SCIM_JSON, **(headers or {})}
        return await self.client.request(method, self.url(path), content=content, headers=self._headers(merged))

    async def post(self, path: str, body: Any) -> Response:
        return await self.send("POST", path, body)

    async def put(self, path: str, body: Any) -> Response:
        return await self.send("PUT", path, body)

    async def patch(self, path: str, body: Any) -> Response:
        return await self.send("PATCH", path, body)

    async def delete(self, path: str) -> Response:
        return await self.client.delete(self.url(path), headers=self._headers())

    def other(self, *, slug: str | None = None, token: str | None = None) -> ScimClient:
        return ScimClient(self.client, slug or self.slug, token)


async def make_scim_provider(db: AsyncSession, slug: str, **overrides: Any) -> tuple[SsoProvider, str]:
    """A provider with SCIM on, and its token."""
    values: dict[str, Any] = {
        "slug": slug,
        "display_name": f"SCIM {slug}",
        "enabled": True,
        "issuer": f"https://{slug}.example.com",
        "client_id": "specivo",
        "token_endpoint_auth_method": "none",
        # Linking existing accounts by email is the admin's opt-in; most tests need it.
        "link_policy": "auto_verified_email",
    }
    values.update(overrides)
    provider = SsoProvider(**values)
    db.add(provider)
    await db.flush()
    token = await ScimConfigService().generate_token(db, provider, actor=None)
    provider.scim_enabled = True
    await db.flush()
    return provider, token


def user_body(user_name: str, email: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": user_name,
        "name": {"givenName": "Given", "familyName": "Family"},
        "displayName": f"Name of {user_name}",
        "emails": [{"value": email or user_name, "type": "work", "primary": True}],
        "active": True,
    }
    body.update(extra)
    return body


@pytest_asyncio.fixture
async def scim_provider(db_session: AsyncSession, request: Any) -> AsyncIterator[tuple[SsoProvider, str]]:
    slug = "scim-" + Path(str(request.node.fspath)).stem.removeprefix("test_scim_").replace("_", "-")[:30]
    yield await make_scim_provider(db_session, slug)


@pytest_asyncio.fixture
async def scim(client: AsyncClient, scim_provider: tuple[SsoProvider, str]) -> ScimClient:
    provider, token = scim_provider
    return ScimClient(client, provider.slug, token)
