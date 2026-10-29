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
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.import_id_map import ImportIdMapping

logger = logging.getLogger(__name__)


class MissingMappingError(LookupError):
    """A mapping that a loader required does not exist.

    Raised only by :meth:`ImportIdMap.require`. Optional references use
    :meth:`ImportIdMap.get` and handle ``None`` themselves.
    """


class ScopeRenameError(ValueError):
    """A scope could not be renamed; nothing was changed."""


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

    async def written_by_run(self, session: AsyncSession, entity_type: str) -> set[str]:
        """Return the source ids of *entity_type* this run mapped: what it created, not what it found.

        A resumed run keeps its run id, so this includes what the attempt it
        continues created.
        """
        stmt = select(ImportIdMapping.source_id).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
            ImportIdMapping.entity_type == str(entity_type),
            ImportIdMapping.import_run_id == self.run_id,
        )
        return set((await session.execute(stmt)).scalars())

    async def count(self, session: AsyncSession, entity_type: str | None = None) -> int:
        """Count mappings for this source, optionally narrowed to one entity type."""
        stmt = select(func.count()).where(
            ImportIdMapping.source_system == self.source_system,
            ImportIdMapping.source_instance == self.source_instance,
        )
        if entity_type is not None:
            stmt = stmt.where(ImportIdMapping.entity_type == str(entity_type))
        return int((await session.execute(stmt)).scalar_one())

    async def counts(self, session: AsyncSession) -> dict[str, int]:
        """Count mappings for this source per entity type."""
        stmt = (
            select(ImportIdMapping.entity_type, func.count())
            .where(
                ImportIdMapping.source_system == self.source_system,
                ImportIdMapping.source_instance == self.source_instance,
            )
            .group_by(ImportIdMapping.entity_type)
        )
        return {entity_type: int(count) for entity_type, count in (await session.execute(stmt)).all()}

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


# ---------------------------------------------------------------------------
# Scopes: inspecting and moving whole imports
# ---------------------------------------------------------------------------

# What a source system may be called: a bundle manifest's rule, which fits the
# column (30 characters) and the import account's login.
SOURCE_SYSTEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
SOURCE_INSTANCE_MAX = 255


@dataclass(frozen=True, slots=True)
class IdMapScope:
    """One ``(source_system, source_instance)`` scope and what it holds."""

    source_system: str
    source_instance: str
    counts: dict[str, int] = field(default_factory=dict)
    runs: int = 0
    last_imported_at: datetime | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def list_scopes(session: AsyncSession) -> list[IdMapScope]:
    """Return every scope in the identifier map, with its rows counted per entity type.

    ``runs`` counts the import runs that wrote rows to the scope, and
    ``last_imported_at`` is when the newest of them was written.
    """
    per_type = (
        select(
            ImportIdMapping.source_system,
            ImportIdMapping.source_instance,
            ImportIdMapping.entity_type,
            func.count(),
        )
        .group_by(ImportIdMapping.source_system, ImportIdMapping.source_instance, ImportIdMapping.entity_type)
        .order_by(ImportIdMapping.source_system, ImportIdMapping.source_instance, ImportIdMapping.entity_type)
    )
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for system, instance, entity_type, count in (await session.execute(per_type)).all():
        counts.setdefault((system, instance), {})[entity_type] = int(count)

    per_scope = select(
        ImportIdMapping.source_system,
        ImportIdMapping.source_instance,
        func.count(func.distinct(ImportIdMapping.import_run_id)),
        func.max(ImportIdMapping.created_at),
    ).group_by(ImportIdMapping.source_system, ImportIdMapping.source_instance)
    runs = {
        (system, instance): (int(n), last) for system, instance, n, last in (await session.execute(per_scope)).all()
    }

    return [
        IdMapScope(system, instance, by_type, *runs.get((system, instance), (0, None)))
        for (system, instance), by_type in counts.items()
    ]


async def _scope_count(session: AsyncSession, system: str, instance: str) -> int:
    stmt = select(func.count()).where(
        ImportIdMapping.source_system == system,
        ImportIdMapping.source_instance == instance,
    )
    return int((await session.execute(stmt)).scalar_one())


async def rename_scope(
    session: AsyncSession,
    *,
    from_system: str,
    from_instance: str,
    to_system: str,
    to_instance: str,
    dry_run: bool = False,
) -> int:
    """Move every row of one scope to another, and return how many moved.

    For an operator whose earlier import was keyed differently from the one
    that comes next (an import keyed by a masked database URL, say, before a
    bundle names the installation): once the rows are
    under the new key, the next import finds what is already there.

    Refuses, with :class:`ScopeRenameError` and nothing changed, a target that
    does not fit the columns, the same scope twice, an empty source scope and
    a target scope that already holds rows (merging two imports' maps could
    point one source id at two rows). With *dry_run* it checks and counts
    only. Runs in the caller's transaction, which the caller commits.
    """
    if not SOURCE_SYSTEM_RE.match(to_system or ""):
        raise ScopeRenameError(
            f"{to_system!r} is not a valid source system: lower case letters, digits and _, up to 30 characters"
        )
    if not to_instance or len(to_instance) > SOURCE_INSTANCE_MAX:
        raise ScopeRenameError(f"The source instance must be 1 to {SOURCE_INSTANCE_MAX} characters long")
    if (from_system, from_instance) == (to_system, to_instance):
        raise ScopeRenameError("The old and the new scope are the same")

    moving = await _scope_count(session, from_system, from_instance)
    if not moving:
        raise ScopeRenameError(f"The scope {from_system} @ {from_instance!r} has no rows; nothing to rename")
    occupied = await _scope_count(session, to_system, to_instance)
    if occupied:
        raise ScopeRenameError(
            f"The scope {to_system} @ {to_instance!r} already holds {occupied} row(s); "
            "rename it out of the way first or choose another name"
        )
    if dry_run:
        return moving

    result = await session.execute(
        update(ImportIdMapping)
        .where(
            ImportIdMapping.source_system == from_system,
            ImportIdMapping.source_instance == from_instance,
        )
        .values(source_system=to_system, source_instance=to_instance)
        .execution_options(synchronize_session=False)
    )
    moved = int(result.rowcount or 0)  # type: ignore[attr-defined]
    logger.info(
        "Moved %d identifier-map rows from %s @ %s to %s @ %s",
        moved,
        from_system,
        from_instance,
        to_system,
        to_instance,
    )
    return moved


async def purge_scope(session: AsyncSession, system: str, instance: str, *, dry_run: bool = False) -> int:
    """Delete every row of one scope, and return how many there were.

    For after cut-over, when the source will not be imported again: the rows
    are what let a later import skip what is already here, so once they are
    gone a delta import of that source is impossible. The imported records
    themselves are untouched. With *dry_run* it only counts. Runs in the
    caller's transaction, which the caller commits.
    """
    count = await _scope_count(session, system, instance)
    if dry_run or not count:
        return count
    await session.execute(
        delete(ImportIdMapping)
        .where(ImportIdMapping.source_system == system, ImportIdMapping.source_instance == instance)
        .execution_options(synchronize_session=False)
    )
    logger.info("Deleted %d identifier-map rows of %s @ %s", count, system, instance)
    return count
