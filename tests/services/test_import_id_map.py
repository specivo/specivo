"""Service tests for the import id map.

The map is what makes an import idempotent, so the behaviour worth pinning is
not just "a write can be read back" but the rules around it: an existing
mapping always wins, a resumed run under a new run id still sees the old rows,
and two source instances never share identifiers.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from specivo.importers.core.id_map import (
    ImportIdMap,
    MissingMappingError,
    ScopeRenameError,
    list_scopes,
    purge_scope,
    rename_scope,
)
from specivo.importers.core.ir import EntityType
from specivo.models.import_id_map import ImportIdMapping

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


@pytest_asyncio.fixture
async def id_map() -> ImportIdMap:
    return ImportIdMap(
        source_system="redmine",
        source_instance="tracker.example.org",
        run_id=uuid.uuid4(),
    )


class TestRoundTrip:
    async def test_put_then_get(self, db_session, id_map):
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        assert await id_map.get(db_session, EntityType.ISSUE, "42") == 7

    async def test_get_unmapped_returns_none(self, db_session, id_map):
        assert await id_map.get(db_session, EntityType.ISSUE, "999") is None

    async def test_get_none_source_id_returns_none(self, db_session, id_map):
        """Optional references pass through without a guard at the call site."""
        assert await id_map.get(db_session, EntityType.USER, None) is None

    async def test_put_returns_the_target_id(self, db_session, id_map):
        assert await id_map.put(db_session, EntityType.PROJECT, "1", "projects", 3) == 3

    async def test_numeric_source_ids_are_coerced_to_text(self, db_session, id_map):
        """Adapters may hand over an int primary key; the column is text."""
        await id_map.put(db_session, EntityType.ISSUE, 42, "issues", 7)
        assert await id_map.get(db_session, EntityType.ISSUE, "42") == 7

    async def test_entity_types_are_independent(self, db_session, id_map):
        await id_map.put(db_session, EntityType.ISSUE, "1", "issues", 100)
        await id_map.put(db_session, EntityType.PROJECT, "1", "projects", 200)
        assert await id_map.get(db_session, EntityType.ISSUE, "1") == 100
        assert await id_map.get(db_session, EntityType.PROJECT, "1") == 200

    async def test_row_records_the_target_table(self, db_session, id_map):
        await id_map.put(db_session, EntityType.WIKI_PAGE, "5", "wiki_pages", 9)
        row = (await db_session.execute(select(ImportIdMapping).where(ImportIdMapping.source_id == "5"))).scalar_one()
        assert row.target_table == "wiki_pages"
        assert row.import_run_id == id_map.run_id


class TestIdempotency:
    async def test_second_put_keeps_the_first_mapping(self, db_session, id_map):
        """Re-importing an entity must not re-point references at a duplicate row."""
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        returned = await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 999)
        assert returned == 7
        assert await id_map.get(db_session, EntityType.ISSUE, "42") == 7

    async def test_second_put_creates_no_second_row(self, db_session, id_map):
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 8)
        rows = (
            (await db_session.execute(select(ImportIdMapping).where(ImportIdMapping.source_id == "42"))).scalars().all()
        )
        assert len(rows) == 1

    async def test_a_resumed_run_sees_the_earlier_run_rows(self, db_session, id_map):
        """The uniqueness key excludes run id, so --resume is not fooled by a new one."""
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)

        resumed = ImportIdMap(
            source_system=id_map.source_system,
            source_instance=id_map.source_instance,
            run_id=uuid.uuid4(),
        )
        assert await resumed.get(db_session, EntityType.ISSUE, "42") == 7

    async def test_a_resumed_run_does_not_duplicate(self, db_session, id_map):
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        resumed = ImportIdMap(id_map.source_system, id_map.source_instance, uuid.uuid4())
        assert await resumed.put(db_session, EntityType.ISSUE, "42", "issues", 123) == 7

    async def test_database_rejects_a_duplicate_written_directly(self, db_session, id_map):
        """The constraint is enforced by the schema, not only by the service."""
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        db_session.add(
            ImportIdMapping(
                import_run_id=uuid.uuid4(),
                source_system="redmine",
                source_instance="tracker.example.org",
                entity_type=str(EntityType.ISSUE),
                source_id="42",
                target_table="issues",
                target_id=8,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestScoping:
    async def test_two_instances_do_not_share_identifiers(self, db_session, id_map):
        """Importing two installations of the same tracker must not cross-wire ids."""
        other = ImportIdMap("redmine", "other.example.org", uuid.uuid4())
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        await other.put(db_session, EntityType.ISSUE, "42", "issues", 8)

        assert await id_map.get(db_session, EntityType.ISSUE, "42") == 7
        assert await other.get(db_session, EntityType.ISSUE, "42") == 8

    async def test_two_source_systems_do_not_share_identifiers(self, db_session, id_map):
        jira = ImportIdMap("jira", "tracker.example.org", uuid.uuid4())
        await id_map.put(db_session, EntityType.ISSUE, "42", "issues", 7)
        await jira.put(db_session, EntityType.ISSUE, "42", "issues", 8)

        assert await id_map.get(db_session, EntityType.ISSUE, "42") == 7
        assert await jira.get(db_session, EntityType.ISSUE, "42") == 8


class TestBatchLookups:
    async def test_get_many_returns_only_mapped_ids(self, db_session, id_map):
        await id_map.put(db_session, EntityType.USER, "1", "users", 10)
        await id_map.put(db_session, EntityType.USER, "2", "users", 20)

        resolved = await id_map.get_many(db_session, EntityType.USER, ["1", "2", "3"])
        assert resolved == {"1": 10, "2": 20}

    async def test_get_many_with_no_ids_returns_empty(self, db_session, id_map):
        assert await id_map.get_many(db_session, EntityType.USER, []) == {}

    async def test_preload_fills_the_cache(self, db_session, id_map):
        await id_map.put(db_session, EntityType.STATUS, "1", "issue_statuses", 11)
        await id_map.put(db_session, EntityType.STATUS, "2", "issue_statuses", 12)

        fresh = ImportIdMap(id_map.source_system, id_map.source_instance, uuid.uuid4())
        assert fresh.cached(EntityType.STATUS, "1") is None

        loaded = await fresh.preload(db_session, EntityType.STATUS)
        assert loaded == {"1": 11, "2": 12}
        assert fresh.cached(EntityType.STATUS, "1") == 11

    async def test_preload_ignores_other_entity_types(self, db_session, id_map):
        await id_map.put(db_session, EntityType.STATUS, "1", "issue_statuses", 11)
        await id_map.put(db_session, EntityType.TRACKER, "1", "trackers", 21)

        fresh = ImportIdMap(id_map.source_system, id_map.source_instance, uuid.uuid4())
        assert await fresh.preload(db_session, EntityType.STATUS) == {"1": 11}

    async def test_count_narrows_by_entity_type(self, db_session, id_map):
        await id_map.put(db_session, EntityType.ISSUE, "1", "issues", 1)
        await id_map.put(db_session, EntityType.ISSUE, "2", "issues", 2)
        await id_map.put(db_session, EntityType.PROJECT, "1", "projects", 3)

        assert await id_map.count(db_session, EntityType.ISSUE) == 2
        assert await id_map.count(db_session) == 3


class TestRequire:
    async def test_require_returns_the_mapping(self, db_session, id_map):
        await id_map.put(db_session, EntityType.PROJECT, "1", "projects", 3)
        assert await id_map.require(db_session, EntityType.PROJECT, "1") == 3

    async def test_require_raises_when_unmapped(self, db_session, id_map):
        with pytest.raises(MissingMappingError):
            await id_map.require(db_session, EntityType.PROJECT, "404")

    async def test_require_message_names_the_entity(self, db_session, id_map):
        """The message has to say what was missing — imports fail hours in."""
        with pytest.raises(MissingMappingError) as exc:
            await id_map.require(db_session, EntityType.PROJECT, "404")
        assert "project" in str(exc.value)
        assert "404" in str(exc.value)


async def _scope_rows(session, system: str, instance: str) -> list[tuple[str, str, int]]:
    stmt = (
        select(ImportIdMapping.entity_type, ImportIdMapping.source_id, ImportIdMapping.target_id)
        .where(ImportIdMapping.source_system == system, ImportIdMapping.source_instance == instance)
        .order_by(ImportIdMapping.entity_type, ImportIdMapping.source_id)
    )
    return [tuple(row) for row in (await session.execute(stmt)).all()]


class TestScopes:
    async def test_list_scopes_counts_rows_per_entity_type(self, db_session):
        first, second = uuid.uuid4(), uuid.uuid4()
        old = ImportIdMap("scopetest", "postgresql://redmine:***@db/redmine", first)
        await old.put(db_session, EntityType.ISSUE, "1", "issues", 10)
        await old.put(db_session, EntityType.ISSUE, "2", "issues", 11)
        await ImportIdMap("scopetest", "postgresql://redmine:***@db/redmine", second).put(
            db_session, EntityType.USER, "5", "users", 3
        )
        await ImportIdMap("scopetest", "tracker.example.org", first).put(
            db_session, EntityType.PROJECT, "1", "projects", 4
        )

        scopes = {(s.source_system, s.source_instance): s for s in await list_scopes(db_session)}
        old_scope = scopes[("scopetest", "postgresql://redmine:***@db/redmine")]
        assert old_scope.counts == {"issue": 2, "user": 1}
        assert old_scope.total == 3
        assert old_scope.runs == 2
        assert old_scope.last_imported_at is not None
        assert scopes[("scopetest", "tracker.example.org")].counts == {"project": 1}


async def test_rename_scope_moves_rows_and_refuses_a_busy_target(db_session):
    """The way an operator moves a direct import's scope to the one its bundles will use."""
    old = ImportIdMap("redmine", "postgresql://redmine:***@db/redmine", uuid.uuid4())
    await old.put(db_session, EntityType.ISSUE, "1", "issues", 10)
    await old.put(db_session, EntityType.USER, "5", "users", 3)
    busy = ImportIdMap("redmine", "busy.example.org", uuid.uuid4())
    await busy.put(db_session, EntityType.ISSUE, "9", "issues", 20)
    moving = await _scope_rows(db_session, "redmine", old.source_instance)

    # A dry run counts and checks, and changes nothing.
    assert (
        await rename_scope(
            db_session,
            from_system="redmine",
            from_instance=old.source_instance,
            to_system="redmine",
            to_instance="tracker.example.org",
            dry_run=True,
        )
        == 2
    )
    assert await _scope_rows(db_session, "redmine", old.source_instance) == moving

    # A target that already holds rows is refused, and nothing moves.
    with pytest.raises(ScopeRenameError, match="already holds 1"):
        await rename_scope(
            db_session,
            from_system="redmine",
            from_instance=old.source_instance,
            to_system="redmine",
            to_instance="busy.example.org",
        )
    assert await _scope_rows(db_session, "redmine", old.source_instance) == moving

    moved = await rename_scope(
        db_session,
        from_system="redmine",
        from_instance=old.source_instance,
        to_system="redmine",
        to_instance="tracker.example.org",
    )

    assert moved == 2
    assert await _scope_rows(db_session, "redmine", old.source_instance) == []
    assert await _scope_rows(db_session, "redmine", "tracker.example.org") == moving
    # The renamed scope is what a later run sees.
    later = ImportIdMap("redmine", "tracker.example.org", uuid.uuid4())
    assert await later.get(db_session, EntityType.ISSUE, "1") == 10


class TestRenameScope:
    async def test_the_system_can_change_too(self, db_session):
        """A bundle loaded before bundles were keyed by their manifest sat under ``bundle``."""
        old = ImportIdMap("bundle", "redmine:tracker.example.org", uuid.uuid4())
        await old.put(db_session, EntityType.ISSUE, "1", "issues", 10)

        moved = await rename_scope(
            db_session,
            from_system="bundle",
            from_instance="redmine:tracker.example.org",
            to_system="redmine",
            to_instance="tracker.example.org",
        )

        assert moved == 1
        assert await _scope_rows(db_session, "redmine", "tracker.example.org") == [("issue", "1", 10)]

    async def test_an_empty_source_scope_is_refused(self, db_session):
        with pytest.raises(ScopeRenameError, match="no rows"):
            await rename_scope(
                db_session,
                from_system="redmine",
                from_instance="nowhere.example.org",
                to_system="redmine",
                to_instance="tracker.example.org",
            )

    async def test_renaming_a_scope_to_itself_is_refused(self, db_session):
        with pytest.raises(ScopeRenameError, match="same"):
            await rename_scope(
                db_session,
                from_system="redmine",
                from_instance="a",
                to_system="redmine",
                to_instance="a",
            )

    @pytest.mark.parametrize(
        ("system", "instance"),
        [("", "x"), ("Redmine", "x"), ("r" * 31, "x"), ("redmine", ""), ("redmine", "x" * 256)],
    )
    async def test_a_target_that_does_not_fit_the_columns_is_refused(self, db_session, system, instance):
        old = ImportIdMap("redmine", "old.example.org", uuid.uuid4())
        await old.put(db_session, EntityType.ISSUE, "1", "issues", 10)
        with pytest.raises(ScopeRenameError):
            await rename_scope(
                db_session,
                from_system="redmine",
                from_instance="old.example.org",
                to_system=system,
                to_instance=instance,
            )


async def test_purge_scope_deletes_one_scope_and_a_dry_run_only_counts(db_session):
    """After cut-over: the scope's rows go, every other scope's stay."""
    done = ImportIdMap("redmine", "tracker.example.org", uuid.uuid4())
    await done.put(db_session, EntityType.ISSUE, "1", "issues", 10)
    await done.put(db_session, EntityType.USER, "5", "users", 3)
    other = ImportIdMap("redmine", "other.example.org", uuid.uuid4())
    await other.put(db_session, EntityType.ISSUE, "1", "issues", 20)

    assert await purge_scope(db_session, "redmine", "tracker.example.org", dry_run=True) == 2
    assert len(await _scope_rows(db_session, "redmine", "tracker.example.org")) == 2

    assert await purge_scope(db_session, "redmine", "tracker.example.org") == 2
    assert await _scope_rows(db_session, "redmine", "tracker.example.org") == []
    assert len(await _scope_rows(db_session, "redmine", "other.example.org")) == 1
    assert await purge_scope(db_session, "redmine", "tracker.example.org") == 0
