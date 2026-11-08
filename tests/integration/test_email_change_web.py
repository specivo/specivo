"""The profile's "Email address" section, the confirm page, and the reauth SSO flow.

* the profile shows the pending change with Resend / Cancel, a SCIM-managed
  account's address read-only, and for an account without a password the
  "Sign in again" buttons instead of the form;
* ``GET /confirm-email/`` changes nothing, ``POST`` does, signed in or not;
* an SSO-only account is refused without a fresh sign-in at the IdP and
  allowed after ``POST /auth/oidc/{slug}/reauth/``, which must come back as
  the same identity.

Logins and addresses carry an ``emw`` prefix (parallel modules, unique email).
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.scim import ScimResource
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import TX_COOKIE, SsoLoginDriver, link_identity

pytestmark = pytest.mark.integration

DELAY = "specivo.tasks.notifications.send_notification_email.delay"
_TOKEN_RE = re.compile(r"/confirm-email/\?token=([A-Za-z0-9_\-]+)")


def _set_cookie_value(resp: Response, name: str) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


class Browser:
    """A browser carrying its cookies by hand (they are ``Secure``; the test transport is http)."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.cookies: dict[str, str] = {}

    async def load_csrf(self) -> None:
        page = await self.get("/login/")
        token = _set_cookie_value(page, "csrf_token")
        assert token
        self.cookies["csrf_token"] = token

    async def sign_in(self, login: str) -> None:
        resp = await self.client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text
        self.cookies["access_token"] = resp.json()["access_token"]
        self.cookies["refresh_token"] = resp.json()["refresh_token"]
        await self.load_csrf()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        cookies = {**self.cookies, **(extra or {})}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.client.get(url, headers=self._headers(kwargs.pop("cookies", None)), **kwargs)

    async def post(self, url: str, data: dict[str, str] | None = None) -> Response:
        form = dict(data or {})
        if "csrf_token" in self.cookies:
            form["csrf_token"] = self.cookies["csrf_token"]
        return await self.client.post(url, data=form, headers=self._headers())


async def _user(db: AsyncSession, **fields: Any) -> User:
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    await db.commit()
    return user


def _token_from(delay: Any) -> str:
    [call] = delay.call_args_list
    match = _TOKEN_RE.search(call.args[3])
    assert match
    return match.group(1)


# ---------------------------------------------------------------------------
# Profile section
# ---------------------------------------------------------------------------


async def test_profile_request_shows_pending_then_cancel(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, login="emwpending", email="emw-pending@example.com")
    browser = Browser(client)
    await browser.sign_in(user.login)

    page = await browser.get("/my/profile/")
    assert "Email address" in page.text and 'name="current_password"' in page.text

    with patch(DELAY) as delay:
        resp = await browser.post(
            "/my/profile/email/", {"new_email": "emw-pending-new@example.com", "current_password": TEST_PASSWORD}
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/my/profile/?email=requested"
    assert delay.call_count == 1

    page = await browser.get("/my/profile/?email=requested")
    assert "Change to emw-pending-new@example.com pending." in page.text
    assert "Check the inbox of the new address" in page.text
    assert "/my/profile/email/resend/" in page.text and "/my/profile/email/cancel/" in page.text

    with patch(DELAY) as delay:
        resp = await browser.post("/my/profile/email/resend/")
    assert resp.headers["location"] == "/my/profile/?email=resent"
    assert delay.call_count == 1

    resp = await browser.post("/my/profile/email/cancel/")
    assert resp.headers["location"] == "/my/profile/?email=cancelled"
    page = await browser.get("/my/profile/?email=cancelled")
    assert "pending." not in page.text
    assert "The email change was cancelled." in page.text


async def test_profile_wrong_password_shows_an_error(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, login="emwwrong", email="emw-wrong@example.com")
    browser = Browser(client)
    await browser.sign_in(user.login)
    with patch(DELAY) as delay:
        resp = await browser.post(
            "/my/profile/email/", {"new_email": "emw-wrong-new@example.com", "current_password": "nope-nope"}
        )
    assert resp.headers["location"] == "/my/profile/?email_error=email_change_password_invalid"
    delay.assert_not_called()
    page = await browser.get(resp.headers["location"])
    assert "Current password is incorrect" in page.text


async def test_profile_scim_managed_is_read_only(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    user = await _user(db_session, login="emwscim", email="emw-scim@example.com")
    db_session.add(
        ScimResource(provider_id=sso_provider.id, resource_type="User", scim_id=str(uuid.uuid4()), user_id=user.id)
    )
    await db_session.commit()
    browser = Browser(client)
    await browser.sign_in(user.login)
    page = await browser.get("/my/profile/")
    assert "Change it at your identity provider." in page.text
    assert 'action="/my/profile/email/"' not in page.text
    with patch(DELAY) as delay:
        resp = await browser.post(
            "/my/profile/email/", {"new_email": "emw-scim-new@example.com", "current_password": TEST_PASSWORD}
        )
    assert resp.headers["location"] == "/my/profile/?email_error=email_change_managed_by_idp"
    delay.assert_not_called()


# ---------------------------------------------------------------------------
# Confirm page
# ---------------------------------------------------------------------------


async def test_confirm_page_get_changes_nothing_and_post_confirms_signed_out(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, login="emwconfirm", email="emw-confirm@example.com")
    browser = Browser(client)
    await browser.sign_in(user.login)
    with patch(DELAY) as delay:
        await browser.post(
            "/my/profile/email/", {"new_email": "emw-confirm-new@example.com", "current_password": TEST_PASSWORD}
        )
    token = _token_from(delay)

    stranger = Browser(client)  # another browser, not signed in
    page = await stranger.get(f"/confirm-email/?token={token}")
    assert page.status_code == 200
    assert "emw-confirm-new@example.com" in page.text
    assert 'name="token"' in page.text
    await db_session.refresh(user)
    assert user.email == "emw-confirm@example.com"

    with patch(DELAY):
        done = await stranger.post("/confirm-email/", {"token": token})
    assert done.status_code == 200, done.text
    assert "Your email address was changed" in done.text
    await db_session.refresh(user)
    assert user.email == "emw-confirm-new@example.com"

    again = await stranger.get(f"/confirm-email/?token={token}")
    assert "This confirmation link has already been used." in again.text
    assert 'name="token"' not in again.text


async def test_confirm_page_with_a_bad_token(client: AsyncClient) -> None:
    page = await client.get("/confirm-email/?token=not-a-real-token")
    assert "This confirmation link is not valid." in page.text
    resp = await client.post("/confirm-email/", data={"token": "not-a-real-token"})
    assert resp.status_code == 400
    assert "This confirmation link is not valid." in resp.text


# ---------------------------------------------------------------------------
# SSO-only accounts: the reauth flow
# ---------------------------------------------------------------------------


async def _sso_browser(
    client: AsyncClient, db: AsyncSession, provider: SsoProvider, idp: MockIdP, sso_login: SsoLoginDriver, login: str
) -> tuple[Browser, User]:
    user = await _user(db, login=login, email=f"{login}@example.com", password_hash=None)
    await link_identity(db, user, provider, subject=idp.default_sub)
    await db.commit()
    signed_in = await sso_login(client)
    assert signed_in.status_code == 302, signed_in.text
    browser = Browser(client)
    browser.cookies["access_token"] = _set_cookie_value(signed_in, "access_token")
    await browser.load_csrf()
    return browser, user


async def _reauth(browser: Browser, idp: MockIdP, claims: dict[str, Any] | None = None) -> Response:
    started = await browser.post("/auth/oidc/test-idp/reauth/")
    assert started.status_code == 303, started.text
    authorize_url = started.headers["location"]
    assert "prompt=login" in authorize_url and "max_age=0" in authorize_url
    tx_cookie = _set_cookie_value(started, TX_COOKIE)
    split = urlsplit(idp.authorize(authorize_url, claims=claims))
    return await browser.get(f"{split.path}?{split.query}", cookies={TX_COOKIE: tx_cookie})


async def test_sso_only_account_needs_a_fresh_sign_in(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
    sso_login: SsoLoginDriver,
) -> None:
    browser, user = await _sso_browser(client, db_session, sso_provider, mock_idp, sso_login, "emwsso")

    page = await browser.get("/my/profile/")
    assert "Sign in again with Test IdP" in page.text
    assert 'action="/auth/oidc/test-idp/reauth/"' in page.text
    assert 'name="current_password"' not in page.text

    with patch(DELAY) as delay:
        refused = await browser.post("/my/profile/email/", {"new_email": "emw-sso-new@example.com"})
    assert refused.headers["location"] == "/my/profile/?email_error=email_change_reauth_required"
    delay.assert_not_called()

    back = await _reauth(browser, mock_idp)
    assert back.status_code == 302, back.text
    assert back.headers["location"] == "/my/profile/?email_reauth=1"
    page = await browser.get("/my/profile/?email_reauth=1")
    assert "You signed in again." in page.text
    assert 'action="/my/profile/email/"' in page.text

    with patch(DELAY) as delay:
        allowed = await browser.post("/my/profile/email/", {"new_email": "emw-sso-new@example.com"})
    assert allowed.headers["location"] == "/my/profile/?email=requested"
    assert delay.call_count == 1
    # The fresh sign-in unlocks one request only.
    with patch(DELAY) as delay:
        again = await browser.post("/my/profile/email/", {"new_email": "emw-sso-other@example.com"})
    assert again.headers["location"] == "/my/profile/?email_error=email_change_reauth_required"


async def test_reauth_as_another_identity_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
    sso_login: SsoLoginDriver,
) -> None:
    browser, _user_a = await _sso_browser(client, db_session, sso_provider, mock_idp, sso_login, "emwssoa")
    other = await _user(db_session, login="emwssob", email="emwssob@example.com", password_hash=None)
    await link_identity(db_session, other, sso_provider, subject="emw-other-subject")
    await db_session.commit()

    back = await _reauth(browser, mock_idp, claims={"sub": "emw-other-subject"})
    assert back.status_code == 302
    assert back.headers["location"] == "/my/profile/?sso_error=sso_reauth_mismatch"
    with patch(DELAY) as delay:
        refused = await browser.post("/my/profile/email/", {"new_email": "emw-ssoa-new@example.com"})
    assert refused.headers["location"] == "/my/profile/?email_error=email_change_reauth_required"
    delay.assert_not_called()


async def test_reauth_needs_an_identity_at_the_provider(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    user = await _user(db_session, login="emwnoid", email="emw-noid@example.com")
    browser = Browser(client)
    await browser.sign_in(user.login)
    resp = await browser.post("/auth/oidc/test-idp/reauth/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/my/profile/?sso_error=sso_reauth_mismatch"
