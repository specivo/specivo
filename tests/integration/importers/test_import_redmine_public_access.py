"""Importing Redmine's non-member and anonymous access from the real fixture.

The seed leaves ``login_required`` unset, which is Redmine's default, gives the
subproject a ``GroupAnonymous`` override that reads issues only, gives the
parent a ``GroupNonMember`` override, and adds a private project. Each test
sets ``login_required`` in the fixture itself and removes it afterwards.

Requires the fixture:

    make redmine-fixture-up && make redmine-fixture-seed

Marked ``serial`` for the same reason as the full import tests, and because
these tests write to the shared fixture's ``settings`` table.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from specivo.importers.core.ir import EntityType
from specivo.importers.load.public_access_loader import (
    NOTE_ANONYMOUS_PROJECTS,
    NOTE_ANONYMOUS_SOURCE,
    NOTE_ANONYMOUS_SWITCH,
    NOTE_NON_MEMBER_DIFFERENCES,
    NOTE_PERMISSIONS_NOT_CARRIED,
)
from specivo.importers.redmine import db
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.setting import Setting
from specivo.services.anonymous_access_service import ANONYMOUS_ACCESS_SETTING_KEY, is_anonymous_access_enabled
from specivo.services.security_audit_service import AuditEvent

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.redmine,
    pytest.mark.serial,
    pytest.mark.slow,
]


@pytest_asyncio.fixture
async def login_required(redmine_adapter, redmine_profile):
    """Return a setter for the fixture's ``login_required``; the row is removed afterwards.

    Uses an engine of its own: the pipeline closes the adapter's when a run ends.
    """
    engine = db.create_source_engine(redmine_profile["url"])

    async def _set(value: str | None) -> None:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM settings WHERE name = 'login_required'"))
            if value is not None:
                await conn.execute(
                    text("INSERT INTO settings (name, value, updated_on) VALUES ('login_required', :v, NULL)"),
                    {"v": value},
                )

    try:
        yield _set
    finally:
        await _set(None)
        await engine.dispose()


async def _anonymous_permissions(session) -> dict[str, list[str]]:
    rows = (await session.execute(select(Project.identifier, Project.anonymous_permissions))).all()
    return {identifier: list(perms or []) for identifier, perms in rows}


async def _import_audit_entries(session) -> list[SecurityAuditLog]:
    stmt = select(SecurityAuditLog).where(
        SecurityAuditLog.event_type == AuditEvent.PROJECT_ANONYMOUS_PERMISSIONS_CHANGED
    )
    return list((await session.execute(stmt)).scalars())


class TestSourceRequiresSignIn:
    @pytest.mark.parametrize("value", [None, "1", "true"])
    async def test_nothing_is_opened(self, db_session, build_import, login_required, value):
        """Absent counts as "sign-in required": Redmine stores only non-default
        settings, and guessing permissively would publish a tracker."""
        await login_required(value)

        summary = await build_import(merge_duplicate_statuses=True).run()

        assert all(perms == [] for perms in (await _anonymous_permissions(db_session)).values())
        assert await _import_audit_entries(db_session) == []
        assert summary.created[EntityType.ANONYMOUS_ACCESS] == 0
        assert NOTE_ANONYMOUS_PROJECTS not in summary.notes
        assert "sign-in required" in summary.notes[NOTE_ANONYMOUS_SOURCE][0]
        assert await is_anonymous_access_enabled(db_session) is False


class TestSourceAllowsAnonymous:
    @pytest.fixture
    async def imported(self, db_session, build_import, login_required):
        await login_required("0")
        return await build_import(merge_duplicate_statuses=True).run()

    async def test_public_projects_are_opted_in_as_in_redmine(self, db_session, imported):
        """The parent uses the builtin Anonymous role (issues and wiki); the
        subproject's GroupAnonymous override reads issues only."""
        perms = await _anonymous_permissions(db_session)
        assert perms["acme-app"] == ["view_issues", "view_wiki"]
        assert perms["acme-mobile"] == ["view_issues"]

    async def test_the_instance_switch_stays_off(self, db_session, imported):
        assert await is_anonymous_access_enabled(db_session) is False
        assert "left it off" in imported.notes[NOTE_ANONYMOUS_SWITCH][0]

    async def test_a_private_project_gets_nothing(self, db_session, imported):
        assert (await _anonymous_permissions(db_session))["acme-internal"] == []

    async def test_an_archived_project_gets_nothing(self, db_session, imported):
        """Redmine grants nothing on an archived project, members included."""
        assert (await _anonymous_permissions(db_session))["acme-legacy"] == []

    async def test_the_report_lists_the_projects(self, db_session, imported):
        listed = imported.notes[NOTE_ANONYMOUS_PROJECTS]
        assert sorted(listed) == ["ACMEAPP: view_issues, view_wiki", "ACMEMOBILE: view_issues"]
        assert "login_required = '0'" in imported.notes[NOTE_ANONYMOUS_SOURCE][0]

    async def test_every_opt_in_is_audited(self, db_session, imported):
        entries = await _import_audit_entries(db_session)
        assert sorted(e.details["project_key"] for e in entries) == ["ACMEAPP", "ACMEMOBILE"]
        assert {e.details["reason"] for e in entries} == {"import"}
        assert all(e.details["old"] == [] for e in entries)

    async def test_permissions_that_could_not_be_represented_are_reported(self, db_session, imported):
        lines = imported.notes[NOTE_PERMISSIONS_NOT_CARRIED]
        assert any(line.startswith("Non member role:") and "add_issues" in line for line in lines)
        assert any(line.startswith("Anonymous role:") and "view_documents" in line for line in lines)
        assert any(line.startswith("acme-app: per-project Non member override (Reporter)") for line in lines)
        override = "acme-mobile: per-project Anonymous override (Issue Reader): view_gantt"
        assert override in lines

    async def test_non_member_differences_are_reported_and_the_role_is_unchanged(self, db_session, imported):
        role = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()
        assert role.permissions == ["view_issues"]
        differences = imported.notes[NOTE_NON_MEMBER_DIFFERENCES]
        assert len(differences) == 1
        assert differences[0].startswith("view_wiki: granted by the source's Non member role")

    async def test_no_anonymous_role_is_created(self, db_session, imported):
        """Redmine's builtin=2 role used to be inserted as an assignable role,
        which the database refuses for a builtin role, failing the import."""
        roles = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.ANONYMOUS))).scalars().all()
        assert roles == []


class TestRerun:
    async def test_a_second_import_changes_nothing(self, db_session, build_import, login_required):
        await login_required("0")
        await build_import(merge_duplicate_statuses=True).run()
        before = await _anonymous_permissions(db_session)

        second = await build_import(merge_duplicate_statuses=True).run()

        assert await _anonymous_permissions(db_session) == before
        assert second.created[EntityType.ANONYMOUS_ACCESS] == 0
        assert second.skipped[EntityType.ANONYMOUS_ACCESS] == 2
        assert len(await _import_audit_entries(db_session)) == 2
        assert len(second.notes[NOTE_ANONYMOUS_PROJECTS]) == 2

    async def test_a_second_import_does_not_overrule_an_administrator(self, db_session, build_import, login_required):
        await login_required("0")
        await build_import(merge_duplicate_statuses=True).run()
        project = (await db_session.execute(select(Project).where(Project.identifier == "acme-app"))).scalar_one()
        project.anonymous_permissions = []
        await db_session.flush()

        await build_import(merge_duplicate_statuses=True).run()

        assert (await _anonymous_permissions(db_session))["acme-app"] == []


class TestSwitchAlreadyOn:
    async def test_nothing_is_opted_in(self, db_session, build_import, login_required):
        """Opting in while the switch is on would publish the project at once."""
        await login_required("0")
        db_session.add(Setting(key=ANONYMOUS_ACCESS_SETTING_KEY, value="true"))
        await db_session.flush()

        summary = await build_import(merge_duplicate_statuses=True).run()

        assert all(perms == [] for perms in (await _anonymous_permissions(db_session)).values())
        assert sorted(summary.notes[NOTE_ANONYMOUS_PROJECTS]) == [
            "ACMEAPP: view_issues, view_wiki (not applied: switch on, opt in by hand)",
            "ACMEMOBILE: view_issues (not applied: switch on, opt in by hand)",
        ]
        assert any("Anonymous access is on" in w.message for w in summary.warnings)
