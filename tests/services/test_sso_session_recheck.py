"""SSO sessions follow the provider and the identity they came from.

- ``AuthService.refresh`` refuses an SSO session whose identity is gone
  (unlinked, deleted) or whose provider is disabled, and deletes its row
  for good; password sessions of the same user keep working.
- Disabling a provider (``ProviderService.update`` with ``enabled`` true to
  false) ends the SSO sessions it started and reports how many; enabling it
  again brings none of them back.
- ``session_max_age_hours`` is capped at one week.

Logins and emails carry a ``recheck`` prefix: those columns are unique.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.core.utils import utcnow
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.schemas.sso import SSO_SESSION_MAX_AGE_HOURS, SsoProviderCreate, SsoProviderUpdate
from specivo.services.auth_service import AuthService
from specivo.services.sso import provider_service as provider_module
from specivo.services.sso.provider_service import ProviderService
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.sso import link_identity

pytestmark = pytest.mark.service

_counter = itertools.count(1)
_auth = AuthService()
_providers = ProviderService()


@pytest.fixture(autouse=True)
def _no_cache_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(provider_id: int, **_: Any) -> None:
        return None

    monkeypatch.setattr(provider_module, "invalidate_provider_cache", _fake)


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = UserFactory.build(is_admin=True, login=f"recheck-admin-{next(_counter)}")
    db_session.add(user)
    await db_session.flush()
    return user


async def _user(db: AsyncSession) -> User:
    login = f"recheck-user-{next(_counter)}"
    user = UserFactory.build(login=login, email=f"{login}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _sso_login(db: AsyncSession, user: User, provider: SsoProvider, identity: UserIdentity) -> str:
    """Start an SSO session; returns its raw refresh token."""
    started = await _auth.create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )
    return started.refresh_token


async def _rows(db: AsyncSession, user_id: int) -> int:
    count = await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id))
    return int(count or 0)


async def _refused(db: AsyncSession, raw: str) -> None:
    with pytest.raises(AppError) as info:
        await _auth.refresh(db, raw)
    assert info.value.code == "auth_refresh_expired"


# ---------------------------------------------------------------------------
# Refresh re-checks the provider and the identity
# ---------------------------------------------------------------------------


async def test_sso_session_refreshes_while_provider_enabled(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    user = await _user(db_session)
    identity = await link_identity(db_session, user, sso_provider, subject="recheck-ok")
    raw = await _sso_login(db_session, user, sso_provider, identity)

    _access, new_raw, refreshed = await _auth.refresh(db_session, raw)

    assert refreshed.id == user.id
    assert new_raw != raw


async def test_refresh_refused_after_provider_disabled(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session)
    identity = await link_identity(db_session, user, sso_provider, subject="recheck-disabled")
    raw = await _sso_login(db_session, user, sso_provider, identity)
    # Disabled behind the service's back: refresh must not rely on the revocation sweep.
    await db_session.execute(update(SsoProvider).where(SsoProvider.id == sso_provider.id).values(enabled=False))
    user_id = user.id
    await db_session.commit()

    await _refused(db_session, raw)

    await db_session.rollback()  # the request fails; the deleted row must stay deleted
    assert await _rows(db_session, user_id) == 0


async def test_refresh_refused_after_identity_deleted(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session)
    identity = await link_identity(db_session, user, sso_provider, subject="recheck-unlinked")
    raw = await _sso_login(db_session, user, sso_provider, identity)
    # The FK is SET NULL: the session row survives with no identity.
    await db_session.execute(delete(UserIdentity).where(UserIdentity.id == identity.id))
    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    await db_session.refresh(row)
    assert row.auth_method == "sso" and row.user_identity_id is None

    await _refused(db_session, raw)

    assert await _rows(db_session, user.id) == 0


async def test_password_session_unaffected_by_disabled_provider(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    user = await _user(db_session)
    await link_identity(db_session, user, sso_provider, subject="recheck-password")
    login = await _auth.login(db_session, user.login, TEST_PASSWORD)
    await db_session.execute(update(SsoProvider).where(SsoProvider.id == sso_provider.id).values(enabled=False))

    _access, _new_raw, refreshed = await _auth.refresh(db_session, login.refresh_token)

    assert refreshed.id == user.id


# ---------------------------------------------------------------------------
# Disabling a provider ends its sessions
# ---------------------------------------------------------------------------


async def test_disabling_provider_revokes_its_sessions_and_counts_them(
    db_session: AsyncSession, sso_provider: SsoProvider, admin: User
) -> None:
    first, second = await _user(db_session), await _user(db_session)
    first_identity = await link_identity(db_session, first, sso_provider, subject="recheck-a")
    second_identity = await link_identity(db_session, second, sso_provider, subject="recheck-b")
    await _sso_login(db_session, first, sso_provider, first_identity)
    await _sso_login(db_session, first, sso_provider, first_identity)
    await _sso_login(db_session, second, sso_provider, second_identity)
    password = await _auth.login(db_session, first.login, TEST_PASSWORD)

    result = await _providers.update(db_session, sso_provider.id, SsoProviderUpdate(enabled=False), actor=admin)

    assert result.provider.enabled is False
    assert result.sessions_revoked == 3
    assert await _rows(db_session, second.id) == 0
    # The password session stays and still refreshes.
    assert await _rows(db_session, first.id) == 1
    await _auth.refresh(db_session, password.refresh_token)


async def test_reenabling_provider_does_not_resurrect_sessions(
    db_session: AsyncSession, sso_provider: SsoProvider, admin: User
) -> None:
    user = await _user(db_session)
    identity = await link_identity(db_session, user, sso_provider, subject="recheck-again")
    raw = await _sso_login(db_session, user, sso_provider, identity)

    await _providers.update(db_session, sso_provider.id, SsoProviderUpdate(enabled=False), actor=admin)
    again = await _providers.update(db_session, sso_provider.id, SsoProviderUpdate(enabled=True), actor=admin)

    assert again.sessions_revoked == 0
    assert await _rows(db_session, user.id) == 0
    await _refused(db_session, raw)


@pytest.mark.parametrize("change", [{"display_name": "Renamed"}, {"enabled": True}])
async def test_update_without_disabling_revokes_nothing(
    db_session: AsyncSession, sso_provider: SsoProvider, admin: User, change: dict[str, Any]
) -> None:
    user = await _user(db_session)
    identity = await link_identity(db_session, user, sso_provider, subject="recheck-keep")
    await _sso_login(db_session, user, sso_provider, identity)

    result = await _providers.update(db_session, sso_provider.id, SsoProviderUpdate(**change), actor=admin)

    assert result.sessions_revoked == 0
    assert await _rows(db_session, user.id) == 1


async def test_disabling_revocation_names_the_admin(
    db_session: AsyncSession, sso_provider: SsoProvider, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.services.session_service import SessionService

    calls: list[dict[str, Any]] = []
    original = SessionService.revoke_by_identity

    async def _spy(self, db, user_identity_id, *, reason, actor_id=None):
        calls.append({"reason": reason, "actor_id": actor_id})
        return await original(self, db, user_identity_id, reason=reason, actor_id=actor_id)

    monkeypatch.setattr(SessionService, "revoke_by_identity", _spy)
    user = await _user(db_session)
    await link_identity(db_session, user, sso_provider, subject="recheck-actor")

    await _providers.update(db_session, sso_provider.id, SsoProviderUpdate(enabled=False), actor=admin)

    assert calls == [{"reason": "sso_provider_disabled", "actor_id": admin.id}]


# ---------------------------------------------------------------------------
# Session length cap
# ---------------------------------------------------------------------------


def test_session_max_age_cap_is_one_week() -> None:
    assert SSO_SESSION_MAX_AGE_HOURS == 168


def test_session_max_age_above_cap_rejected() -> None:
    base = {"slug": "recheck-cap", "display_name": "Cap", "issuer": "https://idp.example.com", "client_id": "c"}
    assert SsoProviderCreate(**base, session_max_age_hours=168).session_max_age_hours == 168
    with pytest.raises(PydanticValidationError):
        SsoProviderCreate(**base, session_max_age_hours=169)
    assert SsoProviderUpdate(session_max_age_hours=168).session_max_age_hours == 168
    with pytest.raises(PydanticValidationError):
        SsoProviderUpdate(session_max_age_hours=720)


async def test_legacy_longer_max_age_is_clamped_at_login(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    """A row saved before the cap (up to 720 h) still gets at most a week."""
    sso_provider.session_max_age_hours = 720
    user = await _user(db_session)
    identity = await link_identity(db_session, user, sso_provider, subject=f"recheck-{uuid.uuid4().hex[:8]}")

    started = await _auth.create_sso_session(
        db_session, user=user, identity=identity, provider=sso_provider, id_token="x", idp_sid=None
    )

    assert started.absolute_expires_at <= utcnow() + timedelta(hours=168)
