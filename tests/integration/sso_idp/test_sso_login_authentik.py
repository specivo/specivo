"""SSO against the disposable authentik, end to end (opt-in compatibility smoke).

The browser is played by httpx, as in ``test_sso_login_keycloak.py``. authentik's
login page is a single-page app, so the browser drives the flow executor API
behind it (``/api/v3/flows/executor/<flow>/``) the way that app does: the
identification stage, then the password stage, then the redirect the flow
hands back. The same API runs the invalidation flow on RP-initiated logout.

Back-channel logout: authentik's worker POSTs logout tokens to the provider's
``logout_uri``. While these tests run it points at a listener in this process
(:func:`backchannel_inbox`); each token it catches is handed to Specivo's real
back-channel route, as authentik would deliver it to a reachable Specivo.

Requires ``make sso-idp-up-authentik`` and ``SSO_TEST_AUTHENTIK_ISSUER``;
skipped otherwise (see ``tests/conftest.py``). Keycloak is not needed.
"""

from __future__ import annotations

import asyncio
import http.server
import platform
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.testing.sso import TX_COOKIE
from tests.fixtures.sso import seed

pytestmark = [pytest.mark.sso_idp, pytest.mark.sso_authentik, pytest.mark.integration]

_API_HEADERS = {"Authorization": f"Bearer {seed.AUTHENTIK_API_TOKEN}"}
_POST_LOGOUT_URI = f"{seed.ASGI_BASE_URL}/login/?logged_out=1"


def _authentik_base() -> str:
    return seed.AUTHENTIK_ISSUER.split("/application/", 1)[0]


async def _discovery() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as http:
        response = await http.get(f"{seed.AUTHENTIK_ISSUER}.well-known/openid-configuration")
        assert response.status_code == 200, response.text
        return dict(response.json())


# --- Back-channel listener -------------------------------------------------------


class _Inbox:
    """Logout tokens authentik POSTed to the listener."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    def add(self, token: str) -> None:
        self.tokens.append(token)

    async def wait_for_sid(self, sid: str, timeout: float = 30.0) -> str:
        """The logout token for IdP session ``sid``; authentik sends them from its worker."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for token in self.tokens:
                if jwt.decode(token, options={"verify_signature": False}).get("sid") == sid:
                    return token
            if loop.time() > deadline:
                pytest.fail(f"no logout token for the session; authentik sent {len(self.tokens)} other(s)")
            await asyncio.sleep(0.2)


def _listener_handler(inbox: _Inbox) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            for token in parse_qs(body.decode()).get("logout_token", []):
                inbox.add(token)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
            return

    return Handler


@pytest.fixture
def backchannel_inbox() -> Iterator[_Inbox]:
    """Point the confidential provider's back-channel ``logout_uri`` at a local listener.

    Docker Desktop forwards ``host.docker.internal`` to the host's loopback; on
    Linux the request arrives on the bridge, so the listener binds every
    interface there (as ``make test-e2e-sso`` does for its server).
    """
    inbox = _Inbox()
    bind = "0.0.0.0" if platform.system() == "Linux" else "127.0.0.1"  # noqa: S104 - test listener
    server = http.server.ThreadingHTTPServer((bind, 0), _listener_handler(inbox))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    api = f"{_authentik_base()}/api/v3/providers/oauth2/"
    with httpx.Client(timeout=10.0, headers=_API_HEADERS) as admin:
        [provider] = admin.get(api, params={"client_id": seed.CONFIDENTIAL_CLIENT_ID}).json()["results"]
        original = provider["logout_uri"]
        url = f"http://host.docker.internal:{port}{urlsplit(seed.backchannel_logout_url('', seed.AUTHENTIK_SLUG)).path}"
        patched = admin.patch(f"{api}{provider['pk']}/", json={"logout_uri": url})
        assert patched.status_code == 200, patched.text
        try:
            yield inbox
        finally:
            admin.patch(f"{api}{provider['pk']}/", json={"logout_uri": original})
            server.shutdown()
            server.server_close()


# --- The app and the provider -------------------------------------------------------


@pytest_asyncio.fixture
async def authentik_app(
    monkeypatch: pytest.MonkeyPatch, db_engine, _test_connection, backchannel_inbox: _Inbox
) -> AsyncIterator[httpx.AsyncClient]:
    """The app as authentik's redirect URIs expect it: APP_URL=http://testserver, http issuer allowed."""
    from specivo.core.database import get_db, set_engine
    from specivo.testing.conftest_base import _create_test_app, _make_test_get_db

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


async def _provider(db: AsyncSession, **overrides: Any) -> SsoProvider:
    """The confidential authentik client as an enabled Specivo provider with JIT on."""
    values: dict[str, Any] = {
        "slug": seed.AUTHENTIK_SLUG,
        "display_name": "authentik",
        "enabled": True,
        "issuer": seed.AUTHENTIK_ISSUER,
        "client_id": seed.CONFIDENTIAL_CLIENT_ID,
        "token_endpoint_auth_method": "client_secret_basic",
        "client_secret_encrypted": secret_box.encrypt(seed.CONFIDENTIAL_CLIENT_SECRET, "sso-client-secret"),
        "scopes": "openid profile email",
        "claim_config": {},
        "extra_auth_params": {},
        "allow_jit_provisioning": True,
        "required_groups": [seed.GROUP_USERS],
        "admin_groups": [seed.GROUP_ADMINS],
        "sync_admin_flag": True,
        "backchannel_logout_enabled": True,
    }
    values.update(overrides)
    provider = SsoProvider(**values)
    db.add(provider)
    await db.flush()
    return provider


# --- The browser at authentik --------------------------------------------------------


async def _run_flow(browser: httpx.AsyncClient, flow_url: str, answers: list[dict[str, Any]]) -> str:
    """Drive an authentik flow through its executor API; return where the flow redirects."""
    split = urlsplit(flow_url)
    assert split.path.startswith("/if/flow/"), flow_url
    flow_slug = split.path.rstrip("/").rsplit("/", 1)[-1]
    executor = f"{_authentik_base()}/api/v3/flows/executor/{flow_slug}/?query={quote(split.query)}"

    challenge = (await browser.get(executor)).json()
    pending = list(answers)
    while challenge.get("component") != "xak-flow-redirect":
        assert pending, f"unexpected authentik stage {challenge.get('component')}: {challenge}"
        answer = pending.pop(0)
        assert challenge.get("component") == answer["component"], challenge
        response = await browser.post(executor, json=answer, follow_redirects=True)
        assert response.status_code == 200, response.text
        challenge = response.json()
    return str(challenge["to"])


def _absolute(url: str) -> str:
    return f"{_authentik_base()}{url}" if url.startswith("/") else url


async def _sign_in_at_authentik(browser: httpx.AsyncClient, authorize_url: str, username: str) -> str:
    """Sign in at authentik from Specivo's authorize URL; return authentik's redirect to the callback."""
    first = await browser.get(authorize_url)
    assert first.status_code == 302, first.text
    target = await _run_flow(
        browser,
        _absolute(first.headers["location"]),
        [
            {"component": "ak-stage-identification", "uid_field": username},
            {"component": "ak-stage-password", "password": seed.USER_PASSWORD},
        ],
    )
    # The authentication flow returns to the authorize endpoint, which now
    # issues the code (implicit consent: no consent stage).
    authorized = await browser.get(_absolute(target))
    assert authorized.status_code == 302, authorized.text
    return str(authorized.headers["location"])


async def _sso_login(app: httpx.AsyncClient, browser: httpx.AsyncClient, username: str) -> httpx.Response:
    """Specivo start route -> authentik -> Specivo callback; return the callback response."""
    start = await app.get(f"/auth/oidc/{seed.AUTHENTIK_SLUG}/login/", params={"next": "/projects/"})
    assert start.status_code == 302, start.text
    authorize_url = start.headers["location"]
    discovery = await _discovery()
    # authentik's authorize endpoint is shared by all applications, not under the issuer.
    assert authorize_url.startswith(discovery["authorization_endpoint"])
    tx_cookie = start.cookies[TX_COOKIE]

    callback_url = await _sign_in_at_authentik(browser, authorize_url, username)
    assert callback_url.startswith(seed.callback_url(seed.ASGI_BASE_URL, seed.AUTHENTIK_SLUG))
    split = urlsplit(callback_url)
    return await app.get(f"{split.path}?{split.query}", headers={"Cookie": f"{TX_COOKIE}={tx_cookie}"})


async def _identity_user(db: AsyncSession, provider: SsoProvider) -> tuple[UserIdentity, User]:
    identity = (await db.execute(select(UserIdentity).where(UserIdentity.provider_id == provider.id))).scalar_one()
    user = await db.get(User, identity.user_id)
    assert user is not None
    return identity, user


async def _deliver(app: httpx.AsyncClient, logout_token: str) -> httpx.Response:
    return await app.post(f"/auth/oidc/{seed.AUTHENTIK_SLUG}/backchannel-logout/", data={"logout_token": logout_token})


# --- Tests -----------------------------------------------------------------------------


async def test_member_is_provisioned_with_mapped_groups(
    authentik_app: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    provider = await _provider(db_session, sync_groups=True)
    group = UserGroup(name="authentik-smoke-users")
    db_session.add(group)
    await db_session.flush()
    db_session.add(SsoGroupMapping(provider_id=provider.id, idp_group=seed.GROUP_USERS, user_group_id=group.id))
    await db_session.flush()

    async with httpx.AsyncClient(timeout=15.0) as browser:
        response = await _sso_login(authentik_app, browser, seed.SSO_MEMBER.username)

    assert response.status_code == 302, response.headers.get("location")
    assert response.headers["location"] == "/projects/"
    identity, user = await _identity_user(db_session, provider)
    # sub_mode hashed_user_id: a 64-character hex digest, stable across renames.
    assert len(identity.subject) == 64
    assert int(identity.subject, 16) >= 0
    assert identity.issuer == seed.AUTHENTIK_ISSUER
    assert identity.linked_via == "jit"
    assert user.login == seed.SSO_MEMBER.username
    assert user.email == seed.SSO_MEMBER.email
    assert user.password_hash is None
    assert user.is_admin is False
    # authentik's default email mapping always says email_verified: false.
    assert user.email_verified_at is None
    managed_by = await db_session.scalar(
        select(UserGroupMember.managed_by_provider_id).where(
            UserGroupMember.user_id == user.id, UserGroupMember.group_id == group.id
        )
    )
    assert managed_by == provider.id

    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    assert row.auth_method == "sso"
    assert row.user_identity_id == identity.id
    assert row.idp_sid  # authentik puts its session id in the ID token
    assert row.id_token_encrypted
    access = jwt.decode(response.cookies["access_token"], get_settings().secret_key, algorithms=["HS256"])
    assert access["sid"] == str(row.session_id)


async def test_admin_group_sets_the_admin_flag(authentik_app: httpx.AsyncClient, db_session: AsyncSession) -> None:
    provider = await _provider(db_session)

    async with httpx.AsyncClient(timeout=15.0) as browser:
        response = await _sso_login(authentik_app, browser, seed.SSO_ADMIN.username)

    assert response.status_code == 302, response.headers.get("location")
    _, user = await _identity_user(db_session, provider)
    assert user.is_admin is True


async def test_outsider_is_refused_by_required_groups(
    authentik_app: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    provider = await _provider(db_session)

    async with httpx.AsyncClient(timeout=15.0) as browser:
        response = await _sso_login(authentik_app, browser, seed.SSO_OUTSIDER.username)

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.path == "/login/"
    assert parse_qs(location.query)["sso_error"] == ["sso_not_in_required_group"]
    assert (await db_session.scalar(select(UserIdentity.id).where(UserIdentity.provider_id == provider.id))) is None


async def test_rp_initiated_logout_ends_the_authentik_session(
    authentik_app: httpx.AsyncClient, db_session: AsyncSession, backchannel_inbox: _Inbox
) -> None:
    provider = await _provider(db_session)
    discovery = await _discovery()

    async with httpx.AsyncClient(timeout=15.0) as browser:
        signed_in = await _sso_login(authentik_app, browser, seed.SSO_MEMBER.username)
        assert signed_in.status_code == 302, signed_in.headers.get("location")
        identity, user = await _identity_user(db_session, provider)

        logout = await authentik_app.post(
            "/api/v1/auth/logout/", headers={"Authorization": f"Bearer {signed_in.cookies['access_token']}"}
        )
        assert logout.status_code == 200, logout.text
        end_session_url = logout.json()["end_session_url"]

        # The URL shape: authentik's per-application end-session endpoint with
        # the stored ID token, the client id and the fixed post-logout address.
        assert end_session_url.startswith(discovery["end_session_endpoint"] + "?")
        params = parse_qs(urlsplit(end_session_url).query)
        assert params["client_id"] == [seed.CONFIDENTIAL_CLIENT_ID]
        assert params["post_logout_redirect_uri"] == [_POST_LOGOUT_URI]
        hint = jwt.decode(params["id_token_hint"][0], options={"verify_signature": False})
        assert hint["sub"] == identity.subject
        assert hint["iss"] == seed.AUTHENTIK_ISSUER
        assert (await db_session.scalar(select(RefreshToken.id).where(RefreshToken.user_id == user.id))) is None

        # The browser follows it: authentik runs its invalidation flow (no
        # stage to answer in the default flow) and returns to Specivo.
        ended = await browser.get(end_session_url)
        assert ended.status_code == 302, ended.text
        back = await _run_flow(browser, _absolute(ended.headers["location"]), [])
        assert back == _POST_LOGOUT_URI

    # Ending the authentik session also makes authentik send a back-channel
    # logout token. Specivo already ended the session, so it matches nothing.
    token = await backchannel_inbox.wait_for_sid(hint["sid"])
    assert jwt.decode(token, options={"verify_signature": False})["sub"] == identity.subject
    assert (await _deliver(authentik_app, token)).status_code == 200


async def test_backchannel_logout_ends_the_specivo_session(
    authentik_app: httpx.AsyncClient, db_session: AsyncSession, backchannel_inbox: _Inbox
) -> None:
    provider = await _provider(db_session)

    async with httpx.AsyncClient(timeout=15.0) as browser:
        signed_in = await _sso_login(authentik_app, browser, seed.SSO_MEMBER.username)
    assert signed_in.status_code == 302, signed_in.headers.get("location")
    identity, user = await _identity_user(db_session, provider)
    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()

    # An authentik administrator ends the user's sessions (earlier tests leave
    # some behind); authentik sends a logout token per session and provider.
    api = f"{_authentik_base()}/api/v3/core/authenticated_sessions/"
    async with httpx.AsyncClient(timeout=10.0, headers=_API_HEADERS) as admin:
        sessions = (await admin.get(api, params={"user__username": seed.SSO_MEMBER.username})).json()["results"]
        assert sessions
        for session in sessions:
            deleted = await admin.delete(f"{api}{session['uuid']}/")
            assert deleted.status_code == 204, deleted.text

    assert row.idp_sid
    token = await backchannel_inbox.wait_for_sid(row.idp_sid)
    assert jwt.get_unverified_header(token)["typ"] == "logout+jwt"
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == identity.subject

    delivered = await _deliver(authentik_app, token)
    assert delivered.status_code == 200, delivered.text
    assert (await db_session.scalar(select(RefreshToken.id).where(RefreshToken.user_id == user.id))) is None
    # A replay is refused.
    assert (await _deliver(authentik_app, token)).status_code == 400
