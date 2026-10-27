"""Smoke tests for the disposable IdP and its seed.

These talk to the IdP only; no Specivo code is involved. They prove the
harness the SSO tests build on: the realm imported, its discovery document and
keys are what the RP will read, and the seeded users and groups exist and reach
the ID token.

Requires ``make sso-idp-up``; skipped otherwise (see ``tests/conftest.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio

from tests.fixtures.sso import seed

pytestmark = [pytest.mark.sso_idp, pytest.mark.integration]


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield client


async def _discovery(http: httpx.AsyncClient, issuer: str) -> dict[str, Any]:
    response = await http.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _password_grant(http: httpx.AsyncClient, username: str) -> dict[str, Any]:
    discovery = await _discovery(http, seed.KEYCLOAK_ISSUER)
    response = await http.post(
        discovery["token_endpoint"],
        data={
            "grant_type": "password",
            "client_id": seed.CONFIDENTIAL_CLIENT_ID,
            "client_secret": seed.CONFIDENTIAL_CLIENT_SECRET,
            "username": username,
            "password": seed.USER_PASSWORD,
            "scope": "openid profile email",
        },
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _verified_id_token(http: httpx.AsyncClient, raw: str) -> dict[str, Any]:
    """Decode the ID token after checking its signature against the realm's JWKS."""
    discovery = await _discovery(http, seed.KEYCLOAK_ISSUER)
    jwks = jwt.PyJWKSet.from_dict((await http.get(discovery["jwks_uri"])).json())
    kid = jwt.get_unverified_header(raw)["kid"]
    key = next(k for k in jwks.keys if k.key_id == kid)
    return jwt.decode(
        raw,
        key=key,
        algorithms=["RS256"],
        audience=seed.CONFIDENTIAL_CLIENT_ID,
        issuer=seed.KEYCLOAK_ISSUER,
    )


async def _admin_token(http: httpx.AsyncClient) -> str:
    response = await http.post(
        f"{seed.KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": seed.KEYCLOAK_ADMIN_USER,
            "password": seed.KEYCLOAK_ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _admin_get(http: httpx.AsyncClient, token: str, path: str, **params: Any) -> Any:
    response = await http.get(
        f"{seed.KEYCLOAK_BASE_URL}/admin/realms/{seed.REALM}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Discovery and keys ------------------------------------------------------


async def test_discovery_issuer_matches_configured_issuer(http: httpx.AsyncClient) -> None:
    discovery = await _discovery(http, seed.KEYCLOAK_ISSUER)

    assert discovery["issuer"] == seed.KEYCLOAK_ISSUER


async def test_discovery_advertises_logout_support(http: httpx.AsyncClient) -> None:
    discovery = await _discovery(http, seed.KEYCLOAK_ISSUER)

    assert discovery["end_session_endpoint"].startswith(seed.KEYCLOAK_ISSUER)
    assert discovery["backchannel_logout_supported"] is True
    assert discovery["backchannel_logout_session_supported"] is True
    assert "S256" in discovery["code_challenge_methods_supported"]


async def test_jwks_has_an_rs256_signing_key(http: httpx.AsyncClient) -> None:
    discovery = await _discovery(http, seed.KEYCLOAK_ISSUER)
    keys = (await http.get(discovery["jwks_uri"])).json()["keys"]

    assert any(k.get("alg") == "RS256" and k.get("use") == "sig" and k.get("kty") == "RSA" for k in keys)


# --- Seeded clients ------------------------------------------------------------


async def test_clients_are_seeded_as_designed(http: httpx.AsyncClient) -> None:
    token = await _admin_token(http)
    clients = {
        c["clientId"]: c
        for c in await _admin_get(http, token, "/clients")
        if c["clientId"] in (seed.CONFIDENTIAL_CLIENT_ID, seed.PUBLIC_CLIENT_ID)
    }

    confidential = clients[seed.CONFIDENTIAL_CLIENT_ID]
    assert confidential["publicClient"] is False
    assert set(confidential["redirectUris"]) >= {
        seed.callback_url(seed.E2E_BASE_URL, seed.KEYCLOAK_SLUG),
        seed.callback_url(seed.ASGI_BASE_URL, seed.KEYCLOAK_SLUG),
    }
    assert not any("*" in uri for uri in confidential["redirectUris"])
    attributes = confidential["attributes"]
    assert attributes["backchannel.logout.url"].endswith("/auth/oidc/keycloak/backchannel-logout/")
    assert attributes["backchannel.logout.session.required"] == "true"
    assert attributes["post.logout.redirect.uris"] == seed.POST_LOGOUT_REDIRECT_URI

    public = clients[seed.PUBLIC_CLIENT_ID]
    assert public["publicClient"] is True
    assert public["directAccessGrantsEnabled"] is False
    assert public["attributes"]["pkce.code.challenge.method"] == "S256"


# --- Seeded users and groups -------------------------------------------------


@pytest.mark.parametrize("user", seed.SEED_USERS, ids=lambda u: u.username)
async def test_seed_user_exists_with_groups(http: httpx.AsyncClient, user: seed.SeedUser) -> None:
    token = await _admin_token(http)
    found = await _admin_get(http, token, "/users", username=user.username, exact="true")
    assert len(found) == 1
    account = found[0]
    assert account["email"] == user.email
    assert account["emailVerified"] is user.email_verified

    groups = await _admin_get(http, token, f"/users/{account['id']}/groups")
    assert sorted(g["name"] for g in groups) == sorted(user.groups)


@pytest.mark.parametrize("user", seed.SEED_USERS, ids=lambda u: u.username)
async def test_id_token_carries_identity_and_groups(http: httpx.AsyncClient, user: seed.SeedUser) -> None:
    tokens = await _password_grant(http, user.username)
    claims = await _verified_id_token(http, tokens["id_token"])

    assert claims["preferred_username"] == user.username
    assert claims["email"] == user.email
    assert claims["email_verified"] is user.email_verified
    # Group names, not full paths: the mapper has full.path off.
    assert sorted(claims.get("groups", [])) == sorted(user.groups)
    assert claims["sid"]


async def test_thai_user_carries_locale_and_zoneinfo(http: httpx.AsyncClient) -> None:
    tokens = await _password_grant(http, seed.SSO_THAI.username)
    claims = await _verified_id_token(http, tokens["id_token"])

    assert claims["locale"] == "th"
    assert claims["zoneinfo"] == "Asia/Bangkok"


async def test_wrong_client_secret_is_rejected(http: httpx.AsyncClient) -> None:
    discovery = await _discovery(http, seed.KEYCLOAK_ISSUER)
    response = await http.post(
        discovery["token_endpoint"],
        data={
            "grant_type": "password",
            "client_id": seed.CONFIDENTIAL_CLIENT_ID,
            "client_secret": "not-the-secret",
            "username": seed.SSO_MEMBER.username,
            "password": seed.USER_PASSWORD,
            "scope": "openid",
        },
    )

    assert response.status_code == 401


# --- authentik (opt-in) --------------------------------------------------------


def _require_authentik() -> str:
    if not seed.AUTHENTIK_ISSUER:
        pytest.skip("authentik is opt-in: make sso-idp-up-authentik and set SSO_TEST_AUTHENTIK_ISSUER")
    return seed.AUTHENTIK_ISSUER


async def test_authentik_discovery(http: httpx.AsyncClient) -> None:
    issuer = _require_authentik()
    discovery = await _discovery(http, issuer)

    assert discovery["issuer"] == issuer
    assert discovery["end_session_endpoint"]
    keys = (await http.get(discovery["jwks_uri"])).json()["keys"]
    assert any(k.get("kty") == "RSA" and k.get("use") == "sig" for k in keys)


async def test_authentik_seed_users_and_groups(http: httpx.AsyncClient) -> None:
    issuer = _require_authentik()
    api = f"{issuer.split('/application/', 1)[0]}/api/v3"
    headers = {"Authorization": f"Bearer {seed.AUTHENTIK_API_TOKEN}"}

    for user in seed.SEED_USERS:
        response = await http.get(f"{api}/core/users/", params={"username": user.username}, headers=headers)
        assert response.status_code == 200, response.text
        found = response.json()["results"]
        assert len(found) == 1, user.username
        assert found[0]["email"] == user.email
        assert sorted(g["name"] for g in found[0]["groups_obj"] or []) == sorted(user.groups)
