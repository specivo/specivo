"""A bundle that carries passwords hashed by a plugin's algorithm, end to end.

Runs the guide's example hasher plugin (``tests/fixtures/acme_hashers.py``)
through the import command's own parsing and pipeline construction: the
credential in the bundle is wrapped by the plugin hasher, verifies at sign-in
and is upgraded to bcrypt there; without the opt-in every account resets, and a
disabled algorithm stops the import before anything is written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

import specivo.services.password_hashers as hashers_mod
from specivo.cli.import_data import build_pipeline, parse_args
from specivo.core.config import get_settings
from specivo.core.plugin_manager import bootstrap
from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import ImportSummary, PhaseFailedError
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.core.source import build_source_registry
from specivo.importers.load.user_loader import NOTE_PASSWORD_KEPT, NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN
from specivo.models.user import User
from specivo.services.auth_utils import verify_password
from tests.fixtures.import_bundle import manifest, user, write_bundle
from tests.services.test_import_pipeline_run import SingleSessionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

# The password behind the example digest (see the hasher guide).
PASSWORD = "open sesame"
SYSTEM = "hashed"
LOGINS = ["dlee", "spark"]


@pytest.fixture(autouse=True)
def _plugin_hasher(monkeypatch):
    """Install the example hasher plugin, as INSTALLED_PLUGINS would."""
    monkeypatch.setattr(hashers_mod, "_registry", hashers_mod._registry)
    settings = get_settings()
    monkeypatch.setattr(settings, "legacy_password_algorithms", ["acme_md5"])
    bootstrap(settings.model_copy(update={"installed_plugins": ["tests.fixtures.acme_hashers.Plugin"]}))


@pytest.fixture
def bundle(tmp_path) -> Path:
    keeper = user(
        "1",
        login="dlee",
        email="dana@example.com",
        credential={
            "algorithm": "acme_md5",
            "fields": {"salt": "NaCl", "password_digest": "ce48c13da732f51a1bf9c4fac876c638"},
        },
    )
    return write_bundle(
        tmp_path / "bundle",
        manifest_data=manifest(source={"system": SYSTEM, "instance": "tracker.example.com"}),
        users=[keeper, user("2", login="spark", email="sam@example.com")],
    )


async def _import(db_session, bundle: Path, *argv: str) -> ImportSummary:
    args, adapter_cls = parse_args(
        ["--source", "bundle", "--path", str(bundle), "--links-file", str(bundle.parent / "links.sqlite"), *argv],
        build_source_registry(),
    )
    adapter = adapter_cls.from_args(args)
    return await build_pipeline(args, adapter, SingleSessionFactory(db_session), NullProgressReporter()).run()


async def _user(db_session, login: str) -> User:
    return (await db_session.execute(select(User).where(User.login == login))).scalar_one()


async def test_the_plugin_hasher_wraps_the_bundles_digest(db_session, bundle):
    summary = await _import(db_session, bundle, "--import-passwords")

    dlee = await _user(db_session, "dlee")
    assert summary.created[EntityType.USER] == 2
    assert dlee.password_hash.startswith("acme_md5$")
    assert dlee.must_change_password is False
    assert verify_password(PASSWORD, dlee.password_hash)
    assert summary.notes[NOTE_PASSWORD_KEPT] == ["dlee"]
    assert summary.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN] == ["spark"]


async def test_sign_in_verifies_and_upgrades_to_bcrypt(client, db_session, bundle):
    await _import(db_session, bundle, "--import-passwords")

    resp = await client.post("/api/v1/auth/login/", json={"login": "dlee", "password": PASSWORD})
    assert resp.status_code == 200, resp.text

    dlee = await _user(db_session, "dlee")
    await db_session.refresh(dlee)
    assert dlee.password_hash.startswith("$2b$")
    assert verify_password(PASSWORD, dlee.password_hash)


async def test_without_the_flag_every_account_resets(db_session, bundle):
    await _import(db_session, bundle)

    dlee = await _user(db_session, "dlee")
    assert dlee.password_hash.startswith("$2b$")
    assert not verify_password(PASSWORD, dlee.password_hash)
    assert dlee.must_change_password is True


async def test_a_disabled_algorithm_stops_the_import_before_it_writes(db_session, bundle, monkeypatch):
    monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])

    with pytest.raises(PhaseFailedError, match="'acme_md5' algorithm is not enabled"):
        await _import(db_session, bundle, "--import-passwords")

    count = (
        await db_session.execute(select(func.count()).select_from(User).where(User.login.in_(LOGINS)))
    ).scalar_one()
    assert count == 0
