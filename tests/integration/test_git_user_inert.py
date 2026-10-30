"""The ``$git`` service account writes repository journals and nothing signs in as it.

Covers every way into an account: password login, password reset (request and
confirm), API keys (creation and use, over REST and MCP), SSO (an identity and
the verified-email auto link), the admin actions on an account, the CLI
promotion, and the database CHECK that keeps every ``$`` login without a
password and without admin rights.

The row is created by the migration. Tests run inside a rolled-back
transaction, so a test that plants a token or key for it leaves nothing behind.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli.admin import _create_admin, _reset_password
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError
from specivo.core.utils import utcnow
from specivo.models.auth import ApiKey, PasswordResetToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.anonymous_user_service import AnonymousUserProtectedError
from specivo.services.api_key_service import ApiKeyService, _hash_key
from specivo.services.auth_service import AuthService, _hash_token, _make_access_token
from specivo.services.repository_hooks import get_git_user
from specivo.services.user_service import UserService
from specivo.testing.sso import SsoLoginDriver, link_identity
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

GIT_EMAIL = "git@specivo.invalid"


@pytest_asyncio.fixture
async def git(db_session: AsyncSession) -> User:
    return await get_git_user(db_session)


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="gitinert_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _error_code(resp) -> str:
    return resp.json()["errors"][0]["code"]


async def _plant_key(db: AsyncSession, git: User) -> str:
    raw = "spv_planted-key-for-the-git-account"
    db.add(ApiKey(user_id=git.id, name="planted", key_prefix=raw[:12], key_hash=_hash_key(raw), is_active=True))
    await db.commit()
    return raw


# ---------------------------------------------------------------------------
# The row and the database CHECK
# ---------------------------------------------------------------------------


async def test_the_row_is_an_inert_service_account(git: User) -> None:
    assert (git.email, git.password_hash, git.is_admin, git.is_service_account) == (GIT_EMAIL, None, False, True)
    assert git.is_system_principal


@pytest.mark.parametrize("assignment", ["is_admin = true", "password_hash = 'not-a-real-hash'"])
async def test_database_refuses_a_password_or_admin_rights(
    db_session: AsyncSession, git: User, assignment: str
) -> None:
    with pytest.raises(IntegrityError, match="ck_users_system_principal_inert"):
        await db_session.execute(text(f"UPDATE users SET {assignment} WHERE login = '$git'"))


# ---------------------------------------------------------------------------
# Password and reset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("login", ["$git", GIT_EMAIL])
async def test_password_login_is_refused(client: AsyncClient, git: User, login: str) -> None:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": "any-password-at-all"})

    assert resp.status_code == 401
    assert _error_code(resp) == "auth_invalid_credentials"


async def test_password_reset_request_issues_no_token(db_session: AsyncSession, git: User) -> None:
    assert await AuthService().request_password_reset(db_session, GIT_EMAIL) is None

    count = await db_session.scalar(
        select(func.count()).select_from(PasswordResetToken).where(PasswordResetToken.user_id == git.id)
    )
    assert count == 0


async def test_password_reset_confirm_cannot_set_a_password(db_session: AsyncSession, git: User) -> None:
    raw = "planted-reset-token-for-the-git-account"
    db_session.add(
        PasswordResetToken(user_id=git.id, token_hash=_hash_token(raw), expires_at=utcnow() + timedelta(hours=1))
    )
    await db_session.flush()

    with pytest.raises(AppError) as exc:
        await AuthService().reset_password_with_token(db_session, raw, "a-brand-new-password")

    assert exc.value.code == "password_reset_invalid"
    await db_session.refresh(git)
    assert git.password_hash is None


async def test_jwt_minted_for_its_id_is_rejected(client: AsyncClient, git: User) -> None:
    resp = await client.get("/api/v1/my/api-keys/", headers=_bearer(git))

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API keys and MCP
# ---------------------------------------------------------------------------


async def test_api_key_creation_is_refused(db_session: AsyncSession, git: User) -> None:
    with pytest.raises(AnonymousUserProtectedError):
        await ApiKeyService().create_key(db_session, git.id, "not-allowed")

    count = await db_session.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == git.id))
    assert count == 0


async def test_admin_cannot_create_an_api_key_for_it(client: AsyncClient, admin: User, git: User) -> None:
    resp = await client.post(
        f"/api/v1/admin/users/{git.id}/api-keys/", json={"name": "not-allowed"}, headers=_bearer(admin)
    )

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


async def test_planted_api_key_is_refused(client: AsyncClient, db_session: AsyncSession, git: User) -> None:
    raw = await _plant_key(db_session, git)

    resp = await client.get("/api/v1/my/api-keys/", headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 401
    assert _error_code(resp) == "api_key_invalid"


async def test_planted_api_key_is_refused_by_mcp_tools(db_session: AsyncSession, git: User) -> None:
    from specivo.mcp.auth import authenticate_mcp_tool, mcp_raw_key_var

    raw = await _plant_key(db_session, git)
    token = mcp_raw_key_var.set(raw)
    try:
        with pytest.raises(AppError) as exc:
            await authenticate_mcp_tool(db_session)
    finally:
        mcp_raw_key_var.reset(token)

    assert exc.value.code == "api_key_invalid"


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------


async def test_an_identity_cannot_be_connected_to_it(
    db_session: AsyncSession, sso_provider: SsoProvider, git: User
) -> None:
    with pytest.raises(IntegrityError):
        await link_identity(db_session, git, sso_provider, subject="git-subject")


async def test_sso_with_its_email_never_signs_in_as_it(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    git: User,
) -> None:
    sso_provider.link_policy = "auto_verified_email"
    sso_provider.trust_email_verified = True
    await db_session.flush()

    response = await sso_login(client, claims={"email": GIT_EMAIL, "email_verified": True})

    assert response.headers["location"].startswith("/login/?sso_error=")
    identities = await db_session.scalar(
        select(func.count()).select_from(UserIdentity).where(UserIdentity.user_id == git.id)
    )
    assert identities == 0


# ---------------------------------------------------------------------------
# Admin actions and the CLI
# ---------------------------------------------------------------------------


async def test_admin_cannot_set_its_password(client: AsyncClient, admin: User, git: User) -> None:
    resp = await client.post(
        f"/api/v1/admin/users/{git.id}/reset-password/",
        json={"password": "a-long-enough-password"},
        headers=_bearer(admin),
    )

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


@pytest.mark.parametrize("action", ["unlock", "lock"])
async def test_admin_cannot_lock_or_unlock_it(client: AsyncClient, admin: User, git: User, action: str) -> None:
    resp = await client.post(f"/api/v1/admin/users/{git.id}/{action}/", headers=_bearer(admin))

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


@pytest.mark.parametrize("action", ["deactivate", "reactivate"])
async def test_admin_cannot_deactivate_or_reactivate_it(
    client: AsyncClient, admin: User, git: User, action: str
) -> None:
    resp = await client.post(f"/api/v1/admin/users/{git.id}/{action}/", headers=_bearer(admin))

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


async def test_user_service_refuses_to_reactivate_it(db_session: AsyncSession, admin: User, git: User) -> None:
    with pytest.raises(AnonymousUserProtectedError):
        await UserService().reactivate(db_session, git, actor_id=admin.id)


async def test_cli_cannot_make_it_an_administrator(db_session: AsyncSession, git: User) -> None:
    with pytest.raises(SystemExit):
        await _create_admin(db_session, "$git", "promote@example.com", "password-long-enough")

    await db_session.refresh(git)
    assert not git.is_admin


async def test_cli_cannot_set_its_password(db_session: AsyncSession, git: User) -> None:
    with pytest.raises(SystemExit):
        await _reset_password(db_session, "$git", "password-long-enough")

    await db_session.refresh(git)
    assert git.password_hash is None


async def test_reserved_login_cannot_be_created(client: AsyncClient, admin: User) -> None:
    resp = await client.post(
        "/api/v1/admin/users/",
        json={
            "login": "$git2",
            "email": "git2@example.com",
            "display_name": "Git two",
            "password": "a-long-enough-password",
        },
        headers=_bearer(admin),
    )

    assert resp.status_code == 422
    assert any(err.get("field") == "login" for err in resp.json()["errors"])
