"""Loading roles and public access from IR, without a Redmine fixture.

The fixture-backed tests in ``test_import_redmine_public_access.py`` skip when
the fixture is down, so the load-layer rules that keep an import from opening
an instance, and the builtin-role handling, are pinned here against IR written
by hand.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import EntityType, IRLookups, IRPublicAccess, IRRole
from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportSummary, PhaseContext
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.load.lookup_loader import NOTE_ROLES_NEED_PERMISSIONS, load_lookups
from specivo.importers.load.public_access_loader import (
    NOTE_ANONYMOUS_PROJECTS,
    NOTE_ANONYMOUS_SWITCH,
    NOTE_NON_MEMBER_DIFFERENCES,
    NOTE_PERMISSIONS_NOT_CARRIED,
    load_public_access,
)
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.setting import Setting
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    ANONYMOUS_ACCESS_SETTING_KEY,
    ImportedAnonymousPermissionsResult,
    apply_imported_anonymous_permissions,
    is_anonymous_access_enabled,
)
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


class _Adapter:
    source_system = "redmine"
    source_instance = "unit-ir"

    def __init__(self, lookups: IRLookups | None = None, access: IRPublicAccess | None = None) -> None:
        self._lookups = lookups or IRLookups()
        self._access = access or IRPublicAccess()

    async def extract_lookups(self) -> IRLookups:
        return self._lookups

    async def extract_public_access(self) -> IRPublicAccess:
        return self._access


def _context(session, adapter, phase: ImportPhase, run_id: uuid.UUID | None = None) -> PhaseContext:
    run_id = run_id or uuid.uuid4()
    instance = f"unit-ir-{run_id.hex[:8]}"
    return PhaseContext(
        phase=phase,
        session=session,
        adapter=adapter,
        options=ImportOptions(source_instance=instance),
        summary=ImportSummary(run_id=run_id, source_system="redmine", source_instance=instance, dry_run=False),
        reporter=NullProgressReporter(),
        id_map=ImportIdMap(source_system="redmine", source_instance=instance, run_id=run_id),
    )


class TestRoles:
    async def test_the_anonymous_role_is_not_created(self, db_session):
        """It used to be inserted as an assignable builtin role, which the database refuses."""
        lookups = IRLookups(roles=[IRRole(source_ref="2", name="Anonymous", builtin=2)])
        ctx = _context(db_session, _Adapter(lookups), ImportPhase.LOOKUPS)

        await load_lookups(ctx)

        anonymous = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.ANONYMOUS))).all()
        assert anonymous == []
        assert await ctx.id_map.get(db_session, EntityType.ROLE, "2") is None

    async def test_the_non_member_role_maps_onto_the_seeded_one_unchanged(self, db_session):
        seeded = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()
        before = list(seeded.permissions)
        lookups = IRLookups(roles=[IRRole(source_ref="1", name="Non-member users", builtin=1)])
        ctx = _context(db_session, _Adapter(lookups), ImportPhase.LOOKUPS)

        await load_lookups(ctx)

        assert await ctx.id_map.get(db_session, EntityType.ROLE, "1") == seeded.id
        await db_session.refresh(seeded)
        assert seeded.permissions == before

    async def test_an_ordinary_role_named_like_a_builtin_one_gets_its_own_row(self, db_session):
        """A builtin role cannot hold a membership, so it must not absorb a custom role."""
        lookups = IRLookups(roles=[IRRole(source_ref="7", name="Non member", builtin=0)])
        ctx = _context(db_session, _Adapter(lookups), ImportPhase.LOOKUPS)

        await load_lookups(ctx)

        role = await db_session.get(Role, await ctx.id_map.get(db_session, EntityType.ROLE, "7"))
        assert role.builtin == RoleBuiltin.CUSTOM
        assert role.name == "Non member (imported)"
        assert ctx.summary.notes[NOTE_ROLES_NEED_PERMISSIONS] == ["Non member (imported)"]


@pytest_asyncio.fixture
async def imported_projects(db_session) -> dict[str, Project]:
    """Two public projects and a private one, mapped as if an earlier phase imported them."""
    built = {}
    for ref, key, is_public in (("1", "IRPUB", True), ("2", "IRPUBTWO", True), ("3", "IRPRIV", False)):
        project = ProjectFactory.build(key=key, identifier=key.lower(), name=key, is_public=is_public)
        db_session.add(project)
        built[ref] = project
    await db_session.flush()
    return built


def _access(**overrides) -> IRPublicAccess:
    values = {
        "anonymous_allowed": True,
        "anonymous_source_setting": "login_required = '0'",
        "anonymous_permissions": {"1": ["view_issues", "view_wiki"], "2": ["view_issues"], "3": ["view_issues"]},
        "non_member_permissions": ["view_issues", "view_wiki"],
        "not_carried_over": ["Non member role: add_issues"],
    }
    values.update(overrides)
    return IRPublicAccess(**values)


async def _projects_phase(db_session, ctx, projects: dict[str, Project]) -> None:
    for ref, project in projects.items():
        await ctx.id_map.put(db_session, EntityType.PROJECT, ref, "projects", project.id)
    ctx.project_refs = list(projects)


async def _run(db_session, projects, access, run_id=None) -> PhaseContext:
    ctx = _context(db_session, _Adapter(access=access), ImportPhase.MEMBERSHIPS, run_id)
    await _projects_phase(db_session, ctx, projects)
    await load_public_access(ctx)
    return ctx


async def _import_audit_rows(db_session, projects: dict[str, Project]) -> list[SecurityAuditLog]:
    stmt = select(SecurityAuditLog).where(
        SecurityAuditLog.event_type == AuditEvent.PROJECT_ANONYMOUS_PERMISSIONS_CHANGED,
        SecurityAuditLog.project_id.in_([p.id for p in projects.values()]),
    )
    return list((await db_session.execute(stmt)).scalars().all())


class TestApplyImportedAnonymousPermissions:
    """The service the loader calls, which alone decides whether an opt-in is written."""

    async def test_refuses_while_the_switch_is_on(self, db_session, imported_projects):
        db_session.add(Setting(key=ANONYMOUS_ACCESS_SETTING_KEY, value="true"))
        await db_session.flush()
        actor = await _actor(db_session)

        result = await apply_imported_anonymous_permissions(db_session, imported_projects["1"], ["view_issues"], actor)

        assert result is ImportedAnonymousPermissionsResult.SWITCH_ON
        assert imported_projects["1"].anonymous_permissions == []
        assert await _import_audit_rows(db_session, imported_projects) == []

    async def test_applies_while_the_switch_is_off(self, db_session, imported_projects):
        actor = await _actor(db_session)

        result = await apply_imported_anonymous_permissions(db_session, imported_projects["1"], ["view_issues"], actor)

        assert result is ImportedAnonymousPermissionsResult.APPLIED
        assert imported_projects["1"].anonymous_permissions == ["view_issues"]

    async def test_reports_why_nothing_was_written(self, db_session, imported_projects):
        actor = await _actor(db_session)
        private, public = imported_projects["3"], imported_projects["2"]
        await apply_imported_anonymous_permissions(db_session, public, ["view_issues"], actor)

        assert (
            await apply_imported_anonymous_permissions(db_session, private, ["view_issues"], actor)
            is ImportedAnonymousPermissionsResult.NOT_PUBLIC
        )
        assert (
            await apply_imported_anonymous_permissions(db_session, public, ["view_wiki"], actor)
            is ImportedAnonymousPermissionsResult.ALREADY_SET
        )
        assert (
            await apply_imported_anonymous_permissions(db_session, imported_projects["1"], [], actor)
            is ImportedAnonymousPermissionsResult.NOTHING_TO_GRANT
        )
        assert public.anonymous_permissions == ["view_issues"]


async def _actor(db_session) -> User:
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    return user


class TestPublicAccess:
    async def test_public_projects_are_opted_in_and_the_switch_stays_off(self, db_session, imported_projects):
        ctx = await _run(db_session, imported_projects, _access())

        assert imported_projects["1"].anonymous_permissions == ["view_issues", "view_wiki"]
        assert imported_projects["2"].anonymous_permissions == ["view_issues"]
        assert await is_anonymous_access_enabled(db_session) is False
        assert sorted(ctx.summary.notes[NOTE_ANONYMOUS_PROJECTS]) == [
            "IRPUB: view_issues, view_wiki",
            "IRPUBTWO: view_issues",
        ]
        assert ctx.summary.notes[NOTE_PERMISSIONS_NOT_CARRIED] == ["Non member role: add_issues"]

    async def test_a_private_project_is_never_opted_in(self, db_session, imported_projects):
        """Even if the IR asks for it: the service refuses and the import warns."""
        ctx = await _run(db_session, imported_projects, _access())

        assert imported_projects["3"].anonymous_permissions == []
        assert any(w.context.get("project") == "IRPRIV" for w in ctx.summary.warnings)

    async def test_opt_ins_are_audited_as_an_import(self, db_session, imported_projects):
        await _run(db_session, imported_projects, _access())

        rows = (
            (
                await db_session.execute(
                    select(SecurityAuditLog).where(
                        SecurityAuditLog.event_type == AuditEvent.PROJECT_ANONYMOUS_PERMISSIONS_CHANGED,
                        SecurityAuditLog.project_id.in_([p.id for p in imported_projects.values()]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(r.details["project_key"] for r in rows) == ["IRPUB", "IRPUBTWO"]
        assert {r.details["reason"] for r in rows} == {"import"}

    async def test_nothing_when_the_source_required_sign_in(self, db_session, imported_projects):
        ctx = await _run(db_session, imported_projects, _access(anonymous_allowed=False, anonymous_permissions={}))

        assert all(p.anonymous_permissions == [] for p in imported_projects.values())
        assert NOTE_ANONYMOUS_PROJECTS not in ctx.summary.notes

    async def test_a_rerun_is_idempotent_and_respects_an_administrator(self, db_session, imported_projects):
        run_id = uuid.uuid4()
        await _run(db_session, imported_projects, _access(), run_id)
        imported_projects["1"].anonymous_permissions = []
        await db_session.flush()

        second = await _run(db_session, imported_projects, _access(), run_id)

        assert imported_projects["1"].anonymous_permissions == []
        assert imported_projects["2"].anonymous_permissions == ["view_issues"]
        assert second.summary.created[EntityType.ANONYMOUS_ACCESS] == 0
        assert second.summary.skipped[EntityType.ANONYMOUS_ACCESS] == 2

    async def test_nothing_is_applied_while_the_switch_is_on(self, db_session, imported_projects):
        db_session.add(Setting(key=ANONYMOUS_ACCESS_SETTING_KEY, value="true"))
        await db_session.flush()

        ctx = await _run(db_session, imported_projects, _access())

        assert all(p.anonymous_permissions == [] for p in imported_projects.values())
        assert (
            "IRPUB: view_issues, view_wiki (not applied: switch on, opt in by hand)"
            in ctx.summary.notes[NOTE_ANONYMOUS_PROJECTS]
        )

    async def test_the_switch_turned_on_after_the_loader_looked_still_blocks_the_opt_in(
        self, db_session, imported_projects, monkeypatch
    ):
        """The loader's own read of the switch is only for the report.

        Simulates the switch being turned on between that read and the opt-in:
        the loader is made to see it off while the database has it on. The
        service re-checks under the switch's lock and writes nothing.
        """
        import specivo.importers.load.public_access_loader as loader

        async def _looks_off(_session) -> bool:
            return False

        monkeypatch.setattr(loader, "is_anonymous_access_enabled", _looks_off)
        db_session.add(Setting(key=ANONYMOUS_ACCESS_SETTING_KEY, value="true"))
        await db_session.flush()

        ctx = await _run(db_session, imported_projects, _access())

        assert all(p.anonymous_permissions == [] for p in imported_projects.values())
        assert ctx.summary.created[EntityType.ANONYMOUS_ACCESS] == 0
        assert await _import_audit_rows(db_session, imported_projects) == []
        assert (
            "IRPUBTWO: view_issues (not applied: switch on, opt in by hand)"
            in ctx.summary.notes[NOTE_ANONYMOUS_PROJECTS]
        )
        assert ctx.summary.notes[NOTE_ANONYMOUS_SWITCH][0].startswith("Anonymous access is on")

    async def test_non_member_differences_are_reported(self, db_session, imported_projects):
        ctx = await _run(db_session, imported_projects, _access(non_member_permissions=["view_wiki"]))

        differences = ctx.summary.notes[NOTE_NON_MEMBER_DIFFERENCES]
        assert any(line.startswith("view_wiki: granted by the source's") for line in differences)
        assert any(line.startswith("view_issues: granted by Specivo's") for line in differences)
