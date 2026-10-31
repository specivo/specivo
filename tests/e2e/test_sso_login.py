"""SSO sign-in, sign-out and offboarding in a real browser, against the disposable IdPs.

Every test runs once per IdP: Keycloak (``make sso-idp-up``) and authentik
(``make sso-authentik-up``); an IdP that is down skips its half. The
integration tests (tests/integration/sso_idp/) prove the protocol with httpx
playing the browser. This module proves what only a browser shows: the
cookies across the cross-site redirects (the transaction cookie must survive
the IdP's redirect back), the IdPs' own pages, RP-initiated logout through
the end-session endpoint, a back-channel logout ending the browser session,
and a user disabled at the IdP losing the session at its next renewal (the
E2E server checks SSO sessions at the IdP on every renewal).

The browser uses http://localhost:9944, not the suite's 127.0.0.1, because
that is what the IdP fixtures register as redirect URI, and the session
cookies must belong to the host the redirects come back to.

No test here touches the session-wide ``e2e_user`` login: each one signs in
through the IdP in a browser context of its own, so signing out revokes only
that session.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from specivo.testing.e2e_base import E2E_SSO_BASE_URL
from tests.conftest import _issuer_is_up
from tests.e2e.pages.authentik_login import AuthentikLoginPage
from tests.e2e.pages.keycloak_login import KeycloakLoginPage
from tests.fixtures.sso import seed

pytestmark = [pytest.mark.e2e, pytest.mark.sso_idp]

_PROVIDERS_PATH = "/api/v1/admin/sso/providers/"
_LOGIN_URL = re.compile(r"/login/")


# ---------------------------------------------------------------------------
# IdP admin APIs (sync: the browser tests run on Playwright's sync loop)
# ---------------------------------------------------------------------------


def _keycloak_admin() -> httpx.Client:
    """An httpx client signed in to Keycloak's admin REST API (master realm admin)."""
    token = httpx.post(
        f"{seed.KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": seed.KEYCLOAK_ADMIN_USER,
            "password": seed.KEYCLOAK_ADMIN_PASSWORD,
        },
        timeout=10,
    )
    assert token.status_code == 200, token.text
    return httpx.Client(
        base_url=f"{seed.KEYCLOAK_BASE_URL}/admin/realms/{seed.REALM}",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        timeout=10,
    )


def _keycloak_user_id(admin: httpx.Client, username: str) -> str:
    users = admin.get("/users", params={"username": username, "exact": "true"})
    assert users.status_code == 200, users.text
    [user] = users.json()
    return str(user["id"])


def _keycloak_sign_out(username: str) -> None:
    """End every Keycloak session of the user; Keycloak sends the back-channel logout tokens."""
    with _keycloak_admin() as admin:
        user_id = _keycloak_user_id(admin, username)
        sessions = admin.get(f"/users/{user_id}/sessions")
        assert sessions.status_code == 200, sessions.text
        assert sessions.json(), f"{username} has no Keycloak session to log out"
        logout = admin.post(f"/users/{user_id}/logout")
        assert logout.status_code == 204, logout.text


def _keycloak_set_enabled(username: str, enabled: bool) -> None:
    with _keycloak_admin() as admin:
        resp = admin.put(f"/users/{_keycloak_user_id(admin, username)}", json={"enabled": enabled})
        assert resp.status_code == 204, resp.text


def _authentik_admin() -> httpx.Client:
    return httpx.Client(
        base_url=f"{seed.AUTHENTIK_BASE_URL}/api/v3",
        headers={"Authorization": f"Bearer {seed.AUTHENTIK_API_TOKEN}"},
        timeout=10,
    )


def _authentik_sign_out(username: str) -> None:
    """End every authentik session of the user, as an administrator; authentik sends the logout tokens."""
    with _authentik_admin() as admin:
        sessions = admin.get("/core/authenticated_sessions/", params={"user__username": username})
        assert sessions.status_code == 200, sessions.text
        assert sessions.json()["results"], f"{username} has no authentik session to end"
        for session in sessions.json()["results"]:
            resp = admin.delete(f"/core/authenticated_sessions/{session['uuid']}/")
            assert resp.status_code == 204, resp.text


def _authentik_set_enabled(username: str, enabled: bool) -> None:
    with _authentik_admin() as admin:
        [user] = admin.get("/core/users/", params={"username": username}).json()["results"]
        resp = admin.patch(f"/core/users/{user['pk']}/", json={"is_active": enabled})
        assert resp.status_code == 200, resp.text


@dataclass(frozen=True)
class E2EIdp:
    name: str
    slug: str
    issuer: str
    base_url: str
    display_name: str
    scopes: str
    #: The display name JIT gives SSO_MEMBER from the IdP's name claim.
    member_display_name: str
    login_page: Callable[[Page], KeycloakLoginPage | AuthentikLoginPage]
    sign_out: Callable[[str], None]
    set_enabled: Callable[[str, bool], None]

    @property
    def button(self) -> str:
        return f"Sign in with {self.display_name}"


_IDPS = {
    "keycloak": E2EIdp(
        name="keycloak",
        slug=seed.KEYCLOAK_SLUG,
        issuer=seed.KEYCLOAK_ISSUER,
        base_url=seed.KEYCLOAK_BASE_URL,
        display_name="Keycloak",
        scopes="openid profile email",
        member_display_name="SSO Member User",
        login_page=KeycloakLoginPage,
        sign_out=_keycloak_sign_out,
        set_enabled=_keycloak_set_enabled,
    ),
    "authentik": E2EIdp(
        name="authentik",
        slug=seed.AUTHENTIK_SLUG,
        issuer=seed.AUTHENTIK_ISSUER,
        base_url=seed.AUTHENTIK_BASE_URL,
        display_name="authentik",
        # authentik issues refresh tokens (for the session check) only with offline_access.
        scopes="openid profile email offline_access",
        member_display_name="SSO Member",
        login_page=AuthentikLoginPage,
        sign_out=_authentik_sign_out,
        set_enabled=_authentik_set_enabled,
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _delete_provider(api: httpx.Client, slug: str) -> None:
    """Remove the provider row (and unlink its identities) if a run left one."""
    listed = api.get(_PROVIDERS_PATH)
    assert listed.status_code == 200, listed.text
    for provider in listed.json()["items"]:
        if provider["slug"] == slug:
            deleted = api.delete(f"{_PROVIDERS_PATH}{provider['id']}/", params={"force": "true"})
            assert deleted.status_code == 204, deleted.text


_RELEASE_EMAILS_SCRIPT = """
import asyncio, os, sys, uuid
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            "UPDATE users SET email = 'sso-e2e-' || id || '-' || $2 || '@example.com' "
            "WHERE LOWER(email) = ANY($1::text[])",
            sys.argv[1:],
            uuid.uuid4().hex[:8],
        )
    finally:
        await conn.close()

asyncio.run(main())
"""


def _release_seed_emails() -> None:
    """Rename the accounts JIT provisioning made, so no seeded IdP email stays taken.

    The E2E database outlives a run and is shared with the integration tests,
    which create their own ``sso-member@example.com`` user (and the IdPs may
    have been recreated with new subject ids). Specivo has no API to delete a
    user, so the leftover account keeps its row and gets a unique email; the
    next sign-in provisions a fresh one. A subprocess, like the other E2E DB
    helpers, keeps asyncio away from Playwright's sync loop.
    """
    emails = [user.email.lower() for user in seed.SEED_USERS]
    result = subprocess.run(
        ["uv", "run", "python", "-c", _RELEASE_EMAILS_SCRIPT, *emails],
        env={**os.environ},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module", params=sorted(_IDPS))
def e2e_idp(request: pytest.FixtureRequest) -> E2EIdp:
    idp = _IDPS[request.param]
    if not _issuer_is_up(idp.issuer):
        target = "make sso-authentik-up" if idp.name == "authentik" else "make sso-idp-up"
        pytest.skip(f"{idp.display_name} test IdP is not serving {idp.issuer} (start it with: {target})")
    return idp


@pytest.fixture(scope="module")
def idp_provider(e2e_server: str, _admin_auth: tuple[str, list[dict]], e2e_idp: E2EIdp) -> Iterator[dict]:
    """The IdP's provider, created through the admin API, removed afterwards.

    JIT provisioning creates ``sso-member``'s account on its first sign-in;
    ``required_groups`` keeps ``sso-outsider`` out. Before and after the
    module the seeded IdP emails are freed (``_release_seed_emails``).

    Deleting the provider at the end matters beyond this module: an enabled
    provider adds a "Sign in with" button to the login page every other E2E
    test (and the visual baselines) sees.
    """
    token, _ = _admin_auth
    with httpx.Client(base_url=e2e_server, headers={"Authorization": f"Bearer {token}"}, timeout=10) as api:
        for other in _IDPS.values():
            _delete_provider(api, other.slug)
        _release_seed_emails()
        created = api.post(
            _PROVIDERS_PATH,
            json={
                "slug": e2e_idp.slug,
                "display_name": e2e_idp.display_name,
                "enabled": True,
                "issuer": e2e_idp.issuer,
                "client_id": seed.CONFIDENTIAL_CLIENT_ID,
                "client_secret": seed.CONFIDENTIAL_CLIENT_SECRET,
                "scopes": e2e_idp.scopes,
                "allow_jit_provisioning": True,
                "required_groups": [seed.GROUP_USERS],
                "rp_logout_enabled": True,
                "backchannel_logout_enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        yield created.json()
        _delete_provider(api, e2e_idp.slug)
        _release_seed_emails()


@pytest.fixture
def sso_context(browser: Browser, idp_provider: dict) -> Generator[BrowserContext, None, None]:
    """A fresh browser (no Specivo or IdP cookies) on the host the IdPs redirect to."""
    context = browser.new_context(base_url=E2E_SSO_BASE_URL)
    yield context
    context.close()


@pytest.fixture
def sso_page(sso_context: BrowserContext) -> Generator[Page, None, None]:
    page = sso_context.new_page()
    yield page
    page.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_sign_in(page: Page, idp: E2EIdp) -> KeycloakLoginPage | AuthentikLoginPage:
    """From Specivo's login page to the IdP's sign-in form."""
    page.goto("/login/")
    page.get_by_role("link", name=idp.button).click()
    login = idp.login_page(page)
    expect(login.username_input).to_be_visible()
    assert page.url.startswith(idp.base_url), page.url
    return login


def _sign_in(page: Page, idp: E2EIdp, user: seed.SeedUser = seed.SSO_MEMBER) -> None:
    """Sign in through the IdP and wait for the dashboard."""
    _start_sign_in(page, idp).sign_in(user.username, seed.USER_PASSWORD)
    page.wait_for_url(f"{E2E_SSO_BASE_URL}/")


def _refresh_cookie(context: BrowserContext) -> str:
    [value] = [c["value"] for c in context.cookies(E2E_SSO_BASE_URL) if c["name"] == "refresh_token"]
    return value


def _refresh_status(page: Page, refresh_token: str) -> int:
    """What the refresh endpoint answers for this refresh token."""
    response = page.request.post("/api/v1/auth/refresh/", data={"refresh_token": refresh_token})
    return response.status


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sso_login_creates_session(sso_page: Page, e2e_idp: E2EIdp) -> None:
    _sign_in(sso_page, e2e_idp)

    # The header shows the account the ID token signed in.
    expect(sso_page.locator(".header-user")).to_be_visible()
    expect(sso_page.locator(".user-dropdown-name")).to_have_text(e2e_idp.member_display_name)
    expect(sso_page.locator(".user-dropdown-email")).to_have_text(seed.SSO_MEMBER.email)

    # The profile lists the IdP account as connected.
    sso_page.goto("/my/profile/")
    expect(sso_page.locator(".sp-connected-row .sp-connected-name")).to_contain_text(e2e_idp.display_name)


def test_sso_logout_ends_idp_session(sso_page: Page, sso_context: BrowserContext, e2e_idp: E2EIdp) -> None:
    _sign_in(sso_page, e2e_idp)
    refresh_token = _refresh_cookie(sso_context)

    # Sign out through the header menu: Specivo revokes its session, then sends
    # the browser through the IdP's end-session endpoint and back.
    sso_page.locator(".header-user").click()
    sso_page.locator(".user-dropdown-logout").click()
    # With id_token_hint Keycloak logs out without asking; tolerate its
    # confirmation page anyway, in case a Keycloak release brings it back.
    sso_page.wait_for_url(
        re.compile(r"/login/\?logged_out=1|/protocol/openid-connect/logout|/end-session/|/if/flow/"),
    )
    if "/protocol/openid-connect/logout" in sso_page.url:
        sso_page.locator("#kc-logout").click()
    sso_page.wait_for_url(f"{E2E_SSO_BASE_URL}/login/?logged_out=1")
    expect(sso_page.get_by_role("status")).to_contain_text("You have been signed out.")

    # The browser's old refresh token was revoked on the server, not just dropped.
    assert _refresh_status(sso_page, refresh_token) == 401

    # The IdP session is gone too: signing in again asks for the password
    # instead of coming straight back signed in.
    _start_sign_in(sso_page, e2e_idp)


def test_backchannel_logout_kills_browser_session(sso_page: Page, sso_context: BrowserContext, e2e_idp: E2EIdp) -> None:
    _sign_in(sso_page, e2e_idp)
    refresh_token = _refresh_cookie(sso_context)

    # An administrator ends the user's sessions at the IdP. The IdP posts a
    # logout token to Specivo's back-channel endpoint (host.docker.internal:9944).
    e2e_idp.sign_out(seed.SSO_MEMBER.username)

    # The next Specivo navigation finds the session gone. authentik's worker
    # sends the token asynchronously, so give it a moment.
    deadline_attempts = 20
    for _ in range(deadline_attempts):
        sso_page.goto("/my/profile/")
        if _LOGIN_URL.search(sso_page.url):
            break
        sso_page.wait_for_timeout(500)
    assert _LOGIN_URL.search(sso_page.url), (
        f"Still signed in at {sso_page.url}: the back-channel logout did not reach Specivo. The IdP runs "
        "in a container and calls host.docker.internal:9944; on Linux the E2E server must listen on "
        "0.0.0.0 (E2E_SERVER_BIND, set by make test-e2e-sso)."
    )
    # The whole session was revoked, not only its access token.
    assert _refresh_status(sso_page, refresh_token) == 401


def test_user_disabled_at_idp_loses_the_session_at_renewal(
    sso_page: Page, sso_context: BrowserContext, e2e_idp: E2EIdp
) -> None:
    """No back-channel needed: the renewal asks the IdP, which refuses the disabled user."""
    _sign_in(sso_page, e2e_idp)
    refresh_token = _refresh_cookie(sso_context)
    e2e_idp.set_enabled(seed.SSO_MEMBER.username, False)
    try:
        # The access token expires (here: is dropped); the next page renews the
        # session from the refresh cookie, and the IdP refuses it.
        access = [c for c in sso_context.cookies(E2E_SSO_BASE_URL) if c["name"] == "access_token"]
        sso_context.clear_cookies(name="access_token")
        assert access
        sso_page.goto("/my/profile/")
        assert _LOGIN_URL.search(sso_page.url), f"Still signed in at {sso_page.url}"
        assert _refresh_status(sso_page, refresh_token) == 401
    finally:
        e2e_idp.set_enabled(seed.SSO_MEMBER.username, True)


def test_sso_outsider_denied(sso_page: Page, e2e_idp: E2EIdp) -> None:
    _start_sign_in(sso_page, e2e_idp).sign_in(seed.SSO_OUTSIDER.username, seed.USER_PASSWORD)

    sso_page.wait_for_url(f"{E2E_SSO_BASE_URL}/login/?sso_error=sso_not_in_required_group")
    expect(sso_page.get_by_text("is not in a group that may sign in here")).to_be_visible()
