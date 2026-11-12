"""Service tests for AuthService.request_password_reset against a real database."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import PasswordResetToken
from specivo.models.user import User
from specivo.services.auth_service import AuthService
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


async def _add_user(db: AsyncSession, **overrides: object) -> User:
    user = UserFactory.build(**overrides)
    db.add(user)
    await db.flush()
    return user


async def _tokens(db: AsyncSession, user: User) -> list[PasswordResetToken]:
    result = await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    return list(result.scalars().all())


async def test_active_user_gets_a_token_and_one_email(db_session: AsyncSession) -> None:
    user = await _add_user(db_session, status="active")

    with patch("specivo.services.auth_service.send_notification_email") as email:
        user_id = await AuthService().request_password_reset(db_session, email=user.email)

    assert user_id == user.id
    assert len(await _tokens(db_session, user)) == 1
    email.delay.assert_called_once()
    assert email.delay.call_args.args[0] == user.email
    assert "reset" in email.delay.call_args.args[1].lower()


async def test_unknown_email_sends_nothing(db_session: AsyncSession) -> None:
    with patch("specivo.services.auth_service.send_notification_email") as email:
        result = await AuthService().request_password_reset(db_session, email="nobody@example.com")

    assert result is None
    email.delay.assert_not_called()


async def test_deactivated_user_gets_no_token_and_no_email(db_session: AsyncSession) -> None:
    user = await _add_user(db_session, status="deactivated")

    with patch("specivo.services.auth_service.send_notification_email") as email:
        result = await AuthService().request_password_reset(db_session, email=user.email)

    assert result is None
    assert await _tokens(db_session, user) == []
    email.delay.assert_not_called()


async def test_a_new_request_invalidates_the_earlier_unused_token(db_session: AsyncSession) -> None:
    user = await _add_user(db_session, status="active")
    service = AuthService()

    with patch("specivo.services.auth_service.send_notification_email"):
        await service.request_password_reset(db_session, email=user.email)
        await service.request_password_reset(db_session, email=user.email)

    tokens = await _tokens(db_session, user)
    await db_session.refresh(tokens[0])
    assert len(tokens) == 2
    assert sorted(t.used_at is None for t in tokens) == [False, True]
