"""Fixtures for the tests against the disposable IdPs (``make sso-idp-up``, ``make sso-authentik-up``).

* ``idp``: runs a test once per IdP (Keycloak, authentik); an IdP that does
  not serve its discovery document skips its run, so Keycloak alone
  (``make sso-idp-up``) still runs the Keycloak half.
* ``sso_app``: the app as the IdPs' redirect URIs expect it
  (``APP_URL=http://testserver``, http issuers allowed), on the test database.
* ``idp_provider``: an enabled provider row for ``idp`` with JIT
  provisioning, back-channel logout and the session check on.
* ``backchannel_catcher``: a tiny HTTP server on the E2E port (9944) that
  catches the logout tokens the IdPs send to ``host.docker.internal:9944``,
  so a test can hand them to the in-process app.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.models.sso import SsoProvider
from specivo.testing.sso import TX_COOKIE
from tests.conftest import _issuer_is_up
from tests.fixtures.sso import seed
from tests.integration.sso_idp.idps import IDPS, Idp

#: Where the IdP fixtures send back-channel logouts (host.docker.internal:9944).
BACKCHANNEL_PORT = 9944


@pytest.fixture(params=sorted(IDPS))
def idp(request: pytest.FixtureRequest) -> Idp:
    driver = IDPS[request.param]()
    if not _issuer_is_up(driver.issuer):
        target = "make sso-authentik-up" if driver.name == "authentik" else "make sso-idp-up"
        pytest.skip(f"{driver.display_name} test IdP is not serving {driver.issuer} (start it with: {target})")
    return driver


@pytest_asyncio.fixture
async def sso_app(monkeypatch: pytest.MonkeyPatch, db_engine, _test_connection) -> AsyncIterator[httpx.AsyncClient]:
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


def build_idp_provider(idp: Idp, **overrides) -> SsoProvider:
    values = {
        "slug": idp.slug,
        "display_name": idp.display_name,
        "enabled": True,
        "issuer": idp.issuer,
        "client_id": seed.CONFIDENTIAL_CLIENT_ID,
        "token_endpoint_auth_method": "client_secret_basic",
        "client_secret_encrypted": secret_box.encrypt(seed.CONFIDENTIAL_CLIENT_SECRET, "sso-client-secret"),
        "scopes": idp.scopes,
        "claim_config": {},
        "extra_auth_params": {},
        "allow_jit_provisioning": True,
        "backchannel_logout_enabled": True,
    }
    values.update(overrides)
    return SsoProvider(**values)


@pytest_asyncio.fixture
async def idp_provider(db_session: AsyncSession, idp: Idp) -> SsoProvider:
    provider = build_idp_provider(idp)
    db_session.add(provider)
    await db_session.flush()
    return provider


async def sign_in(app: httpx.AsyncClient, idp: Idp, username: str, *, next_path: str = "/") -> httpx.Response:
    """Start at Specivo, sign in at the IdP, hand its redirect to the callback; the callback response."""
    start = await app.get(f"/auth/oidc/{idp.slug}/login/", params={"next": next_path})
    assert start.status_code == 302, start.text
    authorize_url = start.headers["location"]
    assert authorize_url.startswith(idp.base_url + "/"), authorize_url
    callback_url = await idp.sign_in(authorize_url, username)
    assert callback_url.startswith(seed.callback_url(seed.ASGI_BASE_URL, idp.slug)), callback_url
    split = urlsplit(callback_url)
    return await app.get(f"{split.path}?{split.query}", headers={"Cookie": f"{TX_COOKIE}={start.cookies[TX_COOKIE]}"})


# ---------------------------------------------------------------------------
# Back-channel logout catcher
# ---------------------------------------------------------------------------


@dataclass
class Caught:
    path: str
    logout_token: str


@dataclass
class BackchannelCatcher:
    caught: list[Caught] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, slug: str, *, count: int = 1, timeout: float = 30.0) -> list[Caught]:
        """The logout tokens sent for provider ``slug``, once at least ``count`` arrived."""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                found = [c for c in self.caught if c.path == seed.backchannel_logout_url("", slug)]
            if len(found) >= count or time.monotonic() > deadline:
                return found
            time.sleep(0.2)

    def clear(self) -> None:
        with self.lock:
            self.caught.clear()


@pytest.fixture
def backchannel_catcher() -> Iterator[BackchannelCatcher]:
    """Listen where the IdPs send back-channel logouts; answer 200 and keep the token.

    Docker Desktop forwards host.docker.internal to the host's loopback; on
    Linux the request arrives on the bridge, so listen on every interface
    there (``E2E_SERVER_BIND``, as ``make test-e2e-sso`` sets it).
    """
    catcher = BackchannelCatcher()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            token = (form.get("logout_token") or [""])[0]
            with catcher.lock:
                catcher.caught.append(Caught(path=self.path, logout_token=token))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    bind = os.environ.get("E2E_SERVER_BIND", "127.0.0.1")
    try:
        server = ThreadingHTTPServer((bind, BACKCHANNEL_PORT), Handler)
    except OSError as exc:
        pytest.skip(f"port {BACKCHANNEL_PORT} is busy (an E2E server?): {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield catcher
    finally:
        server.shutdown()
        server.server_close()
