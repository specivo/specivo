"""Signing in with a password imported from another system.

An imported account's hash is stored under a legacy prefix (``redmine_sha1$``)
and verified by that hasher until the first successful sign-in replaces it with
native bcrypt. These tests go through the real endpoints and pin:

- the upgrade: it happens once, after every account check, inside the sign-in,
  leaves ``password_changed_at`` alone and is audited by algorithm only;
- that nothing else changes: a wrong password, a lockout or a deactivated
  account behave exactly as for a native account, and an upgrade never happens
  on a refused sign-in;
- that a disabled, expired or unrecognised hash is a plain 401 — never a 500,
  and indistinguishable from a wrong password;
- change-password against a legacy current password, and a hasher supplied by a
  plugin through the shared bootstrap.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import specivo.services.password_hashers as hashers_mod
from specivo.core.config import get_settings
from specivo.core.plugin_manager import bootstrap
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.password_hashers import RedmineSHA1WrappedHasher, get_password_hasher_registry
from specivo.services.security_audit_service import AuditEvent
from tests.factories.user import UserFactory

pytestmark = pytest.mark.integration

SALT = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
PASSWORD = "legacy-passphrase-1"
REDMINE_DIGEST = hashlib.sha1((SALT + hashlib.sha1(PASSWORD.encode()).hexdigest()).encode()).hexdigest()
PASSWORD_CHANGED_AT = datetime(2024, 5, 1, 9, 30, tzinfo=UTC)

_LOGIN_URL = "/api/v1/auth/login/"


@pytest.fixture(autouse=True)
def _legacy_settings(monkeypatch):
    """Enable redmine_sha1, wrap cheaply, and isolate the process-wide registry."""
    settings = get_settings()
    monkeypatch.setattr(settings, "legacy_password_algorithms", ["redmine_sha1"])
    monkeypatch.setattr(settings, "legacy_password_wrap_rounds", 4)
    monkeypatch.setattr(settings, "legacy_password_hashes_expire_at", None)
    monkeypatch.setattr(hashers_mod, "_registry", None)


def _redmine_hash() -> str:
    return RedmineSHA1WrappedHasher().from_source({"salt": SALT, "hashed_password": REDMINE_DIGEST})


async def _legacy_user(db: AsyncSession, login: str, password_hash: str | None = None, **kwargs) -> User:
    kwargs.setdefault("status", "active")
    user = UserFactory.build(
        login=login,
        password_hash=password_hash or _redmine_hash(),
        password_changed_at=PASSWORD_CHANGED_AT,
        **kwargs,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(_LOGIN_URL, json={"login": login, "password": password})


async def _reload(db: AsyncSession, user: User) -> User:
    await db.refresh(user)
    return user


async def _upgrade_events(db: AsyncSession, user_id: int) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(
            SecurityAuditLog.event_type == AuditEvent.PASSWORD_HASH_UPGRADED,
            SecurityAuditLog.user_id == user_id,
        )
    )
    return list(rows.scalars().all())


def _error(resp) -> tuple[int, object]:
    """The status and body of a refusal, minus anything request-specific."""
    body = resp.json()
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k not in {"request_id"}}
    return resp.status_code, body


class TestUpgradeOnSignIn:
    async def test_right_password_signs_in_and_upgrades_the_hash(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_ok")

        resp = await _login(client, "legacy_ok", PASSWORD)
        assert resp.status_code == 200, resp.text

        user = await _reload(db_session, user)
        assert user.password_hash.startswith(f"$2b${get_settings().bcrypt_rounds:02d}$")
        # The password did not change, only how it is stored.
        assert user.password_changed_at == PASSWORD_CHANGED_AT
        assert user.failed_login_count == 0

    async def test_the_upgrade_is_audited_by_algorithm_only(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_audit")
        old_hash = user.password_hash

        assert (await _login(client, "legacy_audit", PASSWORD)).status_code == 200

        events = await _upgrade_events(db_session, user.id)
        assert len(events) == 1
        assert events[0].details == {"from_algorithm": "redmine_sha1"}
        serialised = repr(events[0].details) + (events[0].user_agent or "")
        assert SALT not in serialised and old_hash not in serialised and REDMINE_DIGEST not in serialised

    async def test_the_second_sign_in_uses_the_new_hash_and_upgrades_nothing(self, client, db_session, monkeypatch):
        user = await _legacy_user(db_session, "legacy_twice")
        assert (await _login(client, "legacy_twice", PASSWORD)).status_code == 200
        upgraded = (await _reload(db_session, user)).password_hash

        # Even with legacy hashes switched off, the account now signs in natively.
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])
        assert (await _login(client, "legacy_twice", PASSWORD)).status_code == 200

        assert (await _reload(db_session, user)).password_hash == upgraded
        assert len(await _upgrade_events(db_session, user.id)) == 1

    async def test_an_outdated_bcrypt_cost_is_upgraded_too(self, client, db_session, monkeypatch):
        """Regression: password_needs_rehash existed but nothing called it."""
        import bcrypt

        old = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        user = await _legacy_user(db_session, "legacy_cost", password_hash=old)

        assert (await _login(client, "legacy_cost", PASSWORD)).status_code == 200

        user = await _reload(db_session, user)
        assert user.password_hash.startswith(f"$2b${get_settings().bcrypt_rounds:02d}$")
        assert [e.details for e in await _upgrade_events(db_session, user.id)] == [{"from_algorithm": "bcrypt"}]


class TestNoUpgradeWhenRefused:
    async def test_wrong_password_is_a_plain_failure(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_wrong")
        old_hash = user.password_hash

        resp = await _login(client, "legacy_wrong", "not-it")
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_invalid_credentials"

        user = await _reload(db_session, user)
        assert user.password_hash == old_hash
        assert user.failed_login_count == 1
        assert await _upgrade_events(db_session, user.id) == []

    async def test_five_failures_lock_the_account_like_a_native_one(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_lock")
        old_hash = user.password_hash
        for _ in range(5):
            assert (await _login(client, "legacy_lock", "not-it")).status_code == 401

        user = await _reload(db_session, user)
        assert user.failed_login_count == 5
        assert user.locked_until is not None

        # The right password now hits the lockout, and nothing is upgraded.
        resp = await _login(client, "legacy_lock", PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_locked"
        assert (await _reload(db_session, user)).password_hash == old_hash
        assert await _upgrade_events(db_session, user.id) == []

    async def test_a_deactivated_account_is_not_upgraded(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_gone", status="deactivated")
        old_hash = user.password_hash

        resp = await _login(client, "legacy_gone", PASSWORD)
        assert resp.status_code == 401
        assert (await _reload(db_session, user)).password_hash == old_hash

    async def test_an_admin_locked_account_is_not_upgraded(self, client, db_session):
        """status=locked is checked after the password; the upgrade comes after that check."""
        user = await _legacy_user(db_session, "legacy_admlock", status="locked")
        old_hash = user.password_hash

        resp = await _login(client, "legacy_admlock", PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_account_locked"
        assert (await _reload(db_session, user)).password_hash == old_hash
        assert await _upgrade_events(db_session, user.id) == []


class TestUnusableHashes:
    async def test_after_the_cutoff_the_right_password_fails_generically(self, client, db_session, monkeypatch):
        await _legacy_user(db_session, "legacy_cut")
        await _legacy_user(db_session, "legacy_cut_wrong")
        monkeypatch.setattr(
            get_settings(), "legacy_password_hashes_expire_at", datetime.now(UTC) - timedelta(seconds=1)
        )

        expired = await _login(client, "legacy_cut", PASSWORD)
        wrong = await _login(client, "legacy_cut_wrong", "not-it")
        assert expired.status_code == 401
        # The cutoff reveals nothing: same response as a wrong password.
        assert _error(expired) == _error(wrong)

    async def test_a_disabled_algorithm_fails_like_a_wrong_password(self, client, db_session, monkeypatch):
        await _legacy_user(db_session, "legacy_off")
        await _legacy_user(db_session, "legacy_off_wrong")
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])

        disabled = await _login(client, "legacy_off", PASSWORD)
        wrong = await _login(client, "legacy_off_wrong", "not-it")
        assert disabled.status_code == 401
        assert _error(disabled) == _error(wrong)

    @pytest.mark.parametrize(
        "stored",
        ["not-a-hash", "$2b$xx$", "unknown_algo$abc$def", "redmine_sha1$salt$garbage", "x" * 255],
    )
    async def test_an_unrecognised_hash_is_a_401_not_a_500(self, client, db_session, stored):
        user = await _legacy_user(db_session, "legacy_junk", password_hash=stored)

        resp = await _login(client, "legacy_junk", PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["code"] == "auth_invalid_credentials"
        assert (await _reload(db_session, user)).failed_login_count == 1

    async def test_a_legacy_account_and_an_unknown_login_answer_alike(self, client, db_session):
        await _legacy_user(db_session, "legacy_enum")
        known = await _login(client, "legacy_enum", "not-it")
        unknown = await _login(client, "nobody_by_this_name", "not-it")
        assert _error(known) == _error(unknown)


class TestChangePassword:
    async def test_a_legacy_current_password_is_accepted(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_change")
        token = (await _login(client, "legacy_change", PASSWORD)).json()["access_token"]
        # Put the legacy hash back, as if this were the account's first contact
        # with the new instance through a session it already held.
        user.password_hash = _redmine_hash()
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/change-password/",
            json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        user = await _reload(db_session, user)
        assert user.password_hash.startswith("$2b$")
        assert user.password_changed_at > PASSWORD_CHANGED_AT
        assert (await _login(client, "legacy_change", "a-brand-new-passphrase")).status_code == 200
        # One upgrade at sign-in, one at the change.
        assert len(await _upgrade_events(db_session, user.id)) == 2

    async def test_a_rejected_change_leaves_the_legacy_hash_alone(self, client, db_session):
        user = await _legacy_user(db_session, "legacy_change_bad")
        token = (await _login(client, "legacy_change_bad", PASSWORD)).json()["access_token"]
        legacy = _redmine_hash()
        user.password_hash = legacy
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/change-password/",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert (await _reload(db_session, user)).password_hash == legacy


class TestPluginHasher:
    async def test_a_plugin_hasher_signs_in_and_upgrades(self, client, db_session, monkeypatch):
        bootstrap(get_settings().model_copy(update={"installed_plugins": ["tests.fixtures.acme_hashers.Plugin"]}))
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", ["acme_md5"])
        digest = hashlib.md5(("NaCl" + PASSWORD).encode()).hexdigest()
        stored = get_password_hasher_registry().get("acme_md5").from_source({"salt": "NaCl", "password_digest": digest})
        user = await _legacy_user(db_session, "legacy_plugin", password_hash=stored)

        assert (await _login(client, "legacy_plugin", PASSWORD)).status_code == 200

        user = await _reload(db_session, user)
        assert user.password_hash.startswith("$2b$")
        assert [e.details for e in await _upgrade_events(db_session, user.id)] == [{"from_algorithm": "acme_md5"}]
