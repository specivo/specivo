"""Email password reset never gives an SSO-only account a local password.

An SSO-only account has no password and at least one connected identity. If
"Forgot password" could give it one, its owner would step around every rule
that depends on signing in through the IdP (API keys turned off for SSO users,
password sign-in for administrators only, deprovisioning at the IdP).

- ``POST /auth/forgot-password/`` answers exactly as for an unknown email
  (202, nothing sent, no token stored), so the response reveals nothing.
- A reset token for such an account (issued before the identity was
  connected) is refused like an invalid link, by the form and by
  ``POST /auth/reset-password/``, and the account still has no password.
- An administrator can still set a password deliberately.
- A user with a password and a connected identity resets as before.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.auth import PasswordResetToken
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.auth_service import _hash_token
from specivo.testing.sso import link_identity
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_SEND = "specivo.tasks.notifications.send_notification_email.delay"
_NEW_PASSWORD = "A-new-Strong-Passw0rd!"


async def _user(db: AsyncSession, provider: SsoProvider | None, login: str, **fields: Any) -> User:
    user = UserFactory.build(login=login, email=f"{login}@example.com", **fields)
    db.add(user)
    await db.flush()
    if provider is not None:
        await link_identity(db, user, provider, subject=f"{login}-sub")
    await db.commit()
    return user


async def _tokens(db: AsyncSession, user_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
    )


async def _issue_token(db: AsyncSession, user: User) -> str:
    raw = secrets.token_urlsafe(48)
    db.add(PasswordResetToken(user_id=user.id, token_hash=_hash_token(raw), expires_at=utcnow() + timedelta(hours=1)))
    await db.commit()
    return raw


class TestRequest:
    async def test_sso_only_gets_the_unknown_email_answer(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, sso_provider, "rst_sso", password_hash=None)

        with patch(_SEND) as send:
            known = await client.post("/api/v1/auth/forgot-password/", json={"email": user.email})
            unknown = await client.post("/api/v1/auth/forgot-password/", json={"email": "rst-nobody@example.com"})

        assert known.status_code == unknown.status_code == 202
        assert known.json() == unknown.json()
        send.assert_not_called()
        assert await _tokens(db_session, user.id) == 0

    async def test_password_user_with_an_identity_still_gets_the_email(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, sso_provider, "rst_both")

        with patch(_SEND) as send:
            resp = await client.post("/api/v1/auth/forgot-password/", json={"email": user.email})

        assert resp.status_code == 202
        assert send.called
        assert await _tokens(db_session, user.id) == 1


class TestCompletion:
    async def test_token_of_an_sso_only_account_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, sso_provider, "rst_tok", password_hash=None)
        raw = await _issue_token(db_session, user)

        resp = await client.post("/api/v1/auth/reset-password/", json={"token": raw, "new_password": _NEW_PASSWORD})

        assert resp.status_code == 400
        error = resp.json()["errors"][0]
        assert error["code"] == "password_reset_invalid"
        assert error["message"] == "Invalid or expired password reset link"
        await db_session.refresh(user)
        assert user.password_hash is None

    async def test_form_treats_the_token_as_invalid(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, sso_provider, "rst_form", password_hash=None)
        raw = await _issue_token(db_session, user)

        page = await client.get(f"/reset-password/?token={raw}")

        assert page.status_code == 200
        assert "This password reset link is invalid" in page.text

    async def test_password_user_with_an_identity_can_complete(
        self, client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, sso_provider, "rst_both_done")
        raw = await _issue_token(db_session, user)

        resp = await client.post("/api/v1/auth/reset-password/", json={"token": raw, "new_password": _NEW_PASSWORD})

        assert resp.status_code == 200, resp.text


class TestAdminReset:
    async def test_admin_can_still_set_a_password(
        self, admin_client: AsyncClient, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        user = await _user(db_session, sso_provider, "rst_admin", password_hash=None)

        resp = await admin_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/", json={"password": _NEW_PASSWORD}
        )

        assert resp.status_code == 200, resp.text
        await db_session.refresh(user)
        assert user.password_hash is not None
