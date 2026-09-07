"""``SourceAdapter`` protocol and adapter registry.

An adapter is the only component that knows a source system's schema. It
connects to that system and emits :mod:`specivo.importers.core.ir` objects;
everything downstream works off the IR alone.

Extraction methods return async iterators so a large instance streams rather
than materialising. Implementations should page their queries (keyset
pagination on the source primary key) instead of reading a whole table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from specivo.importers.core.ir import (
    IRAttachment,
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
    IRWatcher,
    IRWikiPage,
)


@runtime_checkable
class SourceAdapter(Protocol):
    """Reads one external tracker and emits IR.

    ``source_system`` is stored on every ``import_id_map`` row, so mappings from
    different systems can coexist in one Specivo database. ``source_instance``
    distinguishes two installations of the same system.
    """

    source_system: ClassVar[str]
    source_instance: str

    async def connect(self) -> None:
        """Open connections and validate the source is readable."""
        ...

    async def close(self) -> None:
        """Release connections. Safe to call when ``connect`` failed."""
        ...

    async def extract_lookups(self) -> IRLookups:
        """Return instance-wide lookups. Small, so returned whole."""
        ...

    def extract_users(self) -> AsyncIterator[IRUser]: ...

    def extract_groups(self) -> AsyncIterator[IRGroup]: ...

    def extract_projects(self) -> AsyncIterator[IRProject]:
        """Yield projects parents-first, so a child's parent is always mapped."""
        ...

    def extract_memberships(self, project_ref: str) -> AsyncIterator[IRMembership]: ...

    def extract_custom_fields(self) -> AsyncIterator[IRCustomField]: ...

    def extract_versions(self, project_ref: str) -> AsyncIterator[object]: ...

    def extract_categories(self, project_ref: str) -> AsyncIterator[object]: ...

    def extract_issues(self, project_ref: str) -> AsyncIterator[IRIssue]:
        """Yield issues parents-first within the project."""
        ...

    def extract_journals(self, project_ref: str) -> AsyncIterator[IRJournalEntry]:
        """Yield journal entries grouped by issue, oldest first."""
        ...

    def extract_relations(self, project_ref: str) -> AsyncIterator[IRRelation]: ...

    def extract_watchers(self, project_ref: str) -> AsyncIterator[IRWatcher]: ...

    def extract_attachments(self, project_ref: str) -> AsyncIterator[IRAttachment]: ...

    def extract_wiki_pages(self, project_ref: str) -> AsyncIterator[IRWikiPage]:
        """Yield wiki pages parents-first, each with its full history."""
        ...

    def extract_time_entries(self, project_ref: str) -> AsyncIterator[IRTimeEntry]: ...

    def resolve_attachment_path(self, attachment: IRAttachment) -> Path:
        """Return the on-disk path for *attachment*.

        Implementations must reject a resolved path that escapes the configured
        source directory: the locator comes from the source database and is not
        trusted input.
        """
        ...


class SourceAdapterRegistry:
    """Maps a source-system name to the adapter factory that handles it."""

    def __init__(self) -> None:
        self._factories: dict[str, type] = {}

    def register(self, name: str, factory: type) -> None:
        """Register *factory* under *name*, replacing any previous entry."""
        self._factories[name] = factory

    def get(self, name: str) -> type:
        """Return the factory registered for *name*.

        Raises ``KeyError`` with the known names when *name* is unregistered.
        """
        try:
            return self._factories[name]
        except KeyError:
            known = ", ".join(sorted(self._factories)) or "none"
            raise KeyError(f"Unknown import source '{name}'. Registered sources: {known}") from None

    def names(self) -> list[str]:
        """Return every registered source name, sorted."""
        return sorted(self._factories)


registry = SourceAdapterRegistry()
