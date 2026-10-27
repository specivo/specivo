"""The signed-in user's own pages for an account that signs in through SSO.

- Preferences, "Sessions": every live session is listed with how it signed in,
  "Signed in with {provider display name}" for an SSO session.
- ``/my/password/`` (and the Security section of the preferences) for a user
  with no password and a connected identity explains that they sign in
  through their identity provider instead of offering a form that cannot work;
  ``POST /api/v1/auth/change-password/`` refuses cleanly with
  ``password_change_unavailable`` and a message that says the same.
- The new strings are translated (Thai).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.auth_service import AuthService
from specivo.testing.sso import link_identity
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SSO_NOTICE = "You sign in through your identity provider"


async def _user(db: AsyncSession, **fields: Any) -> User:
    fields.setdefault("email", f"{fields['login']}@example.com")
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _sso_tokens(db: AsyncSession, user: User, provider: SsoProvider) -> str:
    identity = (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalar_one()
    session = await AuthService().create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )
    await db.commit()
    return session.access_token


async def _get(client: AsyncClient, url: str, access: str, lang: str | None = None) -> Response:
    client.cookies.clear()
    cookie = f"access_token={access}"
    if lang:
        cookie += f"; specivo_lang={lang}"
    return await client.get(url, headers={"Cookie": cookie})


async def _sso_only(db: AsyncSession, provider: SsoProvider, login: str, **fields: Any) -> tuple[User, str]:
    user = await _user(db, login=login, password_hash=None, **fields)
    await link_identity(db, user, provider, subject=f"{login}-sub")
    return user, await _sso_tokens(db, user, provider)


class TestSessionsList:
    async def test_sso_session_names_the_provider(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, login="prof_sess")
        await link_identity(db_session, user, sso_provider, subject="prof-sess-sub")
        access = await _sso_tokens(db_session, user, sso_provider)
        login = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
        assert login.status_code == 200

        page = await _get(client, "/my/preferences/", access)

        assert page.status_code == 200
        assert "Signed in with Test IdP" in page.text
        assert "Signed in with a password" in page.text

    async def test_sessions_in_thai(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        _user_row, access = await _sso_only(db_session, sso_provider, "prof_sess_th", language="th")

        page = await _get(client, "/my/preferences/", access, lang="th")

        assert page.status_code == 200
        assert "Test IdP" in page.text
        assert "Signed in with Test IdP" not in page.text


class TestPasswordPage:
    async def test_sso_only_user_gets_an_explanation(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        _user_row, access = await _sso_only(db_session, sso_provider, "prof_pwd")

        page = await _get(client, "/my/password/", access)

        assert page.status_code == 200
        assert SSO_NOTICE in page.text
        assert "Test IdP" in page.text
        assert 'id="current-password"' not in page.text

    async def test_preferences_security_section_explains_too(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        _user_row, access = await _sso_only(db_session, sso_provider, "prof_pref")

        page = await _get(client, "/my/preferences/", access)

        assert SSO_NOTICE in page.text
        assert 'id="current-password"' not in page.text

    async def test_user_with_a_password_keeps_the_form(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, login="prof_pwd_keep")
        await link_identity(db_session, user, sso_provider, subject="prof-keep-sub")
        access = await _sso_tokens(db_session, user, sso_provider)

        page = await _get(client, "/my/password/", access)

        assert 'id="current-password"' in page.text
        assert SSO_NOTICE not in page.text

    async def test_post_is_refused_cleanly(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        _user_row, access = await _sso_only(db_session, sso_provider, "prof_pwd_post")

        client.cookies.clear()
        resp = await client.post(
            "/api/v1/auth/change-password/",
            json={"current_password": "whatever", "new_password": "A-new-Strong-Passw0rd!"},
            headers={"Authorization": f"Bearer {access}"},
        )

        assert resp.status_code == 400
        error = resp.json()["errors"][0]
        assert error["code"] == "password_change_unavailable"
        assert "identity provider" in error["message"]
        assert "API key" not in error["message"]

    async def test_password_page_in_thai(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        _user_row, access = await _sso_only(db_session, sso_provider, "prof_pwd_th", language="th")

        page = await _get(client, "/my/password/", access, lang="th")

        assert page.status_code == 200
        assert SSO_NOTICE not in page.text
        assert "sp-password-sso-notice" in page.text
