"""Deprovisioning: deactivating an account ends its sessions and its API keys.

Covers the "Deprovisioning" section of the SSO design:

- ``UserService.deactivate`` (every user, not only SSO): status
  ``deactivated``, every session revoked (refresh rows gone, access tokens
  refused at once), every API key ``is_active=False``, one log line with the
  counts. Another user's keys are never touched; a service account goes
  through the same path.
- ``UserService.reactivate`` sets the account active again and leaves the
  keys inactive.
- The last active admin is never deactivated by the service.
- ``deactivate_on_access_loss`` on a provider: a sign-in refused by the access
  policy also deactivates the linked account (never a service account, never
  the last admin); off, only the identity's sessions end.

Logins carry a ``deprov`` prefix: modules run in parallel inside uncommitted
transactions and those columns are unique.
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.core.security import _authenticate_jwt
from specivo.models.auth import ApiKey, RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import AuthService
from specivo.services.sso.claims import Known
from specivo.services.sso.errors import SsoDomainNotAllowedError, SsoNotInRequiredGroupError
from specivo.services.sso.id_token import IdTokenClaims
from specivo.services.sso.mapping import enforce_access
from specivo.services.sso.provisioning import resolve_user
from specivo.services.user_service import LastActiveAdminError, UserService
from specivo.testing.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory
from specivo.testing.sso import link_identity

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

SUB = "deprov-subject-1"
VERIFIED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_counter = itertools.count(1)
_users = UserService()
_keys = ApiKeyService()
_auth = AuthService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name(stem: str) -> str:
    return f"deprov-{stem}-{next(_counter)}"


async def _user(db: AsyncSession, factory: Any = UserFactory, **fields: Any) -> User:
    fields.setdefault("login", _name("user"))
    fields.setdefault("email", f"{fields['login']}@example.com")
    fields.setdefault("email_verified_at", VERIFIED_AT)
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _key(db: AsyncSession, user: User, name: str = "key") -> tuple[ApiKey, str]:
    return await _keys.create_key(db, user_id=user.id, name=name)


async def _active_keys(db: AsyncSession, user_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True))
        )
    )


async def _session_count(db: AsyncSession, user_id: int) -> int:
    return int(await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)))


async def _only_admin(db: AsyncSession, user: User) -> None:
    """Make ``user`` the only active admin (rolled back with the test)."""
    await db.execute(
        update(User).where(User.is_admin.is_(True), User.id != user.id).values(status="locked", locked_until=None)
    )


def _token(provider: SsoProvider, sub: str = SUB) -> IdTokenClaims:
    return IdTokenClaims(
        issuer=provider.issuer, subject=sub, audience=(provider.client_id,), expires_at=0, issued_at=0, nonce="n"
    )


async def _sso_login(db: AsyncSession, provider: SsoProvider, **claims: Any) -> None:
    values: dict[str, Any] = {"email": "deprov-member@example.com", "email_verified": True, "name": "Deprov Member"}
    values.update(claims)
    await resolve_user(db, provider=provider, id_token=_token(provider), claims=values, intent="login")


async def _linked(db: AsyncSession, provider: SsoProvider, factory: Any = UserFactory, **fields: Any) -> User:
    user = await _user(db, factory, **fields)
    await link_identity(db, user, provider, subject=SUB)
    return user


async def _sso_session(db: AsyncSession, user: User, provider: SsoProvider) -> None:
    identity = (
        await db.execute(
            select(UserIdentity).where(UserIdentity.user_id == user.id, UserIdentity.provider_id == provider.id)
        )
    ).scalar_one()
    await _auth.create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )


# ---------------------------------------------------------------------------
# UserService.deactivate
# ---------------------------------------------------------------------------


class TestDeactivate:
    async def test_revokes_sessions_and_deactivates_keys(self, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        login = await _auth.login(db_session, user.login, TEST_PASSWORD)
        await _auth.login(db_session, user.login, TEST_PASSWORD)
        _key_row, raw = await _key(db_session, user, "first")
        await _key(db_session, user, "second")

        result = await _users.deactivate(db_session, user, reason="admin", actor_id=None)

        assert user.status == "deactivated"
        assert result.sessions_revoked == 2
        assert result.api_keys_deactivated == 2
        assert await _session_count(db_session, user.id) == 0
        assert await _active_keys(db_session, user.id) == 0
        # The keys are kept (audit trail), only switched off.
        assert int(await db_session.scalar(select(func.count()).where(ApiKey.user_id == user.id))) == 2

        # The access token already issued is refused on the next request.
        with pytest.raises(AppError) as jwt_exc:
            await _authenticate_jwt(login.access_token, db_session)
        assert jwt_exc.value.code in {"auth_token_revoked", "auth_account_deactivated"}
        assert jwt_exc.value.status_code == 401

        # The refresh token cannot mint a new access token.
        with pytest.raises(AppError) as refresh_exc:
            await _auth.refresh(db_session, login.refresh_token)
        assert refresh_exc.value.status_code == 401

        # The API key is refused.
        with pytest.raises(AppError) as key_exc:
            await _keys.authenticate(db_session, raw)
        assert key_exc.value.status_code == 401

    async def test_does_not_touch_other_users_keys(self, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        other = await _user(db_session)
        agent = await _user(db_session, ServiceAccountFactory, password_hash=None)
        await _key(db_session, user)
        await _key(db_session, other)
        _agent_key, agent_raw = await _key(db_session, agent)

        await _users.deactivate(db_session, user, reason="admin", actor_id=None)

        assert await _active_keys(db_session, other.id) == 1
        assert await _active_keys(db_session, agent.id) == 1
        authenticated, _ = await _keys.authenticate(db_session, agent_raw)
        assert authenticated.id == agent.id

    async def test_service_account_keys_are_deactivated_the_same_way(self, db_session: AsyncSession) -> None:
        agent = await _user(db_session, ServiceAccountFactory, password_hash=None)
        _key_row, raw = await _key(db_session, agent)

        result = await _users.deactivate(db_session, agent, reason="admin", actor_id=None)

        assert result.api_keys_deactivated == 1
        assert agent.status == "deactivated"
        with pytest.raises(AppError):
            await _keys.authenticate(db_session, raw)

    async def test_logs_the_counts(self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
        user = await _user(db_session)
        await _auth.login(db_session, user.login, TEST_PASSWORD)
        await _key(db_session, user)

        with caplog.at_level(logging.INFO):
            await _users.deactivate(db_session, user, reason="admin", actor_id=42)

        lines = [r.getMessage() for r in caplog.records if "user.deprovisioned" in r.getMessage()]
        assert len(lines) == 1
        assert f"user_id={user.id}" in lines[0]
        assert "reason=admin" in lines[0]
        assert "actor=42" in lines[0]
        assert "sessions_revoked=1" in lines[0]
        assert "api_keys_deactivated=1" in lines[0]

    async def test_last_active_admin_is_refused(self, db_session: AsyncSession) -> None:
        admin = await _user(db_session, AdminUserFactory)
        await _only_admin(db_session, admin)

        with pytest.raises(LastActiveAdminError) as exc:
            await _users.deactivate(db_session, admin, reason="admin", actor_id=None)

        assert exc.value.code == "last_active_admin"
        assert admin.status == "active"

    async def test_admin_with_another_admin_can_be_deactivated(self, db_session: AsyncSession) -> None:
        admin = await _user(db_session, AdminUserFactory)
        await _user(db_session, AdminUserFactory)

        await _users.deactivate(db_session, admin, reason="admin", actor_id=None)

        assert admin.status == "deactivated"


# ---------------------------------------------------------------------------
# UserService.reactivate
# ---------------------------------------------------------------------------


class TestReactivate:
    async def test_keys_stay_inactive(self, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        _key_row, raw = await _key(db_session, user)
        await _users.deactivate(db_session, user, reason="admin", actor_id=None)

        inactive = await _users.reactivate(db_session, user, actor_id=None)

        assert user.status == "active"
        assert inactive == 1
        assert await _active_keys(db_session, user.id) == 0
        with pytest.raises(AppError) as exc:
            await _keys.authenticate(db_session, raw)
        assert exc.value.code == "api_key_inactive"
        # The user signs in again with the password.
        assert (await _auth.login(db_session, user.login, TEST_PASSWORD)).access_token


# ---------------------------------------------------------------------------
# deactivate_on_access_loss
# ---------------------------------------------------------------------------


class TestDeactivateOnAccessLoss:
    async def test_on_deactivates_the_user_and_its_keys(
        self, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        sso_provider.required_groups = ["deprov-staff"]
        sso_provider.deactivate_on_access_loss = True
        user = await _linked(db_session, sso_provider)
        await _sso_session(db_session, user, sso_provider)
        await _auth.login(db_session, user.login, TEST_PASSWORD)
        await _key(db_session, user)

        with pytest.raises(SsoNotInRequiredGroupError):
            await _sso_login(db_session, sso_provider, groups=["deprov-guests"])

        await db_session.refresh(user)
        assert user.status == "deactivated"
        # The password session ends as well: the whole account is deprovisioned.
        assert await _session_count(db_session, user.id) == 0
        assert await _active_keys(db_session, user.id) == 0

    async def test_domain_refusal_also_deactivates(self, db_session: AsyncSession, sso_provider: SsoProvider) -> None:
        sso_provider.allowed_email_domains = ["allowed.example.com"]
        sso_provider.trust_email_verified = True
        sso_provider.deactivate_on_access_loss = True
        user = await _linked(db_session, sso_provider)

        with pytest.raises(SsoDomainNotAllowedError):
            await _sso_login(db_session, sso_provider, email="deprov-member@other.example.com")

        await db_session.refresh(user)
        assert user.status == "deactivated"

    async def test_off_only_revokes_the_identitys_sessions(
        self, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        sso_provider.required_groups = ["deprov-staff"]
        assert sso_provider.deactivate_on_access_loss is False
        user = await _linked(db_session, sso_provider)
        await _sso_session(db_session, user, sso_provider)
        await _auth.login(db_session, user.login, TEST_PASSWORD)
        await _key(db_session, user)

        with pytest.raises(SsoNotInRequiredGroupError):
            await _sso_login(db_session, sso_provider, groups=[])

        await db_session.refresh(user)
        assert user.status == "active"
        assert await _session_count(db_session, user.id) == 1
        assert await _active_keys(db_session, user.id) == 1

    async def test_allowed_login_deactivates_nothing(self, db_session: AsyncSession, sso_provider: SsoProvider) -> None:
        sso_provider.required_groups = ["deprov-staff"]
        sso_provider.deactivate_on_access_loss = True
        user = await _linked(db_session, sso_provider)

        await _sso_login(db_session, sso_provider, groups=["deprov-staff"])

        assert user.status == "active"

    async def test_last_admin_is_not_deactivated(
        self, db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        sso_provider.required_groups = ["deprov-staff"]
        sso_provider.deactivate_on_access_loss = True
        admin = await _linked(db_session, sso_provider, AdminUserFactory)
        await _only_admin(db_session, admin)
        await _key(db_session, admin)

        with caplog.at_level(logging.INFO), pytest.raises(SsoNotInRequiredGroupError):
            await _sso_login(db_session, sso_provider, groups=[])

        await db_session.refresh(admin)
        assert admin.status == "active"
        assert await _active_keys(db_session, admin.id) == 1
        assert any(r.levelno == logging.WARNING and "deactivation_skipped" in r.getMessage() for r in caplog.records), (
            caplog.text
        )

    async def test_never_deactivates_a_service_account(
        self, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        """A service account cannot hold an identity; the guard holds even if it is handed one."""
        sso_provider.required_groups = ["deprov-staff"]
        sso_provider.deactivate_on_access_loss = True
        agent = await _user(db_session, ServiceAccountFactory, password_hash=None)
        await _key(db_session, agent)
        # Never persisted (a trigger refuses identities of service accounts).
        identity = UserIdentity(id=2_000_000_000, user_id=agent.id, provider_id=sso_provider.id, subject=SUB)

        with pytest.raises(SsoNotInRequiredGroupError):
            await enforce_access(
                db_session,
                provider=sso_provider,
                claims={},
                groups=Known(frozenset()),
                email=None,
                subject=SUB,
                user=agent,
                identity=identity,
            )

        assert agent.status == "active"
        assert await _active_keys(db_session, agent.id) == 1

    async def test_auto_link_target_is_not_deactivated(
        self, db_session: AsyncSession, sso_provider: SsoProvider
    ) -> None:
        """Only an account already linked to the identity is deprovisioned, not an email match."""
        sso_provider.required_groups = ["deprov-staff"]
        sso_provider.deactivate_on_access_loss = True
        sso_provider.trust_email_verified = True
        sso_provider.link_policy = "auto_verified_email"
        user = await _user(db_session, login=_name("auto"), email="deprov-auto@example.com")

        with pytest.raises(SsoNotInRequiredGroupError):
            await _sso_login(db_session, sso_provider, email="deprov-auto@example.com", groups=[])

        await db_session.refresh(user)
        assert user.status == "active"
