"""The last-admin guard is serialised, and an SSO refusal's revocation is durable.

``another_active_admin_exists`` takes a transaction-scoped advisory lock
before it looks, so two transactions demoting or deactivating different
administrators at once cannot both see the other one still active. The lock
is held until the transaction ends.

A policy refusal in the SSO callback (``mapping.enforce_access``) commits the
sessions it revoked before raising, so the Redis "revoked" markers and the
deleted refresh rows cannot diverge when a later step fails.
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services import user_service
from specivo.services.auth_service import AuthService
from specivo.services.sso.errors import SsoNotInRequiredGroupError
from specivo.services.sso.id_token import IdTokenClaims
from specivo.services.sso.provisioning import resolve_user
from specivo.services.user_service import LAST_ADMIN_LOCK_KEY, UserService, another_active_admin_exists
from specivo.testing.factories.user import AdminUserFactory, UserFactory
from specivo.testing.sso import link_identity

pytestmark = pytest.mark.service

_counter = itertools.count(1)


async def _user(db: AsyncSession, factory=UserFactory) -> User:
    login = f"guard-user-{next(_counter)}"
    user = factory.build(login=login, email=f"{login}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _holds_lock(db: AsyncSession) -> bool:
    """This transaction holds the last-admin advisory lock."""
    count = await db.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
            "AND granted AND objsubid = 1 "
            "AND ((classid::bigint << 32) | objid::bigint) = :key"
        ),
        {"key": LAST_ADMIN_LOCK_KEY},
    )
    return bool(count)


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


async def test_guard_takes_the_advisory_lock(db_session: AsyncSession) -> None:
    admin = await _user(db_session, AdminUserFactory)
    assert not await _holds_lock(db_session)

    await another_active_admin_exists(db_session, admin.id)

    assert await _holds_lock(db_session)


async def test_deactivate_takes_the_lock_before_checking(db_session: AsyncSession) -> None:
    admin = await _user(db_session, AdminUserFactory)
    await _user(db_session, AdminUserFactory)

    await UserService().deactivate(db_session, admin, reason="admin", actor_id=None)

    assert await _holds_lock(db_session)


async def test_second_transaction_waits_for_the_guard(db_session: AsyncSession, db_engine: AsyncEngine) -> None:
    """While one transaction holds the guard, another cannot take it (it would wait)."""
    admin = await _user(db_session, AdminUserFactory)
    await another_active_admin_exists(db_session, admin.id)

    async with db_engine.connect() as other:
        taken = await other.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LAST_ADMIN_LOCK_KEY})
        await other.rollback()

    assert taken is False


async def test_guard_lock_free_once_released(db_engine: AsyncEngine) -> None:
    """The lock is transaction-scoped: nothing holds it outside a guard check."""
    async with db_engine.connect() as other:
        taken = await other.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LAST_ADMIN_LOCK_KEY})
        await other.rollback()

    assert taken is True


async def test_lock_key_does_not_collide_with_other_fixed_keys() -> None:
    from specivo.services.anonymous_access_service import ANONYMOUS_ACCESS_LOCK_KEY

    assert LAST_ADMIN_LOCK_KEY != ANONYMOUS_ACCESS_LOCK_KEY
    assert -(2**63) <= LAST_ADMIN_LOCK_KEY < 2**63


# ---------------------------------------------------------------------------
# A policy refusal commits its revocation
# ---------------------------------------------------------------------------


def _token(provider: SsoProvider, sub: str) -> IdTokenClaims:
    return IdTokenClaims(
        issuer=provider.issuer, subject=sub, audience=(provider.client_id,), expires_at=0, issued_at=0, nonce="n"
    )


async def _refused_login(db: AsyncSession, provider: SsoProvider, sub: str) -> None:
    await resolve_user(
        db,
        provider=provider,
        id_token=_token(provider, sub),
        claims={"email": "guard-member@example.com", "email_verified": True, "groups": ["guard-guests"]},
        intent="login",
    )


async def _sso_rows(db: AsyncSession, user_id: int) -> int:
    count = await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id))
    return int(count or 0)


async def _setup(db: AsyncSession, provider: SsoProvider, sub: str) -> tuple[int, UserIdentity]:
    provider.required_groups = ["guard-staff"]
    user = await _user(db)
    identity = await link_identity(db, user, provider, subject=sub)
    await AuthService().create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )
    user_id = user.id
    await db.commit()
    return user_id, identity


async def test_refusal_revocation_survives_a_later_rollback(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    user_id, _identity = await _setup(db_session, sso_provider, "guard-sub-1")
    assert await _sso_rows(db_session, user_id) == 1

    with pytest.raises(SsoNotInRequiredGroupError):
        await _refused_login(db_session, sso_provider, "guard-sub-1")
    await db_session.rollback()  # the request fails after the refusal

    assert await _sso_rows(db_session, user_id) == 0


async def test_refusal_revocation_survives_a_failing_deactivation(
    db_session: AsyncSession, sso_provider: SsoProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    sso_provider.deactivate_on_access_loss = True
    user_id, _identity = await _setup(db_session, sso_provider, "guard-sub-2")

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(user_service.UserService, "deactivate", _boom)

    with pytest.raises(RuntimeError):
        await _refused_login(db_session, sso_provider, "guard-sub-2")
    await db_session.rollback()

    assert await _sso_rows(db_session, user_id) == 0
