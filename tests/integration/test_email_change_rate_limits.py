"""Email change requests are limited per account and per target address (Redis, so serial)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.models.user import User
from specivo.services.email_change_service import ACCOUNT_RATE_LIMIT, ADDRESS_RATE_LIMIT, EmailChangeService
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

DELAY = "specivo.tasks.notifications.send_notification_email.delay"
_service = EmailChangeService()


async def _user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, email=f"{login}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _request(db: AsyncSession, user: User, address: str, password: str = TEST_PASSWORD) -> Any:
    return await _service.request_change(db, user, new_email=address, current_password=password, session_id=None)


async def test_requests_are_limited_per_account(db_session: AsyncSession) -> None:
    user = await _user(db_session, "emrlaccount")
    limit = ACCOUNT_RATE_LIMIT[1]
    with patch(DELAY) as delay:
        # Wrong passwords count as well.
        with pytest.raises(AppError) as wrong:
            await _request(db_session, user, "emrl-a0@example.com", password="wrong-password")
        assert wrong.value.code == "email_change_password_invalid"
        for n in range(1, limit):
            await _request(db_session, user, f"emrl-a{n}@example.com")
        with pytest.raises(AppError) as exc:
            await _request(db_session, user, "emrl-over@example.com")
    assert exc.value.status_code == 429
    assert exc.value.code == "rate_limit_exceeded"
    assert delay.call_count == limit - 1


async def test_requests_are_limited_per_target_address(db_session: AsyncSession) -> None:
    first = await _user(db_session, "emrladdr1")
    second = await _user(db_session, "emrladdr2")
    limit = ADDRESS_RATE_LIMIT[1]
    with patch(DELAY):
        for _ in range(limit):
            await _request(db_session, first, "emrl-target@example.com")
        # Another account, the same address (in another case): refused.
        with pytest.raises(AppError) as exc:
            await _request(db_session, second, "EMRL-Target@example.com")
        assert exc.value.status_code == 429
        # The second account itself is not limited.
        await _request(db_session, second, "emrl-other@example.com")
