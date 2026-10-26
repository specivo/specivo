"""Service tests for carrying source passwords over at import.

Opt-in twice over: ``--import-passwords`` (``ImportOptions.import_passwords``)
and the algorithm listed in ``LEGACY_PASSWORD_ALGORITHMS``. Without both, or for
an administrator, or for an account with no local credential, the account takes
the reset path exactly as before.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import specivo.services.password_hashers as hashers_mod
from specivo.core.config import get_settings
from specivo.importers.core.ir import EntityType, IRCredential, IRUser
from specivo.importers.core.pipeline import ImportPhase, PhaseFailedError
from specivo.importers.load.registry import _bootstrap
from specivo.importers.load.user_loader import (
    NOTE_ADMIN_PASSWORD_RESET,
    NOTE_EXTERNAL_AUTH,
    NOTE_PASSWORD_KEPT,
    NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN,
    check_credential_import,
    load_users,
)
from specivo.models.user import User
from specivo.services.auth_utils import verify_password
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

SALT = "9a8b7c6d5e4f30211203f4e5d6c7b8a9"
PASSWORD = "imported-passphrase"
DIGEST = hashlib.sha1((SALT + hashlib.sha1(PASSWORD.encode()).hexdigest()).encode()).hexdigest()
CHANGED_ON = datetime(2025, 2, 3, 4, 5, tzinfo=UTC)

# The import service account's login comes from the source system; unique to
# this module so parallel workers never insert the same one.
SOURCE_SYSTEM = "pwimport"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "legacy_password_algorithms", ["redmine_sha1"])
    monkeypatch.setattr(settings, "legacy_password_wrap_rounds", 4)
    monkeypatch.setattr(hashers_mod, "_registry", None)


def _credential(**fields) -> IRCredential:
    return IRCredential("redmine_sha1", fields or {"salt": SALT, "hashed_password": DIGEST})


def _user(source_ref: str, login: str, **overrides) -> IRUser:
    data = {
        "source_ref": source_ref,
        "login": login,
        "display_name": login.title(),
        "email": f"{login}@example.org",
        "credential": _credential(),
        "must_change_password": False,
        "password_changed_at": CHANGED_ON,
    }
    data.update(overrides)
    return IRUser(**data)


def _adapter(*users: IRUser) -> FakeAdapter:
    return FakeAdapter(users=list(users), source_system=SOURCE_SYSTEM, credential_algorithms=("redmine_sha1",))


async def _imported(db_session, ctx, source_ref: str) -> User:
    user_id = await ctx.id_map.get(db_session, EntityType.USER, source_ref)
    return await db_session.get(User, user_id)


class TestPasswordKept:
    async def test_the_wrapped_hash_is_stored_and_verifies(self, db_session, make_context):
        ctx = make_context(_adapter(_user("1", "pw_keep")), import_passwords=True)
        await load_users(ctx)

        user = await _imported(db_session, ctx, "1")
        assert user.password_hash.startswith(f"redmine_sha1${SALT}$$2b$")
        assert DIGEST not in user.password_hash
        assert verify_password(PASSWORD, user.password_hash)
        assert not verify_password("wrong", user.password_hash)

    async def test_the_source_flags_are_mirrored(self, db_session, make_context):
        ctx = make_context(
            _adapter(_user("1", "pw_flag_off"), _user("2", "pw_flag_on", must_change_password=True)),
            import_passwords=True,
        )
        await load_users(ctx)

        off, on = await _imported(db_session, ctx, "1"), await _imported(db_session, ctx, "2")
        assert off.must_change_password is False
        assert on.must_change_password is True
        assert off.password_changed_at == CHANGED_ON

    async def test_the_report_lists_who_kept_their_password(self, make_context):
        ctx = make_context(_adapter(_user("1", "pw_note")), import_passwords=True)
        await load_users(ctx)
        assert ctx.summary.notes[NOTE_PASSWORD_KEPT] == ["pw_note"]
        assert NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN not in ctx.summary.notes


class TestResetPath:
    async def test_without_the_flag_everyone_resets_whatever_the_allowlist(self, db_session, make_context):
        ctx = make_context(_adapter(_user("1", "pw_noflag")))
        await load_users(ctx)

        user = await _imported(db_session, ctx, "1")
        assert user.password_hash.startswith("$2b$")
        assert not verify_password(PASSWORD, user.password_hash)
        assert user.must_change_password is True
        assert user.password_changed_at is None
        assert ctx.summary.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN] == ["pw_noflag"]
        assert NOTE_PASSWORD_KEPT not in ctx.summary.notes

    async def test_an_administrator_always_resets(self, db_session, make_context):
        ctx = make_context(_adapter(_user("1", "pw_admin", is_admin=True)), import_passwords=True)
        await load_users(ctx)

        user = await _imported(db_session, ctx, "1")
        assert not verify_password(PASSWORD, user.password_hash)
        assert user.must_change_password is True
        assert ctx.summary.notes[NOTE_ADMIN_PASSWORD_RESET] == ["pw_admin"]
        assert "pw_admin" in ctx.summary.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN]

    async def test_an_ldap_account_resets_and_is_counted_separately(self, db_session, make_context):
        ctx = make_context(
            _adapter(_user("1", "pw_ldap", credential=None, external_auth=True)),
            import_passwords=True,
        )
        await load_users(ctx)

        user = await _imported(db_session, ctx, "1")
        assert user.must_change_password is True
        assert ctx.summary.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN] == ["pw_ldap"]
        assert ctx.summary.notes[NOTE_EXTERNAL_AUTH] == ["pw_ldap"]

    async def test_an_account_with_no_credential_resets(self, db_session, make_context):
        ctx = make_context(_adapter(_user("1", "pw_blank", credential=None)), import_passwords=True)
        await load_users(ctx)
        assert (await _imported(db_session, ctx, "1")).must_change_password is True

    async def test_an_unusable_credential_is_reported_without_its_contents(self, db_session, make_context):
        ctx = make_context(
            _adapter(_user("1", "pw_broken", credential=_credential(salt=SALT))),  # no digest
            import_passwords=True,
        )
        await load_users(ctx)

        assert (await _imported(db_session, ctx, "1")).must_change_password is True
        warnings = [w.as_dict() for w in ctx.summary.warnings]
        assert any(w["context"].get("login") == "pw_broken" for w in warnings)
        assert SALT not in repr(warnings)


class TestRefusedUpFront:
    async def test_a_disabled_algorithm_stops_the_run_before_anything_is_written(
        self, db_session, make_context, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])
        ctx = make_context(_adapter(_user("1", "pw_refused")), phase=ImportPhase.BOOTSTRAP, import_passwords=True)

        with pytest.raises(PhaseFailedError, match="LEGACY_PASSWORD_ALGORITHMS"):
            await _bootstrap(ctx)

        # Not even the import service account was created.
        login = f"{SOURCE_SYSTEM}-import"
        assert (await db_session.execute(select(User).where(User.login == login))).scalar_one_or_none() is None

    async def test_an_unregistered_algorithm_is_refused(self, make_context):
        adapter = FakeAdapter(source_system=SOURCE_SYSTEM, credential_algorithms=("acme_md5",))
        ctx = make_context(adapter, import_passwords=True)
        with pytest.raises(PhaseFailedError, match="no password hasher is registered"):
            check_credential_import(ctx)

    async def test_without_the_flag_nothing_is_checked(self, make_context, monkeypatch):
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])
        check_credential_import(make_context(_adapter()))

    async def test_a_credential_the_adapter_did_not_declare_is_still_checked(self, make_context):
        stray = _user("1", "pw_stray", credential=IRCredential("django_pbkdf2_sha256", {"password": "x"}))
        ctx = make_context(_adapter(stray), import_passwords=True)
        with pytest.raises(PhaseFailedError, match="not enabled"):
            await load_users(ctx)

    async def test_a_plugin_algorithm_registered_through_the_setting_is_refused_until_enabled(
        self, make_context, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "password_hashers", ["tests.fixtures.acme_hashers.AcmeTrackerMd5"])
        adapter = FakeAdapter(source_system=SOURCE_SYSTEM, credential_algorithms=("acme_md5",))
        ctx = make_context(adapter, import_passwords=True)
        with pytest.raises(PhaseFailedError, match="not enabled"):
            check_credential_import(ctx)

        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", ["acme_md5"])
        check_credential_import(ctx)
