"""A password reset ends every session of the account at once.

A reset is the compromise-recovery path: whoever held a session before it
must lose it on the very next request. Both the self-service email reset
(``POST /auth/reset-password/``) and the operator CLI
(``specivo.cli.admin reset-password``) revoke every session through
``SessionService``: access tokens issued before the reset are refused with
``auth_token_revoked`` and their refresh tokens no longer rotate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli.admin import _reset_password
from specivo.core.utils import utcnow
from specivo.models.auth import PasswordResetToken, RefreshToken
from specivo.models.user import User
from specivo.services.auth_service import _hash_token
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_NEW_PASSWORD = "Another-Strong-Passw0rd!"


async def _create_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, email=f"{login}@example.com")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _sign_in(client: AsyncClient, login: str) -> tuple[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    body = resp.json()
    return body["access_token"], body["refresh_token"]


async def _assert_sessions_dead(
    client: AsyncClient, db: AsyncSession, user: User, sessions: list[tuple[str, str]]
) -> None:
    for access, refresh in sessions:
        client.cookies.clear()
        after = await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {access}"})
        assert after.status_code == 401
        assert after.json()["errors"][0]["code"] == "auth_token_revoked"
        replay = await client.post("/api/v1/auth/refresh/", json={"refresh_token": refresh})
        assert replay.status_code == 401
    remaining = await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user.id))
    assert remaining == 0


async def test_email_reset_revokes_every_session(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _create_user(db_session, "pwreset_revoke_email")
    sessions = [await _sign_in(client, user.login) for _ in range(2)]

    raw_token = "pwreset-revoke-email-token"
    db_session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    resp = await client.post("/api/v1/auth/reset-password/", json={"token": raw_token, "new_password": _NEW_PASSWORD})
    assert resp.status_code == 200, resp.text

    await _assert_sessions_dead(client, db_session, user, sessions)


async def test_cli_reset_revokes_every_session(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _create_user(db_session, "pwreset_revoke_cli")
    sessions = [await _sign_in(client, user.login) for _ in range(2)]

    await _reset_password(db_session, user.login, _NEW_PASSWORD)

    await _assert_sessions_dead(client, db_session, user, sessions)
