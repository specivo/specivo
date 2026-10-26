"""Importing Redmine accounts with their passwords, against the real fixture.

The fixture's accounts were created through Redmine's own models with the
password ``fixture-password-not-a-secret``, so these tests check the hasher
against Redmine's actual formula, not our reading of it: an imported account
signs in through the API with that password, and the sign-in upgrades the hash.

The users phase is run on its own, against the live adapter: it is the only
phase that touches passwords, and it does not depend on what the others write.

Requires the fixture (``make redmine-fixture-up && make redmine-fixture-seed``).
Serial for the same reason as the full import: it writes instance-wide rows.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import specivo.services.password_hashers as hashers_mod
from specivo.core.config import get_settings
from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportSummary, PhaseContext, PhaseFailedError
from specivo.importers.core.progress import NullProgressReporter
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

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.redmine,
    pytest.mark.serial,
    pytest.mark.slow,
]

FIXTURE_PASSWORD = "fixture-password-not-a-secret"


@pytest.fixture(autouse=True)
def _enable_redmine_hashes(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "legacy_password_algorithms", ["redmine_sha1"])
    monkeypatch.setattr(hashers_mod, "_registry", None)


async def _import_users(db_session, adapter, **option_kwargs) -> ImportSummary:
    """Run the users phase against the live fixture inside the test transaction."""
    options = ImportOptions(source_instance=adapter.source_instance, **option_kwargs)
    run_id = uuid.uuid4()
    summary = ImportSummary(
        run_id=run_id, source_system=adapter.source_system, source_instance=adapter.source_instance, dry_run=False
    )
    ctx = PhaseContext(
        phase=ImportPhase.USERS,
        session=db_session,
        adapter=adapter,
        options=options,
        summary=summary,
        reporter=NullProgressReporter(),
        id_map=ImportIdMap(adapter.source_system, adapter.source_instance, run_id),
    )
    check_credential_import(ctx)
    await load_users(ctx)
    await db_session.commit()
    return summary


async def _user(db_session, login: str) -> User:
    return (await db_session.execute(select(User).where(User.login == login))).scalar_one()


class TestWithPasswords:
    @pytest.fixture
    async def imported(self, db_session, redmine_adapter):
        return await _import_users(db_session, redmine_adapter, import_passwords=True)

    async def test_a_local_account_keeps_its_password_wrapped(self, db_session, imported):
        user = await _user(db_session, "fixture_dev")
        assert user.password_hash.startswith("redmine_sha1$")
        assert len(user.password_hash) == 106
        assert user.must_change_password is False
        assert verify_password(FIXTURE_PASSWORD, user.password_hash)
        assert not verify_password(FIXTURE_PASSWORD + "x", user.password_hash)

    async def test_the_account_signs_in_through_the_api_and_is_upgraded(self, client, db_session, imported):
        resp = await client.post("/api/v1/auth/login/", json={"login": "fixture_dev", "password": FIXTURE_PASSWORD})
        assert resp.status_code == 200, resp.text

        user = await _user(db_session, "fixture_dev")
        await db_session.refresh(user)
        assert user.password_hash.startswith("$2b$")
        assert verify_password(FIXTURE_PASSWORD, user.password_hash)

    async def test_redmines_own_change_flag_is_mirrored(self, db_session, imported):
        assert (await _user(db_session, "fixture_new")).must_change_password is True
        assert (await _user(db_session, "fixture_thai")).must_change_password is False

    async def test_an_ldap_account_takes_the_reset_path(self, db_session, imported):
        user = await _user(db_session, "fixture_ldap")
        assert user.password_hash.startswith("$2b$")
        assert user.must_change_password is True
        assert "fixture_ldap" in imported.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN]
        assert imported.notes[NOTE_EXTERNAL_AUTH] == ["fixture_ldap"]

    async def test_administrators_never_keep_their_redmine_password(self, db_session, imported):
        for login in ("fixture_admin", "admin"):
            user = await _user(db_session, login)
            assert not verify_password(FIXTURE_PASSWORD, user.password_hash), login
            assert user.must_change_password is True, login
            assert login in imported.notes[NOTE_ADMIN_PASSWORD_RESET]

    async def test_the_report_lists_who_kept_a_password(self, imported):
        kept = set(imported.notes[NOTE_PASSWORD_KEPT])
        assert {"fixture_dev", "fixture_thai", "fixture_locked", "fixture_new"} <= kept
        assert not kept & {"fixture_admin", "admin", "fixture_ldap"}


class TestWithoutPasswords:
    async def test_without_the_flag_every_account_resets_despite_the_allowlist(self, db_session, redmine_adapter):
        summary = await _import_users(db_session, redmine_adapter)

        user = await _user(db_session, "fixture_dev")
        assert user.password_hash.startswith("$2b$")
        assert not verify_password(FIXTURE_PASSWORD, user.password_hash)
        assert user.must_change_password is True
        assert NOTE_PASSWORD_KEPT not in summary.notes

    async def test_a_disabled_algorithm_stops_the_whole_import_before_it_writes(
        self, db_session, build_import, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])
        with pytest.raises(PhaseFailedError, match="LEGACY_PASSWORD_ALGORITHMS"):
            await build_import(merge_duplicate_statuses=True, import_passwords=True).run()

        found = (await db_session.execute(select(User).where(User.login == "fixture_dev"))).scalar_one_or_none()
        assert found is None
