"""Phase pipeline that drives an import from end to end.

The pipeline owns three things: the order phases run in, the transaction each
phase runs in, and the summary the operator reads afterwards. It knows nothing
about any specific source or target entity — loaders register themselves
against a phase and the pipeline calls them.

**Ordering.** Phases run in the order declared by :class:`ImportPhase`, which
is a dependency order: an entity is never loaded before the entities it points
at. Relations and the issue-reference rewrite come late because they need every
issue in the run, not just the current project's.

**Transactions.** A normal run uses one session per phase and commits at the
end of it, so a failure in a late phase never discards verified earlier work
and ``--resume`` can pick up from there. A dry run instead shares a single
session across all phases and always rolls it back, which exercises the real
write path while guaranteeing nothing is persisted.

**Unregistered phases are skipped.** That keeps the pipeline usable while
loaders are still being written, and lets a source that has no wiki simply not
register the wiki phases.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.progress import NullProgressReporter, ProgressReporter
from specivo.importers.core.source import SourceAdapter

logger = logging.getLogger(__name__)


class ImportPhase(StrEnum):
    """Import phases in dependency order.

    Declaration order is execution order — :data:`PHASE_ORDER` is derived from
    it, so inserting a phase here is all that is needed to schedule it.
    """

    BOOTSTRAP = "bootstrap"
    LOOKUPS = "lookups"
    USERS = "users"
    PROJECTS = "projects"
    PROJECT_LOOKUPS = "project_lookups"
    MEMBERSHIPS = "memberships"
    CUSTOM_FIELD_SCHEMAS = "custom_field_schemas"
    ISSUES = "issues"
    WATCHERS = "watchers"
    CUSTOM_FIELD_VALUES = "custom_field_values"
    JOURNALS = "journals"
    RELATIONS = "relations"
    ISSUE_ATTACHMENTS = "issue_attachments"
    ISSUE_REF_REWRITE = "issue_ref_rewrite"
    WIKI_PAGES = "wiki_pages"
    WIKI_ATTACHMENTS = "wiki_attachments"
    TIME_ENTRIES = "time_entries"
    WIKI_LINK_GRAPH = "wiki_link_graph"
    SEARCH_BACKFILL = "search_backfill"


PHASE_ORDER: tuple[ImportPhase, ...] = tuple(ImportPhase)


@dataclass(slots=True)
class ImportOptions:
    """Operator-supplied settings for one import run.

    Most fields come straight from CLI flags. The mapping overrides exist
    because a few source concepts cannot be translated automatically and the
    operator needs the final say.
    """

    source_instance: str
    dry_run: bool = False
    project_refs: tuple[str, ...] | None = None
    resume_run_id: uuid.UUID | None = None
    stop_after_phase: ImportPhase | None = None
    batch_size: int = 500
    strict: bool = False
    dispatch_celery: bool = False
    project_key_map: dict[str, str] = field(default_factory=dict)
    status_category_map: dict[str, str] = field(default_factory=dict)
    cf_key_map: dict[str, str] = field(default_factory=dict)
    flatten_excess_depth: bool = False
    merge_duplicate_statuses: bool = False


@dataclass(slots=True)
class ImportWarning:
    """Something the operator should look at, which did not stop the import."""

    phase: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable form."""
        return {"phase": self.phase, "message": self.message, "context": self.context}


@dataclass
class ImportSummary:
    """What the run did: counts, warnings, and things needing follow-up."""

    run_id: uuid.UUID
    source_system: str
    source_instance: str
    dry_run: bool
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    created: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    reused: Counter[str] = field(default_factory=Counter)
    phases_run: list[str] = field(default_factory=list)
    warnings: list[ImportWarning] = field(default_factory=list)
    notes: dict[str, list[str]] = field(default_factory=dict)

    def record_created(self, entity_type: str, count: int = 1) -> None:
        """Count newly created rows of *entity_type*."""
        self.created[str(entity_type)] += count

    def record_skipped(self, entity_type: str, count: int = 1) -> None:
        """Count rows skipped because they were already imported."""
        self.skipped[str(entity_type)] += count

    def record_reused(self, entity_type: str, count: int = 1) -> None:
        """Count source entities matched onto a row Specivo already had.

        Distinct from skipped: nothing was created, but the source entity was
        mapped rather than passed over.
        """
        self.reused[str(entity_type)] += count

    def add_warning(self, phase: str, message: str, context: dict[str, Any] | None = None) -> None:
        """Append a warning for the final report."""
        self.warnings.append(ImportWarning(phase=str(phase), message=message, context=context or {}))

    def add_note(self, category: str, item: str) -> None:
        """Append *item* to a named follow-up list (e.g. logins needing a reset)."""
        self.notes.setdefault(category, []).append(item)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable form for ``--report-json``."""
        return {
            "run_id": str(self.run_id),
            "source_system": self.source_system,
            "source_instance": self.source_instance,
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created": dict(sorted(self.created.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "reused": dict(sorted(self.reused.items())),
            "phases_run": list(self.phases_run),
            "warnings": [w.as_dict() for w in self.warnings],
            "notes": {k: list(v) for k, v in sorted(self.notes.items())},
        }

    def format_text(self) -> str:
        """Return the human-readable report printed at the end of a run."""
        lines: list[str] = []
        mode = "DRY RUN (nothing was written)" if self.dry_run else "IMPORT"
        lines.append(f"{mode} — {self.source_system} @ {self.source_instance}")
        lines.append(f"run id: {self.run_id}")
        if self.created:
            lines.append("")
            lines.append("Created:")
            for name, count in sorted(self.created.items()):
                lines.append(f"  {name:<20} {count}")
        if self.reused:
            lines.append("")
            lines.append("Matched onto existing rows:")
            for name, count in sorted(self.reused.items()):
                lines.append(f"  {name:<20} {count}")
        if self.skipped:
            lines.append("")
            lines.append("Already imported (skipped):")
            for name, count in sorted(self.skipped.items()):
                lines.append(f"  {name:<20} {count}")
        for category, items in sorted(self.notes.items()):
            lines.append("")
            lines.append(f"{category} ({len(items)}):")
            lines.extend(f"  {item}" for item in items)
        if self.warnings:
            lines.append("")
            lines.append(f"Warnings ({len(self.warnings)}):")
            for warning in self.warnings:
                detail = ", ".join(f"{k}={v}" for k, v in sorted(warning.context.items()))
                suffix = f" ({detail})" if detail else ""
                lines.append(f"  [{warning.phase}] {warning.message}{suffix}")
        return "\n".join(lines)


@dataclass(slots=True)
class PhaseContext:
    """Everything a phase handler is given.

    ``project_refs`` is the resolved scope for the run. The projects phase fills
    it, so later phases iterate exactly the projects that were imported rather
    than re-deriving the scope.

    ``state`` is a scratch space carried between phases for anything one phase
    works out and a later one needs — group membership, for instance, which is
    read with the users but only applied when projects exist.
    """

    phase: ImportPhase
    session: AsyncSession
    adapter: SourceAdapter
    options: ImportOptions
    summary: ImportSummary
    reporter: ProgressReporter
    id_map: ImportIdMap
    project_refs: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def warn(self, message: str, **context: Any) -> None:
        """Record a warning on both the reporter and the summary.

        Raises ``ImportError`` instead when the run is strict, turning anything
        skippable into a hard failure — used by CI and the test suite.
        """
        if self.options.strict:
            raise PhaseFailedError(f"[{self.phase}] {message} {context}".strip())
        self.reporter.warning(message, {"phase": str(self.phase), **context})
        self.summary.add_warning(str(self.phase), message, context)

    def tick(self, count: int = 1) -> None:
        """Report *count* processed items for the current phase."""
        self.reporter.item_done(str(self.phase), count)


class PhaseFailedError(RuntimeError):
    """A phase could not complete. Earlier committed phases are unaffected."""


PhaseHandler = Callable[[PhaseContext], Awaitable[None]]


class ImportPipeline:
    """Runs registered phase handlers in dependency order."""

    def __init__(
        self,
        adapter: SourceAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        options: ImportOptions,
        reporter: ProgressReporter | None = None,
    ) -> None:
        self._adapter = adapter
        self._session_factory = session_factory
        self._options = options
        self._reporter = reporter or NullProgressReporter()
        self._handlers: dict[ImportPhase, list[PhaseHandler]] = {}

    def register(self, phase: ImportPhase, handler: PhaseHandler) -> None:
        """Add *handler* to *phase*. Handlers run in registration order."""
        self._handlers.setdefault(phase, []).append(handler)

    def register_all(self, handlers: dict[ImportPhase, PhaseHandler]) -> None:
        """Register one handler per phase from a mapping."""
        for phase, handler in handlers.items():
            self.register(phase, handler)

    def registered_phases(self) -> list[ImportPhase]:
        """Return the phases that have at least one handler, in run order."""
        return [phase for phase in PHASE_ORDER if self._handlers.get(phase)]

    async def run(self) -> ImportSummary:
        """Execute every registered phase and return the summary.

        The adapter is connected before the first phase and closed afterwards,
        including when a phase raises.
        """
        summary = ImportSummary(
            run_id=self._options.resume_run_id or uuid.uuid4(),
            source_system=self._adapter.source_system,
            source_instance=self._options.source_instance,
            dry_run=self._options.dry_run,
        )

        # One map for the whole run: it caches lookups across the per-phase
        # sessions, and its identity is what makes a resumed run recognise the
        # rows an earlier attempt wrote.
        id_map = ImportIdMap(
            source_system=self._adapter.source_system,
            source_instance=self._options.source_instance,
            run_id=summary.run_id,
        )

        await self._adapter.connect()
        try:
            if self._options.dry_run:
                # One transaction for the whole run, rolled back unconditionally:
                # a dry run must exercise the real writes and persist none of them.
                async with self._session_factory() as shared:
                    try:
                        await self._run_phases(summary, shared, id_map)
                    finally:
                        await shared.rollback()
            else:
                await self._run_phases(summary, None, id_map)
        finally:
            await self._adapter.close()
            summary.finished_at = datetime.now(UTC)

        return summary

    async def _run_phases(self, summary: ImportSummary, shared: AsyncSession | None, id_map: ImportIdMap) -> None:
        """Run each registered phase, committing per phase unless sharing a session."""
        project_refs: list[str] = list(self._options.project_refs or [])
        state: dict[str, Any] = {}

        for phase in PHASE_ORDER:
            handlers = self._handlers.get(phase)
            if not handlers:
                logger.debug("Phase %s has no handler; skipping", phase)
                continue

            self._reporter.phase_started(str(phase))
            async with self._phase_session(shared) as session:
                context = PhaseContext(
                    phase=phase,
                    session=session,
                    adapter=self._adapter,
                    options=self._options,
                    summary=summary,
                    reporter=self._reporter,
                    id_map=id_map,
                    project_refs=project_refs,
                    state=state,
                )
                for handler in handlers:
                    await handler(context)
                # A handler may narrow or discover the project scope (the
                # projects phase does), so carry its view into later phases.
                project_refs = context.project_refs
                state = context.state

            summary.phases_run.append(str(phase))
            self._reporter.phase_done(str(phase))

            if self._options.stop_after_phase is not None and phase == self._options.stop_after_phase:
                logger.info("Stopping after phase %s as requested", phase)
                break

    @asynccontextmanager
    async def _phase_session(self, shared: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        """Yield the session for one phase.

        With a shared session (dry run) the caller owns the rollback. Otherwise
        the phase gets its own session and commits on success.
        """
        if shared is not None:
            yield shared
            return

        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
