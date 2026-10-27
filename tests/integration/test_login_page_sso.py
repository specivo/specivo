"""The login page with single sign-on: provider buttons, hidden form, auto-redirect, break-glass.

* Every enabled provider gets a "Sign in with {display_name}" link, ordered
  by ``position``, that carries the sanitised ``next``.
* ``sso.hide_local_login`` hides the password form behind a "Sign in with a
  local account" link to ``?local=1``; the form is never removed for good.
* ``sso.auto_redirect_provider`` sends ``GET /login/`` straight to that
  provider, except for ``?local=1``, ``?logged_out=1``, an ``sso_error``, a
  disabled or missing provider, or a visitor who is already signed in.
* ``SSO_FORCE_LOCAL_LOGIN=true`` beats every one of those settings.
"""

from __future__ import annotations

from collections.abc import Iterator
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.sso import SsoProvider
from specivo.schemas.sso import AuthSettingsUpdate
from specivo.services.sso.auth_settings import update_auth_settings
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import build_sso_provider

pytestmark = pytest.mark.integration

PASSWORD_FIELD = 'id="password-field"'
LOCAL_LINK_TEXT = "Sign in with a local account"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _configure(db: AsyncSession, **fields: object) -> None:
    await update_auth_settings(db, AuthSettingsUpdate(**fields))


async def _second_provider(db: AsyncSession, idp: MockIdP, **overrides: object) -> SsoProvider:
    values: dict[str, object] = {"slug": "second-idp", "display_name": "Second IdP", "position": 5}
    values.update(overrides)
    provider = build_sso_provider(idp, **values)
    db.add(provider)
    await db.flush()
    return provider


def _button_hrefs(html: str) -> list[str]:
    """The ``href`` of every provider button, in page order."""
    hrefs = []
    for chunk in html.split('class="sp-sso-provider-btn"')[1:]:
        start = chunk.index('href="') + len('href="')
        hrefs.append(unescape(chunk[start : chunk.index('"', start)]))
    return hrefs


@pytest.fixture
def force_local_login(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SSO_FORCE_LOCAL_LOGIN", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("SSO_FORCE_LOCAL_LOGIN")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Provider buttons
# ---------------------------------------------------------------------------


async def test_no_provider_means_no_buttons_and_the_form(client: AsyncClient) -> None:
    page = await client.get("/login/")
    assert page.status_code == 200
    assert _button_hrefs(page.text) == []
    assert PASSWORD_FIELD in page.text
    assert LOCAL_LINK_TEXT not in page.text


async def test_buttons_for_enabled_providers_in_position_order(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, mock_idp: MockIdP
) -> None:
    sso_provider.position = 10
    await _second_provider(db_session, mock_idp, position=1)
    await _second_provider(db_session, mock_idp, slug="off-idp", display_name="Off IdP", position=0, enabled=False)
    await db_session.flush()

    page = await client.get("/login/")
    assert page.status_code == 200
    assert _button_hrefs(page.text) == ["/auth/oidc/second-idp/login/", "/auth/oidc/test-idp/login/"]
    assert "Sign in with Second IdP" in page.text
    assert "Sign in with Test IdP" in page.text
    assert "Off IdP" not in page.text
    # Both ways to sign in are on the page by default.
    assert PASSWORD_FIELD in page.text


async def test_buttons_carry_the_next_path(client: AsyncClient, sso_provider: SsoProvider) -> None:
    page = await client.get("/login/", params={"next": "/projects/ACME/?tab=open&x=1"})
    (href,) = _button_hrefs(page.text)
    split = urlsplit(href)
    assert split.path == "/auth/oidc/test-idp/login/"
    assert parse_qs(split.query) == {"next": ["/projects/ACME/?tab=open&x=1"]}


@pytest.mark.parametrize("bad_next", ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:x"])
async def test_buttons_drop_an_unsafe_next(client: AsyncClient, sso_provider: SsoProvider, bad_next: str) -> None:
    page = await client.get("/login/", params={"next": bad_next})
    assert _button_hrefs(page.text) == ["/auth/oidc/test-idp/login/"]
    assert "evil.example" not in page.text


async def test_display_name_is_escaped(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.display_name = "<b>Corp</b>"
    await db_session.flush()
    page = await client.get("/login/")
    assert "<b>Corp</b>" not in page.text
    assert "&lt;b&gt;Corp&lt;/b&gt;" in page.text


async def test_buttons_keep_the_stealth_prefix(
    monkeypatch: pytest.MonkeyPatch,
    db_engine,
    _test_connection,
    sso_provider: SsoProvider,
) -> None:
    from specivo.core.database import get_db, set_engine
    from specivo.testing.conftest_base import _create_test_app, _make_test_get_db

    monkeypatch.setenv("STEALTH_PREFIX", "/hidden")
    get_settings.cache_clear()
    try:
        app = _create_test_app()
        set_engine(db_engine)
        app.dependency_overrides[get_db] = _make_test_get_db(_test_connection)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            page = await ac.get("/hidden/login/", params={"next": "/hidden/projects/"})
            assert page.status_code == 200
            (href,) = _button_hrefs(page.text)
            split = urlsplit(href)
            assert split.path == "/hidden/auth/oidc/test-idp/login/"
            assert parse_qs(split.query) == {"next": ["/hidden/projects/"]}
    finally:
        monkeypatch.delenv("STEALTH_PREFIX")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Hidden local form
# ---------------------------------------------------------------------------


async def test_hide_local_login_hides_the_form(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, hide_local_login=True)
    page = await client.get("/login/", params={"next": "/projects/"})
    assert page.status_code == 200
    assert PASSWORD_FIELD not in page.text
    assert _button_hrefs(page.text)
    assert LOCAL_LINK_TEXT in page.text
    assert 'href="?local=1&amp;next=%2Fprojects%2F"' in page.text


async def test_local_1_shows_the_form_and_password_login_still_works(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, hide_local_login=True)
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()

    page = await client.get("/login/", params={"local": "1"})
    assert page.status_code == 200
    assert PASSWORD_FIELD in page.text
    # The provider buttons stay available next to the form.
    assert _button_hrefs(page.text) == ["/auth/oidc/test-idp/login/"]
    assert LOCAL_LINK_TEXT not in page.text

    login = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text


async def test_hide_local_login_without_any_enabled_provider_keeps_the_form(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    """Hiding the form with nothing to show instead would lock everybody out."""
    sso_provider.enabled = False
    await _configure(db_session, hide_local_login=True)
    page = await client.get("/login/")
    assert PASSWORD_FIELD in page.text


async def test_idp_unreachable_points_admins_to_the_local_form(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, hide_local_login=True)
    page = await client.get("/login/", params={"sso_error": "sso_idp_unreachable"})
    assert page.status_code == 200
    assert "The identity provider is unavailable." in page.text
    assert "Administrators can sign in with a local account." in page.text
    assert 'href="?local=1"' in page.text


async def test_other_sso_errors_do_not_add_the_admin_hint(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, hide_local_login=True)
    page = await client.get("/login/", params={"sso_error": "sso_idp_error"})
    assert "Administrators can sign in with a local account." not in page.text


async def test_admins_only_shows_a_hint(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, local_login_admins_only=True)
    page = await client.get("/login/")
    assert "Only administrators can sign in with a password." in page.text


# ---------------------------------------------------------------------------
# Auto-redirect
# ---------------------------------------------------------------------------


async def test_auto_redirect_sends_the_visitor_to_the_provider(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, auto_redirect_provider=sso_provider.slug)
    response = await client.get("/login/", params={"next": "/projects/ACME/"})
    assert response.status_code == 302
    split = urlsplit(response.headers["location"])
    assert split.path == "/auth/oidc/test-idp/login/"
    assert parse_qs(split.query) == {"next": ["/projects/ACME/"]}
    assert response.headers.get("cache-control") == "no-store"


async def test_auto_redirect_drops_an_unsafe_next(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, auto_redirect_provider=sso_provider.slug)
    response = await client.get("/login/", params={"next": "//evil.example/"})
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/oidc/test-idp/login/"


@pytest.mark.parametrize(
    "params",
    [
        {"local": "1"},
        {"logged_out": "1"},
        {"sso_error": "sso_idp_unreachable"},
        {"sso_error": "anything"},
        {"reset": "ok"},
    ],
)
async def test_auto_redirect_is_skipped(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider, params: dict[str, str]
) -> None:
    await _configure(db_session, auto_redirect_provider=sso_provider.slug)
    response = await client.get("/login/", params=params)
    assert response.status_code == 200


async def test_auto_redirect_is_skipped_for_a_disabled_provider(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, auto_redirect_provider=sso_provider.slug)
    sso_provider.enabled = False
    await db_session.flush()
    response = await client.get("/login/")
    assert response.status_code == 200
    assert PASSWORD_FIELD in response.text


async def test_auto_redirect_is_skipped_for_a_signed_in_visitor(
    auth_client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, auto_redirect_provider=sso_provider.slug)
    response = await auth_client.get("/login/")
    assert response.status_code == 200
    assert "You are already signed in." in response.text


# ---------------------------------------------------------------------------
# SSO_FORCE_LOCAL_LOGIN
# ---------------------------------------------------------------------------


async def test_force_local_login_beats_every_setting(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    force_local_login: None,
) -> None:
    await _configure(
        db_session,
        hide_local_login=True,
        local_login_admins_only=True,
        auto_redirect_provider=sso_provider.slug,
    )
    page = await client.get("/login/")
    assert page.status_code == 200
    assert PASSWORD_FIELD in page.text
    assert LOCAL_LINK_TEXT not in page.text
    assert "Only administrators can sign in with a password." not in page.text
    # The buttons are still offered.
    assert _button_hrefs(page.text) == ["/auth/oidc/test-idp/login/"]

    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    login = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------


async def test_login_page_sso_texts_are_translated(
    client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    await _configure(db_session, hide_local_login=True, local_login_admins_only=True)
    thai = (await client.get("/login/", headers={"Accept-Language": "th"})).text
    assert "Test IdP" in thai
    assert "Sign in with Test IdP" not in thai
    assert LOCAL_LINK_TEXT not in thai
    assert "Only administrators can sign in with a password." not in thai

    unreachable = (
        await client.get("/login/", params={"sso_error": "sso_idp_unreachable"}, headers={"Accept-Language": "th"})
    ).text
    assert "Administrators can sign in with a local account." not in unreachable
