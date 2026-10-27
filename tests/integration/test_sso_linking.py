"""SSO account linking and provisioning through the browser routes.

* ``POST /auth/oidc/{slug}/link/`` (signed in, CSRF) starts a flow with
  ``intent=link``; the callback links the identity to the signed-in user,
  keeps their sessions and returns to the profile with a message.
* ``POST /my/identities/{id}/unlink/`` (CSRF) removes one of the user's own
  identities, only while another way to sign in remains, and ends the
  sessions that came from it.
* Admins see and unlink identities on the user detail page and through
  ``/api/v1/admin/users/{id}/identities/``, always.
* JIT provisioning end to end, including the language from ``locale``.

See "Flows -> Link an existing account" and "Account linking and
provisioning rules" in the SSO design. Logins and emails carry an ``lnk``
prefix because modules run in parallel inside uncommitted transactions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.auth_service import AuthService
from specivo.services.sso.provisioning import subject_hash
from specivo.testing.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import TX_COOKIE, SsoLoginDriver, build_sso_provider, link_identity

pytestmark = pytest.mark.integration

LINK_PATH = "/auth/oidc/test-idp/link/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def _set_cookie_value(resp: Response, name: str) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


async def _user(db: AsyncSession, factory=UserFactory, **fields: Any) -> User:
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


class Browser:
    """A signed-in browser: auth cookies plus the CSRF cookie, carried by hand.

    The cookies are ``Secure`` and the test transport is plain http, so httpx
    would not send them back on its own.
    """

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.cookies: dict[str, str] = {}

    async def sign_in(self, login: str) -> None:
        resp = await self.client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        self.cookies["access_token"] = body["access_token"]
        self.cookies["refresh_token"] = body["refresh_token"]
        page = await self.get("/login/")
        token = _set_cookie_value(page, "csrf_token")
        assert token
        self.cookies["csrf_token"] = token

    def use_tokens(self, access_token: str, refresh_token: str) -> None:
        self.cookies["access_token"] = access_token
        self.cookies["refresh_token"] = refresh_token

    @property
    def csrf(self) -> str:
        return self.cookies["csrf_token"]

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        cookies = {**self.cookies, **(extra or {})}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.client.get(url, headers=self._headers(kwargs.pop("cookies", None)), **kwargs)

    async def post(self, url: str, *, csrf: bool = True, data: dict[str, str] | None = None) -> Response:
        form = dict(data or {})
        if csrf:
            form["csrf_token"] = self.csrf
        return await self.client.post(url, data=form, headers=self._headers())

    async def link(
        self,
        idp: MockIdP,
        *,
        claims: dict[str, Any] | None = None,
        path: str = LINK_PATH,
        callback_browser: Browser | None = None,
    ) -> Response:
        """Press "Connect", play the IdP, and return the callback response."""
        started = await self.post(path)
        assert started.status_code == 303, started.text
        authorize_url = started.headers["location"]
        assert authorize_url.startswith(idp.authorization_endpoint)
        tx_cookie = _set_cookie_value(started, TX_COOKIE)
        assert tx_cookie
        callback_url = idp.authorize(authorize_url, claims=claims)
        split = urlsplit(callback_url)
        finisher = callback_browser or self
        return await finisher.get(f"{split.path}?{split.query}", cookies={TX_COOKIE: tx_cookie})


async def _browser(client: AsyncClient, db: AsyncSession, **fields: Any) -> tuple[Browser, User]:
    user = await _user(db, **fields)
    await db.commit()
    browser = Browser(client)
    await browser.sign_in(user.login)
    return browser, user


async def _identities(db: AsyncSession, user_id: int) -> list[UserIdentity]:
    return list(
        (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user_id).order_by(UserIdentity.id)))
        .scalars()
        .all()
    )


async def _sessions(db: AsyncSession, user_id: int) -> list[RefreshToken]:
    return list((await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))).scalars().all())


async def _second_provider(db: AsyncSession, idp: MockIdP) -> SsoProvider:
    provider = build_sso_provider(idp, slug="lnk-second", display_name="Second IdP", issuer="https://idp2.example.com")
    db.add(provider)
    await db.flush()
    return provider


async def _sso_session(db: AsyncSession, user: User, identity: UserIdentity, provider: SsoProvider) -> None:
    await AuthService().create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )


# ---------------------------------------------------------------------------
# Link
# ---------------------------------------------------------------------------


async def test_link_while_signed_in(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    browser, user = await _browser(client, db_session, login="lnkalice", email="lnk-alice@example.com")
    sessions_before = {s.session_id for s in await _sessions(db_session, user.id)}

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        response = await browser.link(mock_idp, claims={"email": "lnk-someone-else@example.com"})

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/my/profile/?sso_linked=test-idp"
    [identity] = await _identities(db_session, user.id)
    assert identity.provider_id == sso_provider.id
    assert identity.subject == mock_idp.default_sub
    assert identity.linked_via == "user_link"
    # The existing sessions stay and no new session is started.
    assert {s.session_id for s in await _sessions(db_session, user.id)} == sessions_before
    assert "access_token" not in response.headers.get("set-cookie", "")
    assert "sso.account.linked" in caplog.text and "method=user_link" in caplog.text
    assert subject_hash(mock_idp.default_sub) in caplog.text

    page = await browser.get("/my/profile/?sso_linked=test-idp")
    assert page.status_code == 200
    assert "Connected accounts" in page.text
    assert "Test IdP" in page.text
    assert "Your Test IdP account is now connected." in page.text


async def test_link_start_records_the_intent_and_user(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    import json

    import redis.asyncio as aioredis

    from specivo.core.config import get_settings
    from specivo.services.sso.flow import tx_key

    browser, user = await _browser(client, db_session, login="lnkintent", email="lnk-intent@example.com")
    started = await browser.post(LINK_PATH)
    assert started.status_code == 303
    state = _params(started.headers["location"])["state"]
    r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        record = json.loads(await r.get(tx_key(state)))
    finally:
        await r.aclose()
    assert record["intent"] == "link"
    assert record["user_id"] == user.id
    assert record["next"] == "/my/profile/"
    assert record["max_age"] == 0


async def test_link_requires_a_fresh_idp_sign_in(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    """Link sends prompt=login and max_age=0; an ID token from an old IdP session is refused."""
    import time

    browser, me = await _browser(client, db_session, login="lnkstale", email="lnk-stale@example.com")
    started = await browser.post(LINK_PATH)
    params = _params(started.headers["location"])
    assert params["prompt"] == "login"
    assert params["max_age"] == "0"

    response = await browser.link(mock_idp, claims={"auth_time": int(time.time()) - 3600})

    assert response.status_code == 302
    assert response.headers["location"] == "/my/profile/?sso_error=sso_token_invalid"
    assert await _identities(db_session, me.id) == []


async def test_link_without_auth_time_is_refused(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    from specivo.testing.oidc_mock import OMIT

    browser, me = await _browser(client, db_session, login="lnknoauth", email="lnk-noauth@example.com")

    response = await browser.link(mock_idp, claims={"auth_time": OMIT})

    assert response.status_code == 302
    assert response.headers["location"] == "/my/profile/?sso_error=sso_token_invalid"
    assert await _identities(db_session, me.id) == []


async def test_link_of_an_identity_owned_by_another_user_is_refused(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    owner = await _user(db_session, login="lnkowner", email="lnk-owner@example.com")
    await link_identity(db_session, owner, sso_provider, subject=mock_idp.default_sub)
    browser, me = await _browser(client, db_session, login="lnkthief", email="lnk-thief@example.com")

    response = await browser.link(mock_idp)

    assert response.status_code == 302
    assert response.headers["location"] == "/my/profile/?sso_error=sso_identity_in_use"
    assert await _identities(db_session, me.id) == []
    assert len(await _identities(db_session, owner.id)) == 1

    page = await browser.get("/my/profile/?sso_error=sso_identity_in_use")
    assert "already connected to another account" in page.text


async def test_second_link_at_the_same_provider_is_refused(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    browser, me = await _browser(client, db_session, login="lnktwice", email="lnk-twice@example.com")
    await link_identity(db_session, me, sso_provider, subject="lnk-earlier-subject")
    await db_session.commit()

    started = await browser.post(LINK_PATH)

    assert started.status_code == 303
    assert started.headers["location"] == "/my/profile/?sso_error=sso_provider_already_linked"
    assert mock_idp.calls["discovery"] == 0
    assert len(await _identities(db_session, me.id)) == 1


async def test_link_without_csrf_is_forbidden(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    browser, _ = await _browser(client, db_session, login="lnknocsrf", email="lnk-nocsrf@example.com")
    response = await browser.post(LINK_PATH, csrf=False)
    assert response.status_code == 403
    assert mock_idp.calls["discovery"] == 0


async def test_link_needs_a_signed_in_user(client: AsyncClient, sso_provider: SsoProvider) -> None:
    response = await client.post(LINK_PATH)
    assert response.status_code == 302
    assert response.headers["location"] == "/login/"


async def test_link_to_an_unknown_provider_is_404(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    browser, _ = await _browser(client, db_session, login="lnk404", email="lnk-404@example.com")
    response = await browser.post("/auth/oidc/no-such-idp/link/")
    assert response.status_code == 404


async def test_link_is_rate_limited_per_user(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    browser, _ = await _browser(client, db_session, login="lnkrate", email="lnk-rate@example.com")
    for _ in range(5):
        assert (await browser.post(LINK_PATH)).status_code == 303
    assert (await browser.post(LINK_PATH)).status_code == 429

    # Another user behind the same address has their own budget.
    other, _ = await _browser(client, db_session, login="lnkrate2", email="lnk-rate2@example.com")
    assert (await other.post(LINK_PATH)).status_code == 303


async def test_link_finished_by_another_signed_in_user_is_refused(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    """The link goes to the user who started it, never to whoever holds the browser at the callback."""
    starter, first = await _browser(client, db_session, login="lnkstarter", email="lnk-starter@example.com")
    swapped, second = await _browser(client, db_session, login="lnkswapped", email="lnk-swapped@example.com")

    response = await starter.link(mock_idp, callback_browser=swapped)

    assert response.status_code == 302
    assert "sso_error=sso_state_invalid" in response.headers["location"]
    assert await _identities(db_session, first.id) == []
    assert await _identities(db_session, second.id) == []


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


async def test_profile_lists_identities_and_connect_buttons(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    second = await _second_provider(db_session, mock_idp)
    browser, me = await _browser(client, db_session, login="lnkprofile", email="lnk-profile@example.com")
    identity = await link_identity(db_session, me, sso_provider, subject="lnk-profile-subject")
    await db_session.commit()

    page = await browser.get("/my/profile/")

    assert page.status_code == 200
    assert "Connected accounts" in page.text
    assert f'action="/my/identities/{identity.id}/unlink/"' in page.text
    assert f'action="/auth/oidc/{second.slug}/link/"' in page.text
    assert 'action="/auth/oidc/test-idp/link/"' not in page.text
    assert "Connect Second IdP" in page.text


async def test_profile_ignores_an_unknown_linked_slug(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    browser, _ = await _browser(client, db_session, login="lnkreflect", email="lnk-reflect@example.com")
    page = await browser.get("/my/profile/?sso_linked=%3Cb%3Epwned%3C/b%3E")
    assert page.status_code == 200
    assert "pwned" not in page.text


async def test_profile_message_is_translated(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    browser, _ = await _browser(client, db_session, login="lnkthai", email="lnk-thai@example.com", language="th")
    page = await browser.get("/my/profile/?sso_error=sso_last_sign_in_method", cookies={"specivo_lang": "th"})
    assert page.status_code == 200
    assert "only way to sign in" not in page.text


# ---------------------------------------------------------------------------
# Unlink
# ---------------------------------------------------------------------------


async def test_unlink_with_a_password_revokes_only_that_identitys_sessions(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    second = await _second_provider(db_session, mock_idp)
    browser, me = await _browser(client, db_session, login="lnkunlink", email="lnk-unlink@example.com")
    first_identity = await link_identity(db_session, me, sso_provider, subject="lnk-unlink-1")
    second_identity = await link_identity(db_session, me, second, subject="lnk-unlink-2")
    await _sso_session(db_session, me, first_identity, sso_provider)
    await _sso_session(db_session, me, second_identity, second)
    await db_session.commit()
    assert len(await _sessions(db_session, me.id)) == 3

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        response = await browser.post(f"/my/identities/{first_identity.id}/unlink/")

    assert response.status_code == 303
    assert response.headers["location"] == "/my/profile/?sso_unlinked=test-idp"
    assert [i.id for i in await _identities(db_session, me.id)] == [second_identity.id]
    remaining = await _sessions(db_session, me.id)
    assert sorted((s.auth_method, s.user_identity_id) for s in remaining) == sorted(
        [("password", None), ("sso", second_identity.id)]
    )
    assert "sso.account.unlinked" in caplog.text and "by=user" in caplog.text
    assert subject_hash("lnk-unlink-1") in caplog.text
    assert "lnk-unlink-1" not in caplog.text

    page = await browser.get("/my/profile/?sso_unlinked=test-idp")
    assert "Your Test IdP account was disconnected." in page.text


async def test_unlink_last_sign_in_method_is_refused(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    browser, me = await _browser(client, db_session, login="lnklast", email="lnk-last@example.com")
    identity = await link_identity(db_session, me, sso_provider, subject="lnk-last-subject")
    me.password_hash = None
    await db_session.commit()

    response = await browser.post(f"/my/identities/{identity.id}/unlink/")

    assert response.status_code == 303
    assert response.headers["location"] == "/my/profile/?sso_error=sso_last_sign_in_method"
    assert len(await _identities(db_session, me.id)) == 1


async def test_unlink_with_another_identity_and_no_password_is_allowed(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    second = await _second_provider(db_session, mock_idp)
    browser, me = await _browser(client, db_session, login="lnkother", email="lnk-other@example.com")
    identity = await link_identity(db_session, me, sso_provider, subject="lnk-other-1")
    await link_identity(db_session, me, second, subject="lnk-other-2")
    me.password_hash = None
    await db_session.commit()

    response = await browser.post(f"/my/identities/{identity.id}/unlink/")

    assert response.status_code == 303
    assert "sso_unlinked=" in response.headers["location"]
    assert len(await _identities(db_session, me.id)) == 1


async def test_unlink_without_csrf_is_forbidden(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    browser, me = await _browser(client, db_session, login="lnkcsrf", email="lnk-csrf@example.com")
    identity = await link_identity(db_session, me, sso_provider, subject="lnk-csrf-subject")
    await db_session.commit()

    response = await browser.post(f"/my/identities/{identity.id}/unlink/", csrf=False)

    assert response.status_code == 403
    assert len(await _identities(db_session, me.id)) == 1


async def test_unlink_of_another_users_identity_is_404(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    owner = await _user(db_session, login="lnkvictim", email="lnk-victim@example.com")
    identity = await link_identity(db_session, owner, sso_provider, subject="lnk-victim-subject")
    browser, _ = await _browser(client, db_session, login="lnkattacker", email="lnk-attacker@example.com")

    response = await browser.post(f"/my/identities/{identity.id}/unlink/")

    assert response.status_code == 404
    assert len(await _identities(db_session, owner.id)) == 1


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


async def test_admin_user_detail_lists_identities_and_unlinks_the_last_method(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = await _user(db_session, login="lnktarget", email="lnk-target@example.com", password_hash=None)
    identity = await link_identity(db_session, target, sso_provider, subject="lnk-target-subject")
    await _sso_session(db_session, target, identity, sso_provider)
    admin_browser, _ = await _browser(
        client, db_session, login="lnkadmin", email="lnk-admin@example.com", is_admin=True
    )

    page = await admin_browser.get(f"/admin/users/{target.id}/")
    assert page.status_code == 200
    assert "Connected accounts" in page.text
    assert "Test IdP" in page.text
    unlink_path = f"/admin/users/{target.id}/identities/{identity.id}/unlink/"
    assert f'action="{unlink_path}"' in page.text

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        response = await admin_browser.post(unlink_path)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/users/{target.id}/"
    assert await _identities(db_session, target.id) == []
    assert await _sessions(db_session, target.id) == []
    assert "sso.account.unlinked" in caplog.text and "by=admin" in caplog.text


async def test_admin_unlink_requires_csrf_and_admin(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    target = await _user(db_session, login="lnktarget2", email="lnk-target2@example.com")
    identity = await link_identity(db_session, target, sso_provider, subject="lnk-target2-subject")
    unlink_path = f"/admin/users/{target.id}/identities/{identity.id}/unlink/"

    admin_browser, _ = await _browser(
        client, db_session, login="lnkadmin2", email="lnk-admin2@example.com", is_admin=True
    )
    assert (await admin_browser.post(unlink_path, csrf=False)).status_code == 403

    member, _ = await _browser(client, db_session, login="lnkmember", email="lnk-member@example.com")
    assert (await member.post(unlink_path)).status_code == 403
    assert len(await _identities(db_session, target.id)) == 1


async def test_admin_unlink_of_an_identity_of_another_user_is_404(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    target = await _user(db_session, login="lnktarget3", email="lnk-target3@example.com")
    other = await _user(db_session, login="lnkother3", email="lnk-other3@example.com")
    identity = await link_identity(db_session, other, sso_provider, subject="lnk-other3-subject")
    admin_browser, _ = await _browser(
        client, db_session, login="lnkadmin3", email="lnk-admin3@example.com", is_admin=True
    )

    response = await admin_browser.post(f"/admin/users/{target.id}/identities/{identity.id}/unlink/")

    assert response.status_code == 404
    assert len(await _identities(db_session, other.id)) == 1


async def test_admin_api_lists_and_deletes_identities(
    admin_client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    target = await _user(db_session, login="lnkapi", email="lnk-api@example.com", password_hash=None)
    identity = await link_identity(db_session, target, sso_provider, subject="lnk-api-subject")
    identity.email_at_last_login = "lnk-api@example.com"
    await db_session.flush()

    listed = await admin_client.get(f"/api/v1/admin/users/{target.id}/identities/")
    assert listed.status_code == 200, listed.text
    [item] = listed.json()
    assert item["id"] == identity.id
    assert item["provider_slug"] == "test-idp"
    assert item["provider_name"] == "Test IdP"
    assert item["linked_via"] == "admin"
    assert item["email_at_last_login"] == "lnk-api@example.com"
    assert "subject" not in item

    deleted = await admin_client.delete(f"/api/v1/admin/users/{target.id}/identities/{identity.id}/")
    assert deleted.status_code == 204
    assert await _identities(db_session, target.id) == []

    missing = await admin_client.delete(f"/api/v1/admin/users/{target.id}/identities/{identity.id}/")
    assert missing.status_code == 404


async def test_admin_api_identities_need_an_admin(
    auth_client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    target = await _user(db_session, login="lnkapi2", email="lnk-api2@example.com")
    response = await auth_client.get(f"/api/v1/admin/users/{target.id}/identities/")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# JIT and auto-link end to end
# ---------------------------------------------------------------------------


async def test_jit_login_creates_the_user_with_the_thai_locale(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.trust_email_verified = True
    await db_session.flush()

    response = await sso_login(
        client,
        claims={
            "email": "lnk-jit@example.com",
            "preferred_username": "lnk.jit",
            "name": "Jit Person",
            "locale": "th-TH",
        },
    )

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/"
    user = (await db_session.execute(select(User).where(User.email == "lnk-jit@example.com"))).scalar_one()
    assert user.login == "lnk-jit"
    assert user.language == "th"
    assert user.password_hash is None
    assert user.email_verified_at is not None
    assert len(await _sessions(db_session, user.id)) == 1


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"email": "lnk-taken@example.com"}, "sso_email_conflict"),
        ({"email": None}, "sso_email_missing"),
    ],
    ids=["email_conflict", "email_missing"],
)
async def test_jit_refusals_reach_the_login_page(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    claims: dict[str, Any],
    code: str,
) -> None:
    from specivo.testing.oidc_mock import OMIT

    sso_provider.allow_jit_provisioning = True
    await _user(db_session, login="lnktaken", email="lnk-taken@example.com")
    claims = {k: (OMIT if v is None else v) for k, v in claims.items()}

    response = await sso_login(client, claims=claims)

    assert response.status_code == 302
    assert response.headers["location"] == f"/login/?sso_error={code}"
    assert await db_session.scalar(select(func.count()).select_from(UserIdentity)) == 0


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("sso_email_conflict", "Sign in with your password and connect the account from your profile."),
        ("sso_email_missing", "The identity provider did not send an email address."),
        ("sso_identity_in_use", "already connected to another account"),
    ],
)
async def test_login_page_explains_the_new_codes(client: AsyncClient, code: str, message: str) -> None:
    page = await client.get("/login/", params={"sso_error": code})
    assert page.status_code == 200
    assert message in page.text


async def test_auto_link_signs_in_the_matching_user(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
) -> None:
    sso_provider.link_policy = "auto_verified_email"
    sso_provider.trust_email_verified = True
    user = await _user(db_session, login="lnkauto", email="lnk-auto@example.com", email_verified_at=datetime.now(UTC))

    response = await sso_login(client, claims={"email": "lnk-auto@example.com", "email_verified": True})

    assert response.status_code == 302 and response.headers["location"] == "/"
    [identity] = await _identities(db_session, user.id)
    assert identity.linked_via == "auto_email"


async def test_auto_link_never_captures_an_admin_end_to_end(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
) -> None:
    sso_provider.link_policy = "auto_verified_email"
    sso_provider.trust_email_verified = True
    admin = await _user(
        db_session,
        AdminUserFactory,
        login="lnkautoadmin",
        email="lnk-autoadmin@example.com",
        email_verified_at=datetime.now(UTC),
    )

    response = await sso_login(client, claims={"email": "lnk-autoadmin@example.com", "email_verified": True})

    assert response.headers["location"] == "/login/?sso_error=sso_account_not_provisioned"
    assert await _identities(db_session, admin.id) == []
