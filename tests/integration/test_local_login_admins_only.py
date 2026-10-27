"""``sso.local_login_admins_only``: password sign-in is the break-glass path for admins only.

A non-admin gets exactly the answer a wrong password gets (status, code,
message), whether their password is right or wrong and whether or not the
account exists, so the setting cannot be used to find accounts. The web login
form posts to the same API endpoint, so this covers both. The env switch
``SSO_FORCE_LOCAL_LOGIN`` turns the restriction off.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.user import User
from specivo.schemas.sso import AuthSettingsUpdate
from specivo.services.sso.auth_settings import update_auth_settings
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login/"


async def _user(db: AsyncSession, **fields: object) -> User:
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _admins_only(db: AsyncSession, value: bool = True) -> None:
    await update_auth_settings(db, AuthSettingsUpdate(local_login_admins_only=value))


def _error(response) -> tuple[int, str, str]:
    body = response.json()
    error = body["errors"][0]
    return response.status_code, error["code"], error["message"]


@pytest.fixture
def force_local_login(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SSO_FORCE_LOCAL_LOGIN", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("SSO_FORCE_LOCAL_LOGIN")
    get_settings.cache_clear()


async def test_admin_password_login_still_works(client: AsyncClient, db_session: AsyncSession) -> None:
    await _admins_only(db_session)
    admin = await _user(db_session, is_admin=True)
    response = await client.post(LOGIN_URL, json={"login": admin.login, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text


async def test_non_admin_gets_the_wrong_password_answer(client: AsyncClient, db_session: AsyncSession) -> None:
    await _admins_only(db_session)
    member = await _user(db_session)
    other = await _user(db_session)

    refused = await client.post(LOGIN_URL, json={"login": member.login, "password": TEST_PASSWORD})
    wrong_password = await client.post(LOGIN_URL, json={"login": other.login, "password": "not-the-password"})
    unknown = await client.post(LOGIN_URL, json={"login": "nobody-here", "password": TEST_PASSWORD})

    assert _error(refused) == _error(wrong_password) == _error(unknown)
    assert _error(refused)[:2] == (401, "auth_invalid_credentials")
    assert "access_token" not in refused.cookies


async def test_non_admin_refusal_does_not_count_towards_lockout(client: AsyncClient, db_session: AsyncSession) -> None:
    """The refusal is not a wrong password: it must not lock the account (or reveal its state)."""
    await _admins_only(db_session)
    member = await _user(db_session)
    for _ in range(6):
        await client.post(LOGIN_URL, json={"login": member.login, "password": TEST_PASSWORD})
    await db_session.refresh(member)
    assert member.failed_login_count == 0
    assert member.locked_until is None


async def test_non_admin_deactivated_account_is_not_revealed(client: AsyncClient, db_session: AsyncSession) -> None:
    await _admins_only(db_session)
    member = await _user(db_session, status="deactivated")
    response = await client.post(LOGIN_URL, json={"login": member.login, "password": TEST_PASSWORD})
    assert _error(response)[:2] == (401, "auth_invalid_credentials")


async def test_turning_the_setting_off_restores_login(client: AsyncClient, db_session: AsyncSession) -> None:
    await _admins_only(db_session)
    member = await _user(db_session)
    assert (await client.post(LOGIN_URL, json={"login": member.login, "password": TEST_PASSWORD})).status_code == 401

    await _admins_only(db_session, False)
    response = await client.post(LOGIN_URL, json={"login": member.login, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text


async def test_force_local_login_lifts_the_restriction(
    client: AsyncClient, db_session: AsyncSession, force_local_login: None
) -> None:
    await _admins_only(db_session)
    member = await _user(db_session)
    response = await client.post(LOGIN_URL, json={"login": member.login, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text


async def test_api_keys_are_unaffected(client: AsyncClient, db_session: AsyncSession) -> None:
    """Service accounts and agents authenticate with API keys, which this setting never touches."""
    from specivo.services.api_key_service import ApiKeyService

    await _admins_only(db_session)
    bot = await _user(db_session, is_service_account=True, password_hash=None)
    _, raw = await ApiKeyService().create_key(db_session, user_id=bot.id, name="agent")
    await db_session.flush()
    response = await client.get("/api/v1/projects/", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Admin accounts: no state is revealed while password sign-in is admins-only
# ---------------------------------------------------------------------------

_ADMIN_STATES: dict[str, dict[str, object]] = {
    "pending_verification": {"status": "pending_verification"},
    "deactivated": {"status": "deactivated"},
    "service_account": {"is_service_account": True},
    "admin_locked": {"status": "locked", "locked_until": None},
    "lockout_active": {"locked_until_minutes": 15},
}


async def _admin_in_state(db: AsyncSession, state: str) -> User:
    from datetime import timedelta

    from specivo.core.utils import utcnow

    fields = dict(_ADMIN_STATES[state])
    minutes = fields.pop("locked_until_minutes", None)
    if minutes is not None:
        fields["locked_until"] = utcnow() + timedelta(minutes=int(minutes))
        fields["failed_login_count"] = 5
    return await _user(db, is_admin=True, **fields)


@pytest.mark.parametrize("state", sorted(_ADMIN_STATES))
async def test_admin_account_state_is_not_revealed(
    client: AsyncClient, db_session: AsyncSession, state: str, caplog: pytest.LogCaptureFixture
) -> None:
    """With the right password, a blocked admin gets exactly the wrong-password answer; the log has the reason."""
    import logging

    await _admins_only(db_session)
    admin = await _admin_in_state(db_session, state)
    other_admin = await _user(db_session, is_admin=True)

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        refused = await client.post(LOGIN_URL, json={"login": admin.login, "password": TEST_PASSWORD})
    wrong_password = await client.post(LOGIN_URL, json={"login": other_admin.login, "password": "not-the-password"})

    assert _error(refused) == _error(wrong_password)
    assert _error(refused)[:2] == (401, "auth_invalid_credentials")
    assert "details" not in refused.json()["errors"][0] or not refused.json()["errors"][0]["details"]
    assert "access_token" not in refused.cookies
    assert f"sso.local_login.refused_admin user_id={admin.id}" in caplog.text


@pytest.mark.parametrize("state", sorted(_ADMIN_STATES))
async def test_admin_account_state_is_reported_when_the_setting_is_off(
    client: AsyncClient, db_session: AsyncSession, state: str
) -> None:
    """Unchanged behaviour without admins-only: the specific code is still returned."""
    admin = await _admin_in_state(db_session, state)
    response = await client.post(LOGIN_URL, json={"login": admin.login, "password": TEST_PASSWORD})
    assert response.status_code == 401
    assert _error(response)[1] != "auth_invalid_credentials"


async def test_admin_lockout_still_counts_but_is_not_revealed(client: AsyncClient, db_session: AsyncSession) -> None:
    """Wrong passwords still lock an admin out (brute-force protection), but the answer stays generic."""
    await _admins_only(db_session)
    admin = await _user(db_session, is_admin=True)
    answers = set()
    for _ in range(6):
        response = await client.post(LOGIN_URL, json={"login": admin.login, "password": "not-the-password"})
        answers.add(_error(response))
    await db_session.refresh(admin)
    assert admin.failed_login_count == 5
    assert admin.locked_until is not None

    locked = await client.post(LOGIN_URL, json={"login": admin.login, "password": TEST_PASSWORD})
    answers.add(_error(locked))
    assert answers == {(401, "auth_invalid_credentials", "Invalid login or password")}
