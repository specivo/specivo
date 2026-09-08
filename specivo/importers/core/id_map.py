"""Persistent source-id to Specivo-id mapping.

Every loader asks this before it creates anything and records the result
afterwards, which is what makes an import idempotent: re-running one that died
half way skips what already landed instead of duplicating it.

The mapping lives in a table rather than a state file so that a mapping and the
row it describes are written in the same transaction. A file could not be kept
consistent with a database that rolls back.

Lookups are cached in memory per entity type. A phase that resolves thousands
of author or project references would otherwise issue one query per row. The
cache is only ever added to, and a failed phase aborts the run, so it cannot
serve a mapping whose row was rolled back.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.import_id_map import ImportIdMapping

logger = logging.getLogger(__name__)


class MissingMappingError(LookupError):
    """A mapping that a loader required does not exist.

    Raised only by :meth:`ImportIdMap.require`. Optional references use
    :meth:`ImportIdMap.get` and handle ``None`` themselves.
    """


class ImportIdMap:
    """Reads and writes ``import_id_map`` rows for one import run.

    Stateless with respect to the database session — like the rest of the
    service layer, each method takes the session to use, so the same instance
    works across the per-phase sessions the pipeline hands out.
    """

    def __init__(self, source_system: str, source_instance: str, run_id: uuid.UUID) -> None:
        self.source_system = source_system
        self.source_instance = source_instance
        self.run_id = run_id
        self._cache: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def cached(self, entity_type: str, source_id: str) -> int | None:
        """Return a mapping already in memory, without touching the database."""
        return self._cache.get(str(entity_type), {}).get(str(source_id))

    async def get(self, session: AsyncSession, entity_type: str, source_id: str | None) -> int | None:
        """Return the Specivo id for *source_id*, or ``None`` if unmapped.

        A ``None`` *source_id* returns ``None``, so callers can pass an optional
        reference straight through without a guard.
        """
        if source_id is None:
            return None

        entity_type, source_id = str(entity_type), str(source_id)
        hit = self.cached(entity_type, source_id)
        if hit is not None:
            return hit

        stmt = select(ImportIdMapping.target_id).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
            ImportIdMapping.entity_type == entity_type,
            ImportIdMapping.source_id == source_id,
        )
        target_id = (await session.execute(stmt)).scalar_one_or_none()
        if target_id is not None:
            self._remember(entity_type, source_id, target_id)
        return target_id

    async def require(self, session: AsyncSession, entity_type: str, source_id: str) -> int:
        """Return the Specivo id for *source_id* or raise :class:`MissingMappingError`.

        For references that cannot be null, where a missing mapping means the
        phases ran out of order rather than that the source data was sparse.
        """
        target_id = await self.get(session, entity_type, source_id)
        if target_id is None:
            raise MissingMappingError(
                f"No {entity_type} mapping for source id {source_id!r} — is a phase out of order?"
            )
        return target_id

    async def get_many(
        self,
        session: AsyncSession,
        entity_type: str,
        source_ids: Iterable[str],
    ) -> dict[str, int]:
        """Resolve several ids at once, returning only those that are mapped.

        One query for the whole batch, so a phase can resolve a page of
        references without a query per row.
        """
        entity_type = str(entity_type)
        wanted = {str(source_id) for source_id in source_ids}
        if not wanted:
            return {}

        resolved = {sid: hit for sid in wanted if (hit := self.cached(entity_type, sid)) is not None}
        missing = wanted - resolved.keys()
        if not missing:
            return resolved

        stmt = select(ImportIdMapping.source_id, ImportIdMapping.target_id).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
            ImportIdMapping.entity_type == entity_type,
            ImportIdMapping.source_id.in_(missing),
        )
        for source_id, target_id in (await session.execute(stmt)).all():
            self._remember(entity_type, source_id, target_id)
            resolved[source_id] = target_id
        return resolved

    async def preload(self, session: AsyncSession, entity_type: str) -> dict[str, int]:
        """Load every mapping for *entity_type* into the cache and return it.

        Worth doing at the start of a phase that resolves the same small set of
        references over and over (statuses, trackers, users).
        """
        entity_type = str(entity_type)
        stmt = select(ImportIdMapping.source_id, ImportIdMapping.target_id).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
            ImportIdMapping.entity_type == entity_type,
        )
        rows = (await session.execute(stmt)).all()
        for source_id, target_id in rows:
            self._remember(entity_type, source_id, target_id)
        logger.debug("Preloaded %d %s mappings", len(rows), entity_type)
        return dict(self._cache.get(entity_type, {}))

    async def count(self, session: AsyncSession, entity_type: str | None = None) -> int:
        """Count mappings for this source, optionally narrowed to one entity type."""
        stmt = select(ImportIdMapping.id).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
        )
        if entity_type is not None:
            stmt = stmt.where(ImportIdMapping.entity_type == str(entity_type))
        return len((await session.execute(stmt)).all())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def put(
        self,
        session: AsyncSession,
        entity_type: str,
        source_id: str,
        target_table: str,
        target_id: int,
    ) -> int:
        """Record that *source_id* became row *target_id*, and return the effective id.

        An existing mapping always wins: if this source entity was already
        imported, the row it points at is returned and nothing is overwritten.
        That keeps a resumed run from re-pointing references at a duplicate row.
        """
        entity_type, source_id = str(entity_type), str(source_id)
        stmt = (
            pg_insert(ImportIdMapping)
            .values(
                import_run_id=self.run_id,
                source_system=self.source_system,
                source_instance=self.source_instance,
                entity_type=entity_type,
                source_id=source_id,
                target_table=target_table,
                target_id=target_id,
            )
            .on_conflict_do_nothing(constraint="uq_import_id_map_source")
            .returning(ImportIdMapping.target_id)
        )
        inserted = (await session.execute(stmt)).scalar_one_or_none()

        if inserted is None:
            # Lost to an existing row: read it back so the caller and the cache
            # agree on which Specivo row this source entity maps to.
            existing = await self._fetch(session, entity_type, source_id)
            if existing is None:  # pragma: no cover - only on a concurrent delete
                raise MissingMappingError(f"Mapping for {entity_type} {source_id!r} vanished between insert and read")
            self._remember(entity_type, source_id, existing)
            return existing

        self._remember(entity_type, source_id, inserted)
        return inserted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _remember(self, entity_type: str, source_id: str, target_id: int) -> None:
        self._cache.setdefault(entity_type, {})[source_id] = target_id

    async def _fetch(self, session: AsyncSession, entity_type: str, source_id: str) -> int | None:
        stmt = select(ImportIdMapping.target_id).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
            ImportIdMapping.entity_type == entity_type,
            ImportIdMapping.source_id == source_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()
