"""Importing through a plugin source, end to end.

Runs the guide's example plugin (``tests/fixtures/acme_import.py``) through
the generic import command's own parsing and pipeline construction, against
the guide's sample export, so what the guide promises is what these check:
the rows, the report, passwords that verify at sign-in through the plugin
hasher, and resuming.

Also covers the base class's promise that an adapter can override a single
stream, and that two sources sharing an instance name keep separate
identifier maps.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

import specivo.importers.core.source as source_mod
import specivo.services.password_hashers as hashers_mod
from specivo.cli.import_data import build_pipeline, parse_args
from specivo.core.config import get_settings
from specivo.core.plugin_manager import bootstrap
from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import EntityType, IRUser
from specivo.importers.core.pipeline import ImportSummary, PhaseFailedError
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.core.source import BaseSourceAdapter, get_source_registry
from specivo.importers.load.user_loader import NOTE_PASSWORD_KEPT, NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN
from specivo.models.import_id_map import ImportIdMapping
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.auth_utils import verify_password
from tests.services.test_import_pipeline_run import SingleSessionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

EXPORT = Path(__file__).resolve().parents[1] / "fixtures" / "acme_export.json"
# The password behind the example export's md5 digest (see the hasher guide).
PASSWORD = "open sesame"


class SoloUsers(BaseSourceAdapter):
    """A source with nothing but users: every other stream is the base class's."""

    source_system = "solo"

    async def extract_users(self) -> AsyncIterator[IRUser]:
        yield IRUser(source_ref="1", login="solo_one", display_name="Solo One", email="solo_one@example.com")
        yield IRUser(source_ref="2", login="solo_two", display_name="Solo Two", email="solo_two@example.com")


class SharedInstanceUsers(SoloUsers):
    """Same source refs and instance name as the example plugin, different system."""

    source_system = "acmetwin"


@pytest.fixture(autouse=True)
def _plugins(monkeypatch):
    """Install the example source and hasher plugins, as INSTALLED_PLUGINS would."""
    monkeypatch.setattr(source_mod, "_registry", source_mod._registry)
    monkeypatch.setattr(hashers_mod, "_registry", hashers_mod._registry)
    settings = get_settings()
    monkeypatch.setattr(settings, "legacy_password_algorithms", ["acme_md5"])
    bootstrap(
        settings.model_copy(
            update={"installed_plugins": ["tests.fixtures.acme_hashers.Plugin", "tests.fixtures.acme_import.Plugin"]}
        )
    )


async def _import(db_session, *argv: str, source: str = "acme") -> ImportSummary:
    """Run the import as the command would, on the test's session."""
    extra = ["--export", str(EXPORT)] if source == "acme" else []
    args, adapter_cls = parse_args(
        ["--source", source, *extra, "--merge-duplicate-statuses", *argv], get_source_registry()
    )
    adapter = adapter_cls.from_args(args)
    return await build_pipeline(args, adapter, SingleSessionFactory(db_session), NullProgressReporter()).run()


async def _count(db_session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for clause in where:
        stmt = stmt.where(clause)
    return (await db_session.execute(stmt)).scalar_one()


async def _user(db_session, login: str) -> User:
    return (await db_session.execute(select(User).where(User.login == login))).scalar_one()


class TestPluginImport:
    async def test_the_export_is_imported(self, db_session):
        summary = await _import(db_session)

        assert summary.source_system == "acme"
        assert summary.created[EntityType.USER] == 2
        assert summary.created[EntityType.PROJECT] == 1
        assert summary.created[EntityType.ISSUE] == 3

        project = (await db_session.execute(select(Project).where(Project.identifier == "web"))).scalar_one()
        subjects = (await db_session.execute(select(Issue.subject).where(Issue.project_id == project.id))).scalars()
        assert set(subjects) == {"Broken link on the pricing page", "Add a dark theme", "Update the footer year"}

    async def test_issues_keep_their_authors_and_statuses(self, db_session):
        await _import(db_session)

        issue = (await db_session.execute(select(Issue).where(Issue.subject == "Update the footer year"))).scalar_one()
        assert issue.author_id == (await _user(db_session, "dlee")).id
        status = await db_session.get(IssueStatus, issue.status_id)
        assert status.name == "Done"

    async def test_markdown_is_kept_as_it_is(self, db_session):
        """The base class converts nothing: Textile rules must not touch Markdown."""
        await _import(db_session)

        description = (
            await db_session.execute(
                select(Issue.description).where(Issue.subject == "Broken link on the pricing page")
            )
        ).scalar_one()
        assert description == "The **Contact sales** button points at a 404."

    async def test_the_report_names_the_source_and_is_json(self, db_session):
        summary = await _import(db_session)

        text = summary.format_text()
        assert "IMPORT — acme @ acme" in text
        assert json.loads(json.dumps(summary.as_dict()))["source_system"] == "acme"
        assert set(summary.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN]) == {"dlee", "spark"}

    async def test_a_dry_run_writes_nothing(self, db_session):
        summary = await _import(db_session, "--dry-run")

        assert summary.created[EntityType.ISSUE] == 3
        await db_session.rollback()
        assert await _count(db_session, User, User.login.in_(["dlee", "spark"])) == 0
        assert await _count(db_session, ImportIdMapping, ImportIdMapping.source_system == "acme") == 0

    async def test_a_second_run_creates_nothing(self, db_session):
        await _import(db_session)
        summary = await _import(db_session)

        assert summary.created[EntityType.ISSUE] == 0
        assert summary.skipped[EntityType.ISSUE] == 3
        assert summary.skipped[EntityType.USER] == 2


class TestPluginPasswords:
    async def test_the_plugin_hasher_wraps_the_source_digest(self, db_session):
        summary = await _import(db_session, "--import-passwords")

        user = await _user(db_session, "dlee")
        assert user.password_hash.startswith("acme_md5$")
        assert user.must_change_password is False
        assert verify_password(PASSWORD, user.password_hash)
        assert summary.notes[NOTE_PASSWORD_KEPT] == ["dlee"]
        assert summary.notes[NOTE_PASSWORD_SET_AT_FIRST_SIGN_IN] == ["spark"]

    async def test_sign_in_verifies_and_upgrades_to_bcrypt(self, client, db_session):
        await _import(db_session, "--import-passwords")

        resp = await client.post("/api/v1/auth/login/", json={"login": "dlee", "password": PASSWORD})
        assert resp.status_code == 200, resp.text

        user = await _user(db_session, "dlee")
        await db_session.refresh(user)
        assert user.password_hash.startswith("$2b$")
        assert verify_password(PASSWORD, user.password_hash)

    async def test_without_the_flag_every_account_resets(self, db_session):
        await _import(db_session)

        user = await _user(db_session, "dlee")
        assert user.password_hash.startswith("$2b$")
        assert not verify_password(PASSWORD, user.password_hash)
        assert user.must_change_password is True

    async def test_a_disabled_algorithm_stops_the_import_before_it_writes(self, db_session, monkeypatch):
        monkeypatch.setattr(get_settings(), "legacy_password_algorithms", [])

        with pytest.raises(PhaseFailedError, match="'acme_md5' algorithm is not enabled"):
            await _import(db_session, "--import-passwords")
        assert await _count(db_session, User, User.login.in_(["dlee", "spark", "acme-import"])) == 0


class TestUsersOnlySource:
    @pytest.fixture(autouse=True)
    def _register(self, _plugins):
        source_mod.install_source_registry(source_mod.build_source_registry([("solo", [SoloUsers])]))

    async def test_it_imports_cleanly(self, db_session):
        """--strict turns any warning into a failure, so this proves the empty phases are quiet."""
        summary = await _import(db_session, "--strict", source="solo")

        assert summary.created[EntityType.USER] == 2
        assert summary.warnings == []
        assert len(summary.phases_run) == 19
        for entity in (EntityType.PROJECT, EntityType.ISSUE, EntityType.WIKI_PAGE, EntityType.STATUS):
            assert summary.created[entity] == 0
        assert "solo_one" in summary.format_text()

    async def test_it_resumes(self, db_session):
        first = await _import(db_session, "--stop-after-phase", "users", source="solo")
        assert first.phases_run[-1] == "users"
        assert await _count(db_session, User, User.login.in_(["solo_one", "solo_two"])) == 2

        second = await _import(db_session, "--resume", str(first.run_id), source="solo")

        assert second.run_id == first.run_id
        assert second.created[EntityType.USER] == 0
        assert second.skipped[EntityType.USER] == 2
        assert second.phases_run[-1] == "search_backfill"
        assert await _count(db_session, User, User.login.in_(["solo_one", "solo_two"])) == 2


class TestIdMapScoping:
    async def test_two_sources_with_one_instance_name_do_not_share_rows(self, db_session):
        source_mod.install_source_registry(
            source_mod.build_source_registry(
                [("acme-import", [get_source_registry().get("acme")]), ("twin", [SharedInstanceUsers])]
            )
        )

        await _import(db_session, "--source-instance", "shared")
        twin = await _import(db_session, "--source-instance", "shared", source="acmetwin")

        # Same source refs ("1", "2") and instance, but nothing is recognised as imported.
        assert twin.skipped[EntityType.USER] == 0
        assert twin.created[EntityType.USER] == 2

        rows = (
            await db_session.execute(
                select(ImportIdMapping.source_system, ImportIdMapping.target_id).where(
                    ImportIdMapping.source_instance == "shared",
                    ImportIdMapping.entity_type == EntityType.USER,
                    ImportIdMapping.source_id == "1",
                )
            )
        ).all()
        by_system = dict(rows)
        assert set(by_system) == {"acme", "acmetwin"}
        assert by_system["acme"] != by_system["acmetwin"]

    async def test_the_map_reads_only_its_own_system(self, db_session):
        writer = ImportIdMap("acme", "shared", run_id=uuid.uuid4())
        user = await _user_row(db_session)
        await writer.put(db_session, EntityType.USER, "1", "users", user.id)

        assert await ImportIdMap("acmetwin", "shared", writer.run_id).get(db_session, EntityType.USER, "1") is None
        assert await ImportIdMap("acme", "shared", writer.run_id).get(db_session, EntityType.USER, "1") == user.id


async def _user_row(db_session) -> User:
    from tests.factories.user import UserFactory

    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    return user
