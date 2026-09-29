"""The anonymous user: one reserved, inert ``users`` row.

Covers the schema guarantees (a single row, the inert CHECK, the membership
trigger), that nothing can authenticate as the row, that admin endpoints refuse
to change it, and that it is absent from the user listings and counts.

The row is created by the migration, not by a fixture. Tests run inside a
rolled-back transaction on a migrated database, so the row is always present,
and they reach it through ``get_anonymous_user``. A test that deletes or
modifies it only does so inside its own transaction.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli.admin import _create_admin, _reset_password
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError
from specivo.core.utils import utcnow
from specivo.importers.load.user_loader import _find_user_by_login
from specivo.models.auth import ApiKey, PasswordResetToken
from specivo.models.member import Member
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services import anonymous_user_service
from specivo.services.anonymous_user_service import (
    AnonymousUserProtectedError,
    clear_anonymous_user_cache,
    get_anonymous_user,
)
from specivo.services.api_key_service import ApiKeyService, _hash_key
from specivo.services.auth_service import AuthService, _hash_token, _make_access_token, _make_refresh_token
from specivo.services.settings_service import SettingsService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _fresh_anonymous_cache():
    clear_anonymous_user_cache()
    yield
    clear_anonymous_user_cache()


@pytest_asyncio.fixture
async def anonymous(db_session: AsyncSession) -> User:
    user = await get_anonymous_user(db_session)
    assert user is not None, "the migration creates the anonymous user"
    return user


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="anonp_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="ANONP", identifier="anon-principal")
    db_session.add(proj)
    await db_session.commit()
    return proj


def _bearer(user: User) -> dict[str, str]:
    """Authorization header carrying a genuinely signed JWT for *user*."""
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _error_code(resp) -> str:
    return resp.json()["errors"][0]["code"]


# ---------------------------------------------------------------------------
# Schema: one row, inert, never a member
# ---------------------------------------------------------------------------


async def test_migration_creates_exactly_one_inert_row(db_session: AsyncSession, anonymous: User) -> None:
    count = await db_session.scalar(select(func.count()).select_from(User).where(User.is_anonymous.is_(True)))

    assert count == 1
    assert anonymous.login == "$anonymous"
    assert anonymous.email == "anonymous@specivo.invalid"
    assert anonymous.display_name == "Anonymous"
    assert anonymous.status == "deactivated"
    assert anonymous.password_hash is None
    assert not anonymous.is_admin
    assert not anonymous.is_service_account
    assert not anonymous.must_change_password


async def test_database_objects_match_the_model(db_session: AsyncSession) -> None:
    index_def = await db_session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_users_single_anonymous'")
    )
    triggers = set(
        (
            await db_session.execute(
                text("SELECT tgname FROM pg_trigger WHERE tgfoid = 'reject_anonymous_principal'::regproc")
            )
        ).scalars()
    )
    model_names = {c.name for c in User.__table__.constraints} | {i.name for i in User.__table__.indexes}

    assert index_def is not None and "WHERE is_anonymous" in index_def
    assert triggers == {"trg_members_reject_anonymous", "trg_user_group_members_reject_anonymous"}
    assert {"uq_users_single_anonymous", "ck_users_anonymous_inert"} <= model_names


async def test_second_anonymous_row_is_rejected(db_session: AsyncSession, anonymous: User) -> None:
    db_session.add(
        User(
            login="second-anonymous",
            email="second-anonymous@example.com",
            display_name="Second anonymous",
            status="deactivated",
            password_hash=None,
            is_anonymous=True,
        )
    )
    with pytest.raises(IntegrityError, match="uq_users_single_anonymous"):
        await db_session.flush()


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'active'",
        "is_admin = true",
        "password_hash = 'not-a-real-hash'",
        "is_service_account = true",
        "must_change_password = true",
    ],
)
async def test_inert_check_rejects_giving_it_any_capability(
    db_session: AsyncSession, anonymous: User, assignment: str
) -> None:
    with pytest.raises(IntegrityError, match="ck_users_anonymous_inert"):
        await db_session.execute(text(f"UPDATE users SET {assignment} WHERE is_anonymous"))


async def test_trigger_rejects_adding_it_to_a_project(
    db_session: AsyncSession, anonymous: User, project: Project
) -> None:
    db_session.add(Member(project_id=project.id, user_id=anonymous.id))
    with pytest.raises(IntegrityError, match="anonymous user cannot be added to members"):
        await db_session.flush()


async def test_trigger_rejects_moving_a_membership_onto_it(
    db_session: AsyncSession, anonymous: User, project: Project
) -> None:
    owner = UserFactory.build(login="anonp_member_owner", status="active")
    db_session.add(owner)
    await db_session.flush()
    member = Member(project_id=project.id, user_id=owner.id)
    db_session.add(member)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="anonymous user cannot be added to members"):
        await db_session.execute(update(Member).where(Member.id == member.id).values(user_id=anonymous.id))


async def test_trigger_rejects_adding_it_to_a_group(db_session: AsyncSession, anonymous: User) -> None:
    group = UserGroup(name="anonp-trigger-group")
    db_session.add(group)
    await db_session.flush()

    db_session.add(UserGroupMember(group_id=group.id, user_id=anonymous.id))
    with pytest.raises(IntegrityError, match="anonymous user cannot be added to user_group_members"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Accessor
# ---------------------------------------------------------------------------


async def test_accessor_caches_only_the_id(db_session: AsyncSession, anonymous: User) -> None:
    assert anonymous_user_service._anonymous_user_id == anonymous.id
    assert await get_anonymous_user(db_session) is anonymous


async def test_accessor_returns_none_and_warns_when_the_row_is_missing(
    db_session: AsyncSession, anonymous: User, caplog: pytest.LogCaptureFixture
) -> None:
    await db_session.execute(text("DELETE FROM users WHERE is_anonymous"))
    db_session.expunge_all()

    with caplog.at_level(logging.WARNING, logger="specivo.services.anonymous_user_service"):
        assert await get_anonymous_user(db_session) is None

    assert "anonymous user row is missing" in caplog.text
    assert anonymous_user_service._anonymous_user_id is None


# ---------------------------------------------------------------------------
# Nothing authenticates as it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("login", ["$anonymous", "anonymous@specivo.invalid"])
async def test_password_login_is_refused(client: AsyncClient, anonymous: User, login: str) -> None:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": "any-password-at-all"})

    assert resp.status_code == 401
    assert _error_code(resp) == "auth_invalid_credentials"


async def test_jwt_minted_for_its_id_is_rejected(client: AsyncClient, anonymous: User) -> None:
    resp = await client.get("/api/v1/my/api-keys/", headers=_bearer(anonymous))

    assert resp.status_code == 401
    assert _error_code(resp) == "auth_token_invalid"


async def test_refresh_token_for_it_is_refused(db_session: AsyncSession, anonymous: User) -> None:
    auth = AuthService()
    raw = _make_refresh_token()
    await auth._store_refresh_token(db_session, anonymous.id, raw, None, None, get_settings())
    await db_session.flush()

    with pytest.raises(AppError) as exc:
        await auth.refresh(db_session, raw)

    assert exc.value.status_code == 401


async def test_api_key_authentication_is_refused(
    client: AsyncClient, db_session: AsyncSession, anonymous: User
) -> None:
    raw = "spv_planted-key-for-the-anonymous-user"
    db_session.add(
        ApiKey(user_id=anonymous.id, name="planted", key_prefix=raw[:12], key_hash=_hash_key(raw), is_active=True)
    )
    await db_session.commit()

    resp = await client.get("/api/v1/my/api-keys/", headers={"Authorization": f"Bearer {raw}"})

    assert resp.status_code == 401
    assert _error_code(resp) == "api_key_invalid"


async def test_api_key_creation_is_refused(db_session: AsyncSession, anonymous: User) -> None:
    with pytest.raises(AnonymousUserProtectedError):
        await ApiKeyService().create_key(db_session, anonymous.id, "not-allowed")

    count = await db_session.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == anonymous.id))
    assert count == 0


async def test_forgot_password_issues_no_token(db_session: AsyncSession, anonymous: User) -> None:
    assert await AuthService().request_password_reset(db_session, "anonymous@specivo.invalid") is None

    count = await db_session.scalar(
        select(func.count()).select_from(PasswordResetToken).where(PasswordResetToken.user_id == anonymous.id)
    )
    assert count == 0


async def test_reset_token_cannot_set_its_password(db_session: AsyncSession, anonymous: User) -> None:
    raw = "planted-reset-token-for-the-anonymous-user"
    db_session.add(
        PasswordResetToken(
            user_id=anonymous.id,
            token_hash=_hash_token(raw),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    await db_session.flush()

    with pytest.raises(AppError) as exc:
        await AuthService().reset_password_with_token(db_session, raw, "a-brand-new-password")

    assert exc.value.code == "password_reset_invalid"


# ---------------------------------------------------------------------------
# Admin cannot change it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["unlock", "lock"])
async def test_admin_cannot_activate_or_lock_it(client: AsyncClient, admin: User, anonymous: User, action: str) -> None:
    resp = await client.post(f"/api/v1/admin/users/{anonymous.id}/{action}/", headers=_bearer(admin))

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


async def test_admin_cannot_set_its_password(client: AsyncClient, admin: User, anonymous: User) -> None:
    resp = await client.post(
        f"/api/v1/admin/users/{anonymous.id}/reset-password/",
        json={"password": "a-long-enough-password"},
        headers=_bearer(admin),
    )

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


async def test_admin_cannot_create_an_api_key_for_it(client: AsyncClient, admin: User, anonymous: User) -> None:
    resp = await client.post(
        f"/api/v1/admin/users/{anonymous.id}/api-keys/", json={"name": "not-allowed"}, headers=_bearer(admin)
    )

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


async def test_no_endpoint_edits_or_deletes_a_user(client: AsyncClient, admin: User, anonymous: User) -> None:
    """Promotion happens only through the CLI, and users are never deleted over the API."""
    for method in ("DELETE", "PATCH", "PUT"):
        resp = await client.request(method, f"/api/v1/admin/users/{anonymous.id}/", headers=_bearer(admin))
        assert resp.status_code in (404, 405), method


async def test_cli_cannot_promote_it(db_session: AsyncSession, anonymous: User) -> None:
    with pytest.raises(SystemExit):
        await _create_admin(db_session, "$anonymous", "promote@example.com", "password-long-enough")

    await db_session.refresh(anonymous)
    assert not anonymous.is_admin


async def test_cli_cannot_set_its_password(db_session: AsyncSession, anonymous: User) -> None:
    with pytest.raises(SystemExit):
        await _reset_password(db_session, "$anonymous", "password-long-enough")


async def test_admin_cannot_add_it_to_a_group(
    client: AsyncClient, db_session: AsyncSession, admin: User, anonymous: User
) -> None:
    group = UserGroup(name="anonp-api-group")
    db_session.add(group)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/groups/{group.id}/users/", json={"user_id": anonymous.id}, headers=_bearer(admin)
    )

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


async def test_admin_cannot_add_it_to_a_project(
    client: AsyncClient, db_session: AsyncSession, admin: User, anonymous: User, project: Project
) -> None:
    role = Role(
        name="AnonpRole",
        position=1,
        assignable=True,
        builtin=0,
        permissions=["view_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.key}/members/",
        json={"user_id": anonymous.id, "role_ids": [role.id]},
        headers=_bearer(admin),
    )

    assert resp.status_code == 403
    assert _error_code(resp) == "anonymous_user_protected"


@pytest.mark.parametrize("login", ["$anonymous", "$visitor"])
async def test_reserved_logins_are_rejected_at_user_creation(client: AsyncClient, admin: User, login: str) -> None:
    resp = await client.post(
        "/api/v1/admin/users/",
        json={
            "login": login,
            "email": "reserved-login@example.com",
            "display_name": "Reserved",
            "password": "a-long-enough-password",
        },
        headers=_bearer(admin),
    )

    assert resp.status_code == 422
    assert any(err.get("field") == "login" for err in resp.json()["errors"])


# ---------------------------------------------------------------------------
# Absent from listings and counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{"q": "anonymous"}, {"q": "$anon"}, {"status": "deactivated", "limit": 200}])
async def test_absent_from_admin_user_list_api(client: AsyncClient, admin: User, anonymous: User, params: dict) -> None:
    resp = await client.get("/api/v1/admin/users/", params=params, headers=_bearer(admin))

    assert resp.status_code == 200
    assert anonymous.id not in {u["id"] for u in resp.json()}


async def test_absent_from_admin_users_page(client: AsyncClient, admin: User, anonymous: User) -> None:
    resp = await client.get("/admin/users/", headers=_bearer(admin))

    assert resp.status_code == 200
    assert "$anonymous" not in resp.text
    assert "anonymous@specivo.invalid" not in resp.text


async def test_has_no_admin_user_page(client: AsyncClient, admin: User, anonymous: User) -> None:
    resp = await client.get(f"/admin/users/{anonymous.id}/", headers=_bearer(admin))

    assert resp.status_code == 404


@pytest.mark.parametrize("q", ["anonymous", "Anonymous", "$anon"])
async def test_absent_from_user_autocomplete(
    client: AsyncClient, db_session: AsyncSession, anonymous: User, q: str
) -> None:
    viewer = UserFactory.build(login="anonp_viewer", status="active")
    db_session.add(viewer)
    await db_session.commit()

    resp = await client.get("/api/v1/users/autocomplete/", params={"q": q}, headers=_bearer(viewer))

    assert resp.status_code == 200
    assert anonymous.id not in {u["id"] for u in resp.json()}


async def test_not_counted_in_admin_dashboard_stats(db_session: AsyncSession, anonymous: User) -> None:
    stats = await SettingsService().get_dashboard_stats(db_session)
    all_rows = await db_session.scalar(select(func.count()).select_from(User))

    assert stats["total_users"] == all_rows - 1


async def test_importer_never_maps_a_source_account_onto_it(db_session: AsyncSession, anonymous: User) -> None:
    assert await _find_user_by_login(db_session, "$anonymous") is None
