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

from specivo.importers.core.id_map import ImportIdMap, MissingMappingError
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
