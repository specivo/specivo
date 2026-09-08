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
from specivo.importers.core.ir import (
    IRCategory,
    IRCustomField,
    IRGroup,
    IRIssue,
    IRJournalEntry,
    IRLookups,
    IRMembership,
    IRProject,
    IRRelation,
    IRTimeEntry,
    IRUser,
    IRVersion,
    IRWatcher,
    IRWikiPage,
)
from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportSummary, PhaseContext
from specivo.importers.core.progress import NullProgressReporter


class FakeAdapter:
    """Serves pre-built IR instead of reading a source system.

    ``source_system`` is settable because it decides the login of the import
    service account. Test modules run in parallel, and two of them inserting
    the same login in uncommitted transactions block on the unique index, so
    each module uses its own.
    """

    def __init__(
        self,
        lookups: IRLookups | None = None,
        users: list[IRUser] | None = None,
        groups: list[IRGroup] | None = None,
        projects: list[IRProject] | None = None,
        versions: list[IRVersion] | None = None,
        categories: list[IRCategory] | None = None,
        memberships: list[IRMembership] | None = None,
        custom_fields: list[IRCustomField] | None = None,
        issues: list[IRIssue] | None = None,
        journals: list[IRJournalEntry] | None = None,
        relations: list[IRRelation] | None = None,
        watchers: list[IRWatcher] | None = None,
        wiki_pages: list[IRWikiPage] | None = None,
        wiki_watchers: list[IRWatcher] | None = None,
        wiki_redirects: list[tuple[str, str]] | None = None,
        time_entries: list[IRTimeEntry] | None = None,
        dropped: dict[str, list[str]] | None = None,
        source_format: str = "textile",
        source_system: str = "redmine",
    ) -> None:
        self.source_system = source_system
        self.source_instance = "tracker.example.org"
        self.lookups = lookups or IRLookups()
        self.users = users or []
        self.groups = groups or []
        self.projects = projects or []
        self.versions = versions or []
        self.categories = categories or []
        self.memberships = memberships or []
        self.custom_fields = custom_fields or []
        self.issues = issues or []
        self.journals = journals or []
        self.relations = relations or []
        self.watchers = watchers or []
        self.wiki_pages = wiki_pages or []
        self.wiki_watchers = wiki_watchers or []
        self.wiki_redirects = wiki_redirects or []
        self.time_entries = time_entries or []
        self._dropped = dropped or {}
        self.source_format = source_format

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

    async def extract_projects(self) -> AsyncIterator[IRProject]:
        for project in self.projects:
            yield project

    def dropped_modules(self, project_ref: str) -> list[str]:
        return self._dropped.get(project_ref, [])

    async def extract_versions(self, project_ref: str) -> AsyncIterator[IRVersion]:
        for version in self.versions:
            if version.project_ref == project_ref:
                yield version

    async def extract_categories(self, project_ref: str) -> AsyncIterator[IRCategory]:
        for category in self.categories:
            if category.project_ref == project_ref:
                yield category

    async def extract_memberships(self, project_ref: str) -> AsyncIterator[IRMembership]:
        for membership in self.memberships:
            if membership.project_ref == project_ref:
                yield membership

    async def extract_custom_fields(self) -> AsyncIterator[IRCustomField]:
        for field in self.custom_fields:
            yield field

    async def extract_issues(self, project_ref: str) -> AsyncIterator[IRIssue]:
        """Yield parents before children, as the real adapter guarantees."""
        from specivo.importers.redmine.extract import order_parents_first

        in_project = {issue.source_ref: issue for issue in self.issues if issue.project_ref == project_ref}
        parents = {ref: issue.parent_ref for ref, issue in in_project.items()}
        for ref in order_parents_first(parents):
            yield in_project[ref]

    async def extract_journals(self, project_ref: str) -> AsyncIterator[IRJournalEntry]:
        refs = {issue.source_ref for issue in self.issues if issue.project_ref == project_ref}
        for journal in self.journals:
            if journal.issue_ref in refs:
                yield journal

    async def extract_relations(self, project_ref: str) -> AsyncIterator[IRRelation]:
        refs = {issue.source_ref for issue in self.issues if issue.project_ref == project_ref}
        for relation in self.relations:
            if relation.from_ref in refs or relation.to_ref in refs:
                yield relation

    async def extract_watchers(self, project_ref: str) -> AsyncIterator[IRWatcher]:
        refs = {issue.source_ref for issue in self.issues if issue.project_ref == project_ref}
        for watcher in self.watchers:
            if watcher.container_ref in refs:
                yield watcher

    async def extract_wiki_pages(self, project_ref: str) -> AsyncIterator[IRWikiPage]:
        """Yield parents before children, as the real adapter guarantees."""
        from specivo.importers.redmine.extract import order_parents_first

        in_project = {page.source_ref: page for page in self.wiki_pages if page.project_ref == project_ref}
        parents = {ref: page.parent_ref for ref, page in in_project.items()}
        for ref in order_parents_first(parents):
            yield in_project[ref]

    async def extract_wiki_watchers(self, project_ref: str) -> AsyncIterator[IRWatcher]:
        refs = {page.source_ref for page in self.wiki_pages if page.project_ref == project_ref}
        for watcher in self.wiki_watchers:
            if watcher.container_ref in refs:
                yield watcher

    async def extract_wiki_redirects(self, project_ref: str) -> AsyncIterator[tuple[str, str]]:
        for pair in self.wiki_redirects:
            yield pair

    async def extract_time_entries(self, project_ref: str) -> AsyncIterator[IRTimeEntry]:
        for entry in self.time_entries:
            if entry.project_ref == project_ref:
                yield entry


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
