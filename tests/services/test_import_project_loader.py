"""Service tests for the project loader.

The behaviour worth pinning is where Redmine and Specivo disagree: a project
key that Redmine never had, a nesting limit Redmine does not enforce, group
memberships Specivo cannot represent, and custom fields that have to become
per-project schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from specivo.importers.core.ir import (
    EntityType,
    IRCategory,
    IRCustomField,
    IRGroup,
    IRMembership,
    IRProject,
    IRRole,
    IRStatus,
    IRTracker,
    IRUser,
    IRVersion,
    PrincipalKind,
)
from specivo.importers.load.lookup_loader import load_lookups
from specivo.importers.load.project_loader import (
    NOTE_MODULES_DROPPED,
    NOTE_PROJECT_KEYS,
    load_custom_field_schemas,
    load_memberships,
    load_project_lookups,
    load_projects,
)
from specivo.importers.load.user_loader import load_groups, load_users
from specivo.models.lookups import IssueCategory
from specivo.models.member import Member, MemberRole
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.project import EnabledModule, Project
from specivo.models.version import Version
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


class ProjectAdapter(FakeAdapter):
    """Adapter with its own source system.

    The import service account's login is derived from it, and test modules
    run in parallel: two of them inserting the same login in uncommitted
    transactions block on the unique index.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("source_system", "redmineproj")
        super().__init__(**kwargs)


def _project(ref: str = "1", identifier: str = "acme-app", **overrides) -> IRProject:
    data = {
        "source_ref": ref,
        "identifier": identifier,
        "name": "Acme App",
        "description": "The app",
        "is_public": True,
        "modules": ["issue_tracking", "wiki"],
        "created_at": datetime(2019, 5, 1, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return IRProject(**data)


@pytest_asyncio.fixture
async def loaded(db_session, make_context):
    """A context whose lookups and users are already imported."""

    async def _load(adapter: FakeAdapter):
        adapter.lookups = adapter.lookups or None
        ctx = make_context(adapter)
        if adapter.lookups.statuses or adapter.lookups.roles:
            await load_lookups(ctx)
        if adapter.users:
            await load_users(ctx)
        if adapter.groups:
            await load_groups(ctx)
        await load_projects(ctx)
        return ctx

    return _load


class TestProjects:
    async def test_creates_the_project(self, db_session, loaded):
        ctx = await loaded(ProjectAdapter(projects=[_project()]))

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        project = await db_session.get(Project, project_id)
        assert project.name == "Acme App"
        assert project.identifier == "acme-app"
        assert project.is_public is True

    async def test_key_is_derived_from_the_identifier(self, db_session, loaded):
        """Redmine has no project key, so one has to be invented."""
        ctx = await loaded(ProjectAdapter(projects=[_project()]))

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        assert (await db_session.get(Project, project_id)).key == "ACMEAPP"

    async def test_every_key_is_reported(self, db_session, loaded):
        """This is the mapping operators most often want to correct."""
        ctx = await loaded(ProjectAdapter(projects=[_project()]))
        assert ctx.summary.notes[NOTE_PROJECT_KEYS] == ["acme-app -> ACMEAPP"]

    async def test_operator_can_override_the_key(self, db_session, make_context):
        ctx = make_context(ProjectAdapter(projects=[_project()]), project_key_map={"acme-app": "ACME"})
        await load_projects(ctx)

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        assert (await db_session.get(Project, project_id)).key == "ACME"

    async def test_identifier_starting_with_a_digit_gets_a_usable_key(self, db_session, loaded):
        """A key must start with a letter; an identifier need not."""
        ctx = await loaded(ProjectAdapter(projects=[_project(identifier="2026-roadmap")]))

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        key = (await db_session.get(Project, project_id)).key
        assert key[0].isalpha()

    async def test_taken_key_is_suffixed(self, db_session, loaded):
        db_session.add(Project(name="Existing", identifier="existing", key="ACMEAPP", path="existing"))
        await db_session.flush()

        ctx = await loaded(ProjectAdapter(projects=[_project()]))
        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        assert (await db_session.get(Project, project_id)).key == "ACMEAPP2"

    async def test_taken_identifier_is_suffixed(self, db_session, loaded):
        db_session.add(Project(name="Existing", identifier="acme-app", key="OTHER", path="acme_app"))
        await db_session.flush()

        ctx = await loaded(ProjectAdapter(projects=[_project()]))
        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        assert (await db_session.get(Project, project_id)).identifier == "acme-app-2"

    async def test_original_timestamps_are_restored(self, db_session, loaded):
        ctx = await loaded(ProjectAdapter(projects=[_project()]))

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        project = await db_session.get(Project, project_id)
        assert project.created_at == datetime(2019, 5, 1, 9, 0, tzinfo=UTC)

    async def test_closed_project_keeps_its_status(self, db_session, loaded):
        """The create schema has no status field, so it is set afterwards."""
        ctx = await loaded(ProjectAdapter(projects=[_project(status=5)]))

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        assert (await db_session.get(Project, project_id)).status == 5

    async def test_archived_project_keeps_its_status(self, db_session, loaded):
        ctx = await loaded(ProjectAdapter(projects=[_project(status=9)]))
        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        assert (await db_session.get(Project, project_id)).status == 9


class TestSubprojects:
    async def test_child_is_attached_to_its_parent(self, db_session, loaded):
        projects = [_project("1", "parent"), _project("2", "child", parent_ref="1")]
        ctx = await loaded(ProjectAdapter(projects=projects))

        parent_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        child_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "2")
        assert (await db_session.get(Project, child_id)).parent_id == parent_id

    async def test_child_path_nests_under_the_parent(self, db_session, loaded):
        projects = [_project("1", "parent"), _project("2", "child", parent_ref="1")]
        ctx = await loaded(ProjectAdapter(projects=projects))

        child_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "2")
        assert str((await db_session.get(Project, child_id)).path) == "parent.child"

    async def test_three_levels_nest(self, db_session, loaded):
        projects = [
            _project("1", "top"),
            _project("2", "middle", parent_ref="1"),
            _project("3", "bottom", parent_ref="2"),
        ]
        ctx = await loaded(ProjectAdapter(projects=projects))

        bottom_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "3")
        assert str((await db_session.get(Project, bottom_id)).path) == "top.middle.bottom"

    async def test_orphan_becomes_a_root_with_a_warning(self, db_session, loaded):
        """A parent outside the selected scope must not take its child with it."""
        ctx = await loaded(ProjectAdapter(projects=[_project("2", "child", parent_ref="999")]))

        child_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "2")
        assert (await db_session.get(Project, child_id)).parent_id is None
        assert any("Parent project was not imported" in w.message for w in ctx.summary.warnings)


class TestModules:
    async def test_mapped_modules_are_enabled(self, db_session, loaded):
        ctx = await loaded(ProjectAdapter(projects=[_project()]))

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        stmt = select(EnabledModule.name).where(EnabledModule.project_id == project_id)
        names = set((await db_session.execute(stmt)).scalars().all())
        assert {"issue_tracking", "wiki"} <= names

    async def test_unmapped_modules_are_reported(self, db_session, make_context):
        """Repository and forums belong to features Specivo does not have."""
        adapter = ProjectAdapter(projects=[_project()], dropped={"1": ["repository", "boards"]})
        ctx = make_context(adapter)
        await load_projects(ctx)

        assert ctx.summary.notes[NOTE_MODULES_DROPPED] == ["acme-app: repository, boards"]


class TestVersionsAndCategories:
    async def test_version_fields_carry_across(self, db_session, loaded):
        """status and sharing use the same vocabulary in both systems."""
        versions = [
            IRVersion(
                source_ref="10",
                project_ref="1",
                name="1.0",
                description="First",
                status="locked",
                sharing="descendants",
            )
        ]
        ctx = await loaded(ProjectAdapter(projects=[_project()], versions=versions))
        await load_project_lookups(ctx)

        version_id = await ctx.id_map.get(db_session, EntityType.VERSION, "10")
        version = await db_session.get(Version, version_id)
        assert version.name == "1.0"
        assert version.status == "locked"
        assert version.sharing == "descendants"

    async def test_category_is_created_with_its_assignee(self, db_session, loaded):
        users = [IRUser(source_ref="7", login="proj_alex", display_name="Alex", email="proj_alex@example.org")]
        categories = [IRCategory(source_ref="20", project_ref="1", name="Backend", assigned_to_ref="7")]
        ctx = await loaded(ProjectAdapter(projects=[_project()], users=users, categories=categories))
        await load_project_lookups(ctx)

        category_id = await ctx.id_map.get(db_session, EntityType.CATEGORY, "20")
        category = await db_session.get(IssueCategory, category_id)
        assert category.name == "Backend"
        assert category.assigned_to_id == await ctx.id_map.get(db_session, EntityType.USER, "7")

    async def test_second_run_creates_no_duplicates(self, db_session, loaded):
        versions = [IRVersion(source_ref="10", project_ref="1", name="1.0")]
        ctx = await loaded(ProjectAdapter(projects=[_project()], versions=versions))
        await load_project_lookups(ctx)
        await load_project_lookups(ctx)

        count = (await db_session.execute(select(func.count()).select_from(Version))).scalar_one()
        assert count == 1


class TestMemberships:
    @pytest_asyncio.fixture
    async def with_roles(self, make_context):
        from specivo.importers.core.ir import IRLookups

        def _adapter(**kwargs):
            lookups = IRLookups(
                statuses=[IRStatus(source_ref="1", name="New", category="backlog")],
                trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1")],
                roles=[IRRole(source_ref="3", name="Proj Developer"), IRRole(source_ref="4", name="Proj Manager")],
            )
            return ProjectAdapter(lookups=lookups, **kwargs)

        return _adapter

    async def test_direct_membership_grants_the_role(self, db_session, loaded, with_roles):
        adapter = with_roles(
            projects=[_project()],
            users=[IRUser(source_ref="7", login="proj_alex", display_name="Alex", email="proj_alex@example.org")],
            memberships=[
                IRMembership(
                    source_ref="100",
                    project_ref="1",
                    principal_ref="7",
                    principal_kind=PrincipalKind.USER,
                    role_refs=["3"],
                )
            ],
        )
        ctx = await loaded(adapter)
        await load_memberships(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "7")
        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        member = (
            await db_session.execute(select(Member).where(Member.user_id == user_id, Member.project_id == project_id))
        ).scalar_one()
        roles = (await db_session.execute(select(MemberRole).where(MemberRole.member_id == member.id))).scalars().all()
        assert len(roles) == 1

    async def test_group_membership_is_flattened_to_its_members(self, db_session, loaded, with_roles):
        """Specivo cannot hang a role off a group, so each member gets it."""
        adapter = with_roles(
            projects=[_project()],
            users=[
                IRUser(source_ref="7", login="proj_alex", display_name="Alex", email="proj_alex@example.org"),
                IRUser(source_ref="8", login="proj_sam", display_name="Sam", email="proj_sam@example.org"),
            ],
            groups=[IRGroup(source_ref="20", name="Platform", member_refs=["7", "8"])],
            memberships=[
                IRMembership(
                    source_ref="100",
                    project_ref="1",
                    principal_ref="20",
                    principal_kind=PrincipalKind.GROUP,
                    role_refs=["3"],
                )
            ],
        )
        ctx = await loaded(adapter)
        await load_memberships(ctx)

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        members = (await db_session.execute(select(Member).where(Member.project_id == project_id))).scalars().all()
        assert len(members) == 2

    async def test_direct_and_group_roles_are_unioned(self, db_session, loaded, with_roles):
        """A user in both keeps every role Redmine gave them."""
        adapter = with_roles(
            projects=[_project()],
            users=[IRUser(source_ref="7", login="proj_alex", display_name="Alex", email="proj_alex@example.org")],
            groups=[IRGroup(source_ref="20", name="Platform", member_refs=["7"])],
            memberships=[
                IRMembership(
                    source_ref="100",
                    project_ref="1",
                    principal_ref="7",
                    principal_kind=PrincipalKind.USER,
                    role_refs=["3"],
                ),
                IRMembership(
                    source_ref="101",
                    project_ref="1",
                    principal_ref="20",
                    principal_kind=PrincipalKind.GROUP,
                    role_refs=["4"],
                ),
            ],
        )
        ctx = await loaded(adapter)
        await load_memberships(ctx)

        user_id = await ctx.id_map.get(db_session, EntityType.USER, "7")
        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        member = (
            await db_session.execute(select(Member).where(Member.user_id == user_id, Member.project_id == project_id))
        ).scalar_one()
        roles = (await db_session.execute(select(MemberRole).where(MemberRole.member_id == member.id))).scalars().all()
        assert len(roles) == 2

    async def test_membership_with_no_imported_role_is_skipped(self, db_session, loaded, with_roles):
        adapter = with_roles(
            projects=[_project()],
            users=[IRUser(source_ref="7", login="proj_alex", display_name="Alex", email="proj_alex@example.org")],
            memberships=[
                IRMembership(
                    source_ref="100",
                    project_ref="1",
                    principal_ref="7",
                    principal_kind=PrincipalKind.USER,
                    role_refs=["999"],
                )
            ],
        )
        ctx = await loaded(adapter)
        await load_memberships(ctx)
        assert any("no role that was imported" in w.message for w in ctx.summary.warnings)


class TestCustomFieldSchemas:
    def _field(self, **overrides) -> IRCustomField:
        data = {
            "source_ref": "1",
            "name": "Severity",
            "key": "severity",
            "field_format": "list",
            "json_schema": {"type": "string", "enum": ["Low", "High"]},
            "is_for_all": True,
            "tracker_refs": ["1"],
        }
        data.update(overrides)
        return IRCustomField(**data)

    @pytest_asyncio.fixture
    async def with_tracker(self, make_context):
        from specivo.importers.core.ir import IRLookups

        def _adapter(**kwargs):
            lookups = IRLookups(
                statuses=[IRStatus(source_ref="1", name="New", category="backlog")],
                trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1")],
            )
            return ProjectAdapter(lookups=lookups, **kwargs)

        return _adapter

    async def test_schema_is_created_per_project_and_tracker(self, db_session, loaded, with_tracker):
        ctx = await loaded(with_tracker(projects=[_project()], custom_fields=[self._field()]))
        await load_custom_field_schemas(ctx)

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        schema = (
            await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project_id))
        ).scalar_one()
        assert schema.name == "Imported fields (Bug)"
        assert schema.tracker_id == await ctx.id_map.get(db_session, EntityType.TRACKER, "1")
        assert schema.schema_definition["properties"]["severity"]["enum"] == ["Low", "High"]

    async def test_field_without_a_tracker_becomes_project_wide(self, db_session, loaded, with_tracker):
        ctx = await loaded(with_tracker(projects=[_project()], custom_fields=[self._field(tracker_refs=[])]))
        await load_custom_field_schemas(ctx)

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        schema = (
            await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project_id))
        ).scalar_one()
        assert schema.tracker_id is None
        assert schema.name == "Imported fields"

    async def test_several_fields_share_one_schema(self, db_session, loaded, with_tracker):
        """One schema per scope, not one per field, keeps the admin view readable."""
        fields = [self._field(), self._field(source_ref="2", name="Reviewer", key="reviewer")]
        ctx = await loaded(with_tracker(projects=[_project()], custom_fields=fields))
        await load_custom_field_schemas(ctx)

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        schemas = (
            (await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project_id)))
            .scalars()
            .all()
        )
        assert len(schemas) == 1
        assert set(schemas[0].schema_definition["properties"]) == {"severity", "reviewer"}

    async def test_required_fields_are_declared_required(self, db_session, loaded, with_tracker):
        ctx = await loaded(with_tracker(projects=[_project()], custom_fields=[self._field(is_required=True)]))
        await load_custom_field_schemas(ctx)

        project_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "1")
        schema = (
            await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project_id))
        ).scalar_one()
        assert schema.schema_definition["required"] == ["severity"]

    async def test_project_scoped_field_skips_other_projects(self, db_session, loaded, with_tracker):
        projects = [_project("1", "one"), _project("2", "two")]
        field = self._field(is_for_all=False, project_refs=["1"])
        ctx = await loaded(with_tracker(projects=projects, custom_fields=[field]))
        await load_custom_field_schemas(ctx)

        other_id = await ctx.id_map.get(db_session, EntityType.PROJECT, "2")
        schemas = (
            (await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == other_id)))
            .scalars()
            .all()
        )
        assert schemas == []

    async def test_second_run_creates_no_duplicate_schema(self, db_session, loaded, with_tracker):
        ctx = await loaded(with_tracker(projects=[_project()], custom_fields=[self._field()]))
        await load_custom_field_schemas(ctx)
        await load_custom_field_schemas(ctx)

        count = (await db_session.execute(select(func.count()).select_from(MetadataSchema))).scalar_one()
        assert count == 1


class TestIdempotency:
    async def test_second_project_run_creates_nothing(self, db_session, loaded):
        ctx = await loaded(ProjectAdapter(projects=[_project()]))
        await load_projects(ctx)

        count = (
            await db_session.execute(select(func.count()).select_from(Project).where(Project.identifier == "acme-app"))
        ).scalar_one()
        assert count == 1
        assert ctx.summary.skipped[EntityType.PROJECT] >= 1
