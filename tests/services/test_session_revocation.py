"""Session identity and immediate revocation.

A login is one session: every refresh-token row it rotates through shares a
``session_id``, and every access token it mints carries that id as ``sid``.
``SessionService.revoke`` deletes the rows and marks the id revoked in Redis,
so an access token of the session stops working on the next request instead
of when it expires. Redis being unreachable denies the token (fail closed),
exactly like the ``jti`` blocklist. A token minted before the ``sid`` claim
existed is still accepted until it expires.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from unittest.mock import patch

import jwt
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import JWT_ALGORITHM
from specivo.core.exceptions import AppError
from specivo.core.security import REVOKED_SESSION_PREFIX, _authenticate_jwt
from specivo.core.utils import utcnow
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.auth_service import AuthService, _hash_token
from specivo.services.session_service import SessionService
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_auth = AuthService()
_sessions = SessionService()


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = UserFactory.build(login="sessrev_user", email="sessrev_user@example.com")
    db_session.add(u)
    await db_session.commit()
    return u


async def _login(db: AsyncSession, user: User) -> tuple[str, str]:
    result = await _auth.login(db, user.login, TEST_PASSWORD)
    return result.access_token, result.refresh_token


async def _row(db: AsyncSession, refresh_raw: str) -> RefreshToken | None:
    return await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash_token(refresh_raw)))


def _claims(access_token: str) -> dict:
    return jwt.decode(access_token, get_settings().secret_key, algorithms=[JWT_ALGORITHM])


async def _redis():
    import redis.asyncio as aioredis

    return aioredis.from_url(os.environ["REDIS_URL"])


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------


class TestSessionIdentity:
    async def test_login_creates_a_session_and_the_access_token_names_it(
        self, db_session: AsyncSession, user: User
    ) -> None:
        access, refresh = await _login(db_session, user)
        row = await _row(db_session, refresh)
        assert row is not None
        assert isinstance(row.session_id, uuid.UUID)
        assert row.auth_method == "password"
        assert _claims(access)["sid"] == str(row.session_id)

    async def test_two_logins_are_two_sessions(self, db_session: AsyncSession, user: User) -> None:
        _a1, r1 = await _login(db_session, user)
        _a2, r2 = await _login(db_session, user)
        row1, row2 = await _row(db_session, r1), await _row(db_session, r2)
        assert row1 is not None and row2 is not None
        assert row1.session_id != row2.session_id

    async def test_rotation_keeps_the_session_id(self, db_session: AsyncSession, user: User) -> None:
        _access, refresh = await _login(db_session, user)
        original = await _row(db_session, refresh)
        assert original is not None
        session_id = original.session_id

        new_access, new_refresh, _ = await _auth.refresh(db_session, refresh)
        new_access2, new_refresh2, _ = await _auth.refresh(db_session, new_refresh)

        rotated = await _row(db_session, new_refresh2)
        assert rotated is not None
        assert rotated.session_id == session_id
        assert _claims(new_access)["sid"] == str(session_id)
        assert _claims(new_access2)["sid"] == str(session_id)
        assert await _row(db_session, refresh) is None

    async def test_rotation_copies_every_session_column(self, db_session: AsyncSession, user: User) -> None:
        provider = SsoProvider(
            slug="sessrev-idp", display_name="Session IdP", issuer="https://idp.example.com", client_id="c"
        )
        db_session.add(provider)
        await db_session.flush()
        identity = UserIdentity(
            user_id=user.id, provider_id=provider.id, issuer=provider.issuer, subject="s-1", linked_via="user_link"
        )
        db_session.add(identity)
        await db_session.flush()

        _access, refresh = await _login(db_session, user)
        row = await _row(db_session, refresh)
        assert row is not None
        cap = utcnow() + timedelta(hours=24)
        row.auth_method = "sso"
        row.user_identity_id = identity.id
        row.idp_sid = "idp-session-1"
        row.id_token_encrypted = "sealed-id-token"
        row.absolute_expires_at = cap
        await db_session.flush()

        _new_access, new_refresh, _ = await _auth.refresh(db_session, refresh)
        rotated = await _row(db_session, new_refresh)
        assert rotated is not None
        assert rotated.auth_method == "sso"
        assert rotated.user_identity_id == identity.id
        assert rotated.idp_sid == "idp-session-1"
        assert rotated.id_token_encrypted == "sealed-id-token"
        assert rotated.absolute_expires_at == cap
        # The refresh row never outlives the absolute cap.
        assert rotated.expires_at <= cap


# ---------------------------------------------------------------------------
# Absolute session lifetime
# ---------------------------------------------------------------------------


class TestAbsoluteExpiry:
    async def test_refresh_after_the_absolute_cap_is_refused(self, db_session: AsyncSession, user: User) -> None:
        _access, refresh = await _login(db_session, user)
        row = await _row(db_session, refresh)
        assert row is not None
        row.absolute_expires_at = utcnow() - timedelta(seconds=1)
        await db_session.flush()

        with pytest.raises(AppError) as exc_info:
            await _auth.refresh(db_session, refresh)
        assert exc_info.value.code == "auth_refresh_expired"
        assert exc_info.value.status_code == 401
        assert await _row(db_session, refresh) is None

    async def test_refresh_before_the_absolute_cap_works(self, db_session: AsyncSession, user: User) -> None:
        _access, refresh = await _login(db_session, user)
        row = await _row(db_session, refresh)
        assert row is not None
        row.absolute_expires_at = utcnow() + timedelta(minutes=5)
        await db_session.flush()

        _new_access, new_refresh, _ = await _auth.refresh(db_session, refresh)
        assert await _row(db_session, new_refresh) is not None


# ---------------------------------------------------------------------------
# SessionService.revoke
# ---------------------------------------------------------------------------


class TestRevoke:
    async def test_revoke_deletes_every_row_of_the_session(self, db_session: AsyncSession, user: User) -> None:
        _access, refresh = await _login(db_session, user)
        row = await _row(db_session, refresh)
        assert row is not None
        # A second row of the same session (e.g. a rotation racing a revoke).
        sibling = RefreshToken(
            user_id=user.id,
            token_hash=_hash_token("sibling-token"),
            session_id=row.session_id,
            expires_at=row.expires_at,
        )
        db_session.add(sibling)
        _other_access, other_refresh = await _login(db_session, user)
        await db_session.flush()

        count = await _sessions.revoke(db_session, [row.session_id], reason="test")

        assert count == 2
        remaining = await db_session.scalar(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.session_id == row.session_id)
        )
        assert remaining == 0
        assert await _row(db_session, other_refresh) is not None

    async def test_revoke_marks_the_session_in_redis_for_the_access_token_lifetime(
        self, db_session: AsyncSession, user: User
    ) -> None:
        _access, refresh = await _login(db_session, user)
        row = await _row(db_session, refresh)
        assert row is not None

        await _sessions.revoke(db_session, [row.session_id], reason="test")

        r = await _redis()
        try:
            ttl = await r.ttl(f"{REVOKED_SESSION_PREFIX}{row.session_id}")
        finally:
            await r.aclose()
        lifetime = get_settings().access_token_expire_minutes * 60
        assert lifetime - 5 <= ttl <= lifetime + 1

    async def test_revoked_access_token_is_refused_on_the_next_request(
        self, db_session: AsyncSession, user: User
    ) -> None:
        access, refresh = await _login(db_session, user)
        assert (await _authenticate_jwt(access, db_session)).id == user.id
        row = await _row(db_session, refresh)
        assert row is not None

        await _sessions.revoke(db_session, [row.session_id], reason="test")

        with pytest.raises(AppError) as exc_info:
            await _authenticate_jwt(access, db_session)
        assert exc_info.value.code == "auth_token_revoked"

    async def test_revoking_one_session_leaves_the_others_working(self, db_session: AsyncSession, user: User) -> None:
        _a1, r1 = await _login(db_session, user)
        a2, _r2 = await _login(db_session, user)
        row = await _row(db_session, r1)
        assert row is not None

        await _sessions.revoke(db_session, [row.session_id], reason="test")

        assert (await _authenticate_jwt(a2, db_session)).id == user.id

    async def test_revoke_of_nothing_is_a_no_op(self, db_session: AsyncSession) -> None:
        assert await _sessions.revoke(db_session, [], reason="test") == 0

    async def test_redis_down_denies_a_token_with_sid(self, db_session: AsyncSession, user: User) -> None:
        access, _refresh = await _login(db_session, user)
        with (
            patch("specivo.core.redis.get_redis", side_effect=ConnectionError("Redis down")),
            pytest.raises(AppError) as exc_info,
        ):
            await _authenticate_jwt(access, db_session)
        assert exc_info.value.code == "auth_token_revoked"

    async def test_token_without_sid_is_still_accepted(self, db_session: AsyncSession, user: User) -> None:
        settings = get_settings()
        now = utcnow()
        legacy = jwt.encode(
            {
                "sub": str(user.id),
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "jti": str(uuid.uuid4()),
                "rem": True,
            },
            settings.secret_key,
            algorithm=JWT_ALGORITHM,
        )
        assert (await _authenticate_jwt(legacy, db_session)).id == user.id


# ---------------------------------------------------------------------------
# Bulk helpers
# ---------------------------------------------------------------------------


class TestBulkRevocation:
    async def test_revoke_user_sessions_kills_every_access_token(self, db_session: AsyncSession, user: User) -> None:
        tokens = [await _login(db_session, user) for _ in range(3)]

        count = await _sessions.revoke_user_sessions(db_session, user.id, reason="test")

        assert count == 3
        for access, _refresh in tokens:
            with pytest.raises(AppError) as exc_info:
                await _authenticate_jwt(access, db_session)
            assert exc_info.value.code == "auth_token_revoked"

    async def test_revoke_user_sessions_can_keep_one(self, db_session: AsyncSession, user: User) -> None:
        keep_access, keep_refresh = await _login(db_session, user)
        other_access, _other_refresh = await _login(db_session, user)
        keep_row = await _row(db_session, keep_refresh)
        assert keep_row is not None

        count = await _sessions.revoke_user_sessions(
            db_session, user.id, reason="test", keep_session_id=keep_row.session_id
        )

        assert count == 1
        assert (await _authenticate_jwt(keep_access, db_session)).id == user.id
        with pytest.raises(AppError):
            await _authenticate_jwt(other_access, db_session)

    async def test_revoke_by_identity_and_idp_sid(self, db_session: AsyncSession, user: User) -> None:
        provider = SsoProvider(
            slug="sessrev-bulk", display_name="Bulk IdP", issuer="https://bulk.example.com", client_id="c"
        )
        db_session.add(provider)
        await db_session.flush()
        identity = UserIdentity(
            user_id=user.id, provider_id=provider.id, issuer=provider.issuer, subject="s-2", linked_via="user_link"
        )
        db_session.add(identity)
        await db_session.flush()

        logins = [await _login(db_session, user) for _ in range(4)]
        rows = [await _row(db_session, refresh) for _access, refresh in logins]
        assert all(r is not None for r in rows)
        # Sessions 0 and 1 came from IdP session "a", 2 from "b"; 3 is a password login.
        for row, idp_sid in zip(rows[:3], ("a", "a", "b"), strict=True):
            assert row is not None
            row.auth_method = "sso"
            row.user_identity_id = identity.id
            row.idp_sid = idp_sid
        await db_session.flush()

        assert await _sessions.revoke_by_idp_sid(db_session, user_identity_id=identity.id, idp_sid="a", reason="t") == 2
        with pytest.raises(AppError):
            await _authenticate_jwt(logins[0][0], db_session)
        assert (await _authenticate_jwt(logins[2][0], db_session)).id == user.id

        assert await _sessions.revoke_by_identity(db_session, identity.id, reason="t") == 1
        with pytest.raises(AppError):
            await _authenticate_jwt(logins[2][0], db_session)
        # The password session is not tied to the identity and survives.
        assert (await _authenticate_jwt(logins[3][0], db_session)).id == user.id
