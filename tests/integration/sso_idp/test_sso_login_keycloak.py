"""SSO login against the disposable Keycloak, end to end.

The browser is played by httpx: Specivo's start route redirects to Keycloak,
httpx fills Keycloak's login form, and Keycloak's redirect is handed to the
callback route. The mock IdP covers the protocol in depth; this proves the
same code works with a real IdP (cookies, iss parameter, sid, at_hash).

Requires ``make sso-idp-up``; skipped otherwise (see ``tests/conftest.py``).
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider
from specivo.testing.factories.user import UserFactory
from specivo.testing.sso import TX_COOKIE, link_identity
from tests.fixtures.sso import seed

pytestmark = [pytest.mark.sso_idp, pytest.mark.integration]

_FORM_ACTION = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', re.S)


async def _keycloak_user_id(username: str) -> str:
    """The Keycloak user id, which is the ``sub`` of its ID tokens."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        token = await http.post(
            f"{seed.KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": seed.KEYCLOAK_ADMIN_USER,
                "password": seed.KEYCLOAK_ADMIN_PASSWORD,
            },
        )
        assert token.status_code == 200, token.text
        users = await http.get(
            f"{seed.KEYCLOAK_BASE_URL}/admin/realms/{seed.REALM}/users",
            params={"username": username, "exact": "true"},
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        )
        assert users.status_code == 200, users.text
        [user] = users.json()
        return str(user["id"])


@pytest_asyncio.fixture
async def keycloak_app(
    monkeypatch: pytest.MonkeyPatch, db_engine, _test_connection
) -> AsyncIterator[httpx.AsyncClient]:
    """The app as Keycloak's redirect URIs expect it: APP_URL=http://testserver, http issuer allowed."""
    from specivo.core.database import get_db, set_engine
    from specivo.services.sso import http as sso_http
    from specivo.testing.conftest_base import _create_test_app, _make_test_get_db

    # The test IdP publishes its port on 127.0.0.1 only, while "localhost"
    # resolves to ::1 first and the SSO client connects to the first answer.
    resolve = sso_http._resolve_host

    async def _ipv4_first(host: str, port: int) -> list[str]:
        return sorted(await resolve(host, port), key=lambda address: ":" in address)

    monkeypatch.setattr(sso_http, "_resolve_host", _ipv4_first)
    monkeypatch.setenv("APP_URL", seed.ASGI_BASE_URL)
    monkeypatch.setenv("SSO_ALLOW_INSECURE_HTTP", "true")
    get_settings.cache_clear()
    try:
        app = _create_test_app()
        set_engine(db_engine)
        app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=seed.ASGI_BASE_URL) as ac:
            yield ac
    finally:
        monkeypatch.delenv("APP_URL")
        monkeypatch.delenv("SSO_ALLOW_INSECURE_HTTP")
        get_settings.cache_clear()


async def test_member_signs_in_through_keycloak(keycloak_app: httpx.AsyncClient, db_session: AsyncSession) -> None:
    provider = SsoProvider(
        slug=seed.KEYCLOAK_SLUG,
        display_name="Keycloak",
        enabled=True,
        issuer=seed.KEYCLOAK_ISSUER,
        client_id=seed.CONFIDENTIAL_CLIENT_ID,
        token_endpoint_auth_method="client_secret_basic",
        client_secret_encrypted=secret_box.encrypt(seed.CONFIDENTIAL_CLIENT_SECRET, "sso-client-secret"),
        scopes="openid profile email",
        claim_config={},
        extra_auth_params={},
    )
    db_session.add(provider)
    await db_session.flush()
    user = UserFactory.build(email=seed.SSO_MEMBER.email)
    db_session.add(user)
    await db_session.flush()
    await link_identity(db_session, user, provider, subject=await _keycloak_user_id(seed.SSO_MEMBER.username))

    # 1. Specivo sends the browser to Keycloak.
    start = await keycloak_app.get(f"/auth/oidc/{seed.KEYCLOAK_SLUG}/login/", params={"next": "/projects/"})
    assert start.status_code == 302, start.text
    authorize_url = start.headers["location"]
    assert authorize_url.startswith(seed.KEYCLOAK_ISSUER)
    tx_cookie = start.cookies[TX_COOKIE]

    # 2. The browser signs in at Keycloak.
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as browser:
        form = await browser.get(authorize_url)
        assert form.status_code == 200, form.text
        match = _FORM_ACTION.search(form.text)
        assert match, "Keycloak login form not found"
        # Keycloak marks its cookies Secure (SameSite=None), which httpx never
        # sends over plain http, so carry them by hand like a browser on localhost.
        keycloak_cookies = "; ".join(
            header.split(";", 1)[0] for header in form.headers.get_list("set-cookie") if "=" in header
        )
        submitted = await browser.post(
            html.unescape(match.group(1)),
            data={"username": seed.SSO_MEMBER.username, "password": seed.USER_PASSWORD, "credentialId": ""},
            headers={"Cookie": keycloak_cookies},
        )
        assert submitted.status_code == 302, submitted.text
        callback_url = submitted.headers["location"]
    assert callback_url.startswith(seed.callback_url(seed.ASGI_BASE_URL, seed.KEYCLOAK_SLUG))

    # 3. Keycloak's redirect reaches the callback in the browser that started.
    split = urlsplit(callback_url)
    response = await keycloak_app.get(f"{split.path}?{split.query}", headers={"Cookie": f"{TX_COOKIE}={tx_cookie}"})
    assert response.status_code == 302, response.headers.get("location")
    assert response.headers["location"] == "/projects/"

    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    assert row.auth_method == "sso"
    assert row.idp_sid  # Keycloak puts the session id in the ID token
    access = jwt.decode(response.cookies["access_token"], get_settings().secret_key, algorithms=["HS256"])
    assert access["sid"] == str(row.session_id)
