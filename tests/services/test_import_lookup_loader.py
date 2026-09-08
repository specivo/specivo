"""Service tests for the lookup loader.

The interesting behaviour is what happens when the source and the target both
already have a status called "New": whether a second row appears, when an
existing row is reused instead, and what the operator is told either way.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from specivo.importers.core.ir import EntityType, IRActivity, IRLookups, IRPriority, IRRole, IRStatus, IRTracker
from specivo.importers.load.lookup_loader import NOTE_ROLES_NEED_PERMISSIONS, load_lookups
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntryActivity
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


def _lookups(**overrides) -> IRLookups:
    data = {
        "statuses": [
            IRStatus(source_ref="1", name="New", category="backlog", position=1),
            IRStatus(source_ref="5", name="Closed", category="closed", position=5),
        ],
        "trackers": [IRTracker(source_ref="1", name="Bug", default_status_ref="1", position=1)],
        "priorities": [IRPriority(source_ref="4", name="Normal", position=2, is_default=True)],
        "activities": [IRActivity(source_ref="9", name="Lookup Development", is_default=True)],
        "roles": [IRRole(source_ref="3", name="Lookup Developer")],
    }
    data.update(overrides)
    return IRLookups(**data)


@pytest_asyncio.fixture
async def seeded_status(db_session):
    """A status that collides by name with the source's, as a seeded instance would."""
    row = IssueStatus(name="New", category="backlog", position=1)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestFreshInstance:
    async def test_creates_every_lookup(self, db_session, make_context):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        assert ctx.summary.created[EntityType.STATUS] == 2
        assert ctx.summary.created[EntityType.TRACKER] == 1
        assert ctx.summary.created[EntityType.PRIORITY] == 1
        assert ctx.summary.created[EntityType.ACTIVITY] == 1
        assert ctx.summary.created[EntityType.ROLE] == 1

    async def test_status_category_is_carried_over(self, db_session, make_context):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        status_id = await ctx.id_map.get(db_session, EntityType.STATUS, "5")
        status = await db_session.get(IssueStatus, status_id)
        assert status.name == "Closed"
        assert status.category == "closed"

    async def test_tracker_points_at_the_imported_default_status(self, db_session, make_context):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        tracker_id = await ctx.id_map.get(db_session, EntityType.TRACKER, "1")
        tracker = await db_session.get(Tracker, tracker_id)
        expected = await ctx.id_map.get(db_session, EntityType.STATUS, "1")
        assert tracker.default_status_id == expected

    async def test_tracker_keeps_its_disabled_fields(self, db_session, make_context):
        lookups = _lookups(
            trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1", disabled_core_fields=["due_date"])]
        )
        ctx = make_context(FakeAdapter(lookups=lookups))
        await load_lookups(ctx)

        tracker_id = await ctx.id_map.get(db_session, EntityType.TRACKER, "1")
        tracker = await db_session.get(Tracker, tracker_id)
        assert tracker.disabled_core_fields == ["due_date"]

    async def test_missing_default_status_falls_back_and_warns(self, db_session, make_context):
        """A tracker must point at some status, so an unresolved one is not fatal."""
        lookups = _lookups(trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="404")])
        ctx = make_context(FakeAdapter(lookups=lookups))
        await load_lookups(ctx)

        tracker_id = await ctx.id_map.get(db_session, EntityType.TRACKER, "1")
        tracker = await db_session.get(Tracker, tracker_id)
        assert tracker.default_status_id is not None
        assert any("default status" in w.message for w in ctx.summary.warnings)

    async def test_new_role_is_created_without_permissions(self, db_session, make_context):
        """Redmine's permission names are its own; an empty role grants nothing."""
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        role_id = await ctx.id_map.get(db_session, EntityType.ROLE, "3")
        role = await db_session.get(Role, role_id)
        assert role.permissions == []
        assert "Lookup Developer" in ctx.summary.notes[NOTE_ROLES_NEED_PERMISSIONS]


class TestNameCollisions:
    async def test_second_status_is_created_by_default(self, db_session, make_context, seeded_status):
        """Merging two same-named statuses would merge their meaning too."""
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        count = (
            await db_session.execute(select(func.count()).select_from(IssueStatus).where(IssueStatus.name == "New"))
        ).scalar_one()
        assert count == 2

    async def test_collision_is_reported(self, db_session, make_context, seeded_status):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)
        assert any("--merge-duplicate-statuses" in w.message for w in ctx.summary.warnings)

    async def test_merge_flag_reuses_the_existing_status(self, db_session, make_context, seeded_status):
        ctx = make_context(FakeAdapter(lookups=_lookups()), merge_duplicate_statuses=True)
        await load_lookups(ctx)

        mapped = await ctx.id_map.get(db_session, EntityType.STATUS, "1")
        assert mapped == seeded_status.id
        assert ctx.summary.reused[EntityType.STATUS] == 1

    async def test_merge_warns_when_the_category_differs(self, db_session, make_context, seeded_status):
        """Reusing a row whose meaning differs is exactly what needs saying out loud."""
        lookups = _lookups(statuses=[IRStatus(source_ref="1", name="New", category="active")])
        ctx = make_context(FakeAdapter(lookups=lookups), merge_duplicate_statuses=True)
        await load_lookups(ctx)
        assert any("category differs" in w.message for w in ctx.summary.warnings)

    async def test_name_matching_ignores_case(self, db_session, make_context, seeded_status):
        lookups = _lookups(statuses=[IRStatus(source_ref="1", name="new", category="backlog")])
        ctx = make_context(FakeAdapter(lookups=lookups), merge_duplicate_statuses=True)
        await load_lookups(ctx)
        assert await ctx.id_map.get(db_session, EntityType.STATUS, "1") == seeded_status.id

    async def test_activity_is_always_reused(self, db_session, make_context):
        """time_entry_activities.name is unique, so a duplicate cannot exist."""
        existing = TimeEntryActivity(name="Lookup Development", is_default=False)
        db_session.add(existing)
        await db_session.flush()

        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        assert await ctx.id_map.get(db_session, EntityType.ACTIVITY, "9") == existing.id
        assert ctx.summary.reused[EntityType.ACTIVITY] == 1

    async def test_role_is_always_reused(self, db_session, make_context):
        """roles.name is unique, and duplicating a role would fragment access."""
        existing = Role(name="Lookup Developer", permissions=["view_issues"])
        db_session.add(existing)
        await db_session.flush()

        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        assert await ctx.id_map.get(db_session, EntityType.ROLE, "3") == existing.id
        assert existing.permissions == ["view_issues"]

    async def test_builtin_role_is_matched_on_its_flag(self, db_session, make_context):
        """Either system may name the non-member role differently."""
        existing = Role(name="Lookup Non member", builtin=1, permissions=[])
        db_session.add(existing)
        await db_session.flush()

        lookups = _lookups(roles=[IRRole(source_ref="1", name="Anonymous visitors", builtin=1)])
        ctx = make_context(FakeAdapter(lookups=lookups))
        await load_lookups(ctx)

        assert await ctx.id_map.get(db_session, EntityType.ROLE, "1") == existing.id


class TestDefaults:
    async def test_existing_default_priority_is_not_displaced(self, db_session, make_context):
        """Two defaults would make which one applies arbitrary."""
        existing = IssuePriority(name="Medium", position=2, is_default=True)
        db_session.add(existing)
        await db_session.flush()

        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        imported_id = await ctx.id_map.get(db_session, EntityType.PRIORITY, "4")
        imported = await db_session.get(IssuePriority, imported_id)
        assert imported.is_default is False
        assert existing.is_default is True
        assert any("already has one" in w.message for w in ctx.summary.warnings)

    async def test_default_priority_is_kept_on_a_fresh_instance(self, db_session, make_context):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)

        imported_id = await ctx.id_map.get(db_session, EntityType.PRIORITY, "4")
        imported = await db_session.get(IssuePriority, imported_id)
        assert imported.is_default is True


class TestIdempotency:
    async def test_second_run_creates_nothing(self, db_session, make_context):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)
        first = (await db_session.execute(select(func.count()).select_from(IssueStatus))).scalar_one()

        again = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(again)
        second = (await db_session.execute(select(func.count()).select_from(IssueStatus))).scalar_one()

        assert second == first
        assert again.summary.skipped[EntityType.STATUS] == 2
        assert again.summary.created[EntityType.STATUS] == 0

    async def test_second_run_keeps_the_original_mapping(self, db_session, make_context):
        ctx = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(ctx)
        mapped = await ctx.id_map.get(db_session, EntityType.STATUS, "1")

        again = make_context(FakeAdapter(lookups=_lookups()))
        await load_lookups(again)
        assert await again.id_map.get(db_session, EntityType.STATUS, "1") == mapped


class TestStrictMode:
    async def test_a_collision_is_fatal_under_strict(self, db_session, make_context, seeded_status):
        from specivo.importers.core.pipeline import PhaseFailedError

        ctx = make_context(FakeAdapter(lookups=_lookups()), strict=True)
        with pytest.raises(PhaseFailedError):
            await load_lookups(ctx)
