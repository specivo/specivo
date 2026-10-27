"""``sso.allow_api_keys_for_sso_users``: API keys of users who sign in only through SSO.

With the setting off, a user who has no password and at least one connected
identity cannot create API keys (403 ``api_keys_not_allowed_for_sso_users``,
on the API and as a notice on ``/my/api-keys/``) and the keys they already
hold are refused when used, with the same code. A user with a password, a
service account, and everyone while the setting is on (the default) are not
affected.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.schemas.sso import AuthSettingsUpdate
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import AuthService
from specivo.services.sso.auth_settings import update_auth_settings
from specivo.testing.sso import link_identity
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

CODE = "api_keys_not_allowed_for_sso_users"
_keys = ApiKeyService()


async def _user(db: AsyncSession, factory: Any = UserFactory, **fields: Any) -> User:
    fields.setdefault("email", f"{fields['login']}@example.com")
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _sso_user(db: AsyncSession, provider: SsoProvider, login: str, *, password: bool = False) -> User:
    extra: dict[str, Any] = {} if password else {"password_hash": None}
    user = await _user(db, login=login, **extra)
    await link_identity(db, user, provider, subject=f"{login}-sub")
    return user


async def _sso_access_token(db: AsyncSession, user: User, provider: SsoProvider) -> str:
    from sqlalchemy import select

    from specivo.models.sso import UserIdentity

    identity = (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalar_one()
    session = await AuthService().create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )
    await db.commit()
    return session.access_token


async def _disallow(db: AsyncSession) -> None:
    await update_auth_settings(db, AuthSettingsUpdate(allow_api_keys_for_sso_users=False))
    await db.commit()


async def _create_key(client: AsyncClient, credential: str) -> Response:
    client.cookies.clear()
    return await client.post(
        "/api/v1/my/api-keys/", json={"name": "laptop"}, headers={"Authorization": f"Bearer {credential}"}
    )


async def _use(client: AsyncClient, credential: str) -> Response:
    client.cookies.clear()
    return await client.get("/api/v1/my/api-keys/", headers={"Authorization": f"Bearer {credential}"})


def _code(resp: Response) -> str:
    return resp.json()["errors"][0]["code"]


class TestSettingOff:
    async def test_sso_only_user_cannot_create_a_key(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_create")
        access = await _sso_access_token(db_session, user, sso_provider)
        await _disallow(db_session)

        resp = await _create_key(client, access)

        assert resp.status_code == 403, resp.text
        assert _code(resp) == CODE

    async def test_sso_only_users_existing_key_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_use")
        _key, raw = await _keys.create_key(db_session, user_id=user.id, name="old")
        await db_session.commit()
        assert (await _use(client, raw)).status_code == 200
        await _disallow(db_session)

        resp = await _use(client, raw)

        assert resp.status_code == 403, resp.text
        assert _code(resp) == CODE

    async def test_admin_cannot_create_a_key_for_an_sso_only_user(
        self, admin_client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_admin")
        await _disallow(db_session)

        resp = await admin_client.post(f"/api/v1/admin/users/{user.id}/api-keys/", json={"name": "for-them"})

        assert resp.status_code == 403, resp.text
        assert _code(resp) == CODE

    async def test_user_with_a_password_is_not_affected(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_pwd", password=True)
        _key, raw = await _keys.create_key(db_session, user_id=user.id, name="old")
        await db_session.commit()
        await _disallow(db_session)

        assert (await _use(client, raw)).status_code == 200
        assert (await _create_key(client, raw)).status_code == 201

    async def test_service_account_is_not_affected(self, agent_client: AsyncClient, db_session: AsyncSession) -> None:
        await _disallow(db_session)

        assert (await agent_client.get("/api/v1/my/api-keys/")).status_code == 200
        created = await agent_client.post("/api/v1/my/api-keys/", json={"name": "second"})
        assert created.status_code == 201, created.text

    async def test_user_without_password_or_identity_is_not_affected(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _user(db_session, login="akso_plain", password_hash=None)
        _key, raw = await _keys.create_key(db_session, user_id=user.id, name="old")
        await db_session.commit()
        await _disallow(db_session)

        assert (await _use(client, raw)).status_code == 200


class TestSettingOn:
    async def test_sso_only_user_may_create_and_use_keys(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_on")
        access = await _sso_access_token(db_session, user, sso_provider)

        created = await _create_key(client, access)

        assert created.status_code == 201, created.text
        assert (await _use(client, created.json()["raw_key"])).status_code == 200


class TestApiKeysPage:
    async def test_page_explains_and_hides_the_create_button(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_page")
        access = await _sso_access_token(db_session, user, sso_provider)
        await _disallow(db_session)

        client.cookies.clear()
        page = await client.get("/my/api-keys/", headers={"Cookie": f"access_token={access}"})

        assert page.status_code == 200
        assert "API keys are turned off for accounts that sign in through single sign-on" in page.text
        assert "New API Key" not in page.text

    async def test_page_in_thai(self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_thai")
        user.language = "th"
        access = await _sso_access_token(db_session, user, sso_provider)
        await _disallow(db_session)

        client.cookies.clear()
        page = await client.get("/my/api-keys/", headers={"Cookie": f"access_token={access}; specivo_lang=th"})

        assert page.status_code == 200
        assert "API keys are turned off" not in page.text
        assert "sp-api-keys-disabled" in page.text

    async def test_page_unchanged_while_allowed(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _sso_user(db_session, sso_provider, "akso_page_on")
        access = await _sso_access_token(db_session, user, sso_provider)

        client.cookies.clear()
        page = await client.get("/my/api-keys/", headers={"Cookie": f"access_token={access}"})

        assert page.status_code == 200
        assert "sp-api-keys-disabled" not in page.text
        assert "New API Key" in page.text
