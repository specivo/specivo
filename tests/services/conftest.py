"""Shared fixtures for importer loader tests.

Loaders are phase handlers: they take a ``PhaseContext`` and write through the
real service and model layers. These fixtures assemble a context around the
rollback-isolated test session and a fake adapter, so a loader can be exercised
with hand-built IR and no source system anywhere in sight.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import IRGroup, IRLookups, IRUser
from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportSummary, PhaseContext
from specivo.importers.core.progress import NullProgressReporter


class FakeAdapter:
    """Serves pre-built IR instead of reading a source system."""

    source_system = "redmine"

    def __init__(
        self,
        lookups: IRLookups | None = None,
        users: list[IRUser] | None = None,
        groups: list[IRGroup] | None = None,
    ) -> None:
        self.source_instance = "tracker.example.org"
        self.lookups = lookups or IRLookups()
        self.users = users or []
        self.groups = groups or []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def extract_lookups(self) -> IRLookups:
        return self.lookups

    async def extract_users(self) -> AsyncIterator[IRUser]:
        for user in self.users:
            yield user

    async def extract_groups(self) -> AsyncIterator[IRGroup]:
        for group in self.groups:
            yield group


@pytest.fixture
def make_context(db_session: AsyncSession) -> Callable[..., PhaseContext]:
    """Return a factory building a PhaseContext around the test session."""

    def _make(
        adapter: FakeAdapter | None = None,
        phase: ImportPhase = ImportPhase.LOOKUPS,
        **option_kwargs,
    ) -> PhaseContext:
        adapter = adapter or FakeAdapter()
        run_id = uuid.uuid4()
        options = ImportOptions(source_instance=adapter.source_instance, **option_kwargs)
        summary = ImportSummary(
            run_id=run_id,
            source_system=adapter.source_system,
            source_instance=adapter.source_instance,
            dry_run=options.dry_run,
        )
        return PhaseContext(
            phase=phase,
            session=db_session,
            adapter=adapter,
            options=options,
            summary=summary,
            reporter=NullProgressReporter(),
            id_map=ImportIdMap(adapter.source_system, adapter.source_instance, run_id),
        )

    return _make
