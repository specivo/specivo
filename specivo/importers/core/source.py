"""``SourceAdapter`` protocol, its no-op base class and the adapter registry.

An adapter is the only component that knows a source system's schema. It
connects to that system and emits :mod:`specivo.importers.core.ir` objects;
everything downstream works off the IR alone.

Extraction methods return async iterators so a large instance streams rather
than materialising. Implementations should page their queries (keyset
pagination on the source primary key) instead of reading a whole table.

Adapters are found through :class:`SourceAdapterRegistry`. ``bundle`` is the
only source: an importer for another tracker is a separate tool that reads that
tracker and writes an import bundle (``docs/import-bundle/v1.md``).
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, runtime_checkable

from specivo.importers.core.converter import ContentConverter, PassthroughConverter
from specivo.importers.core.ir import (
    IRAttachment,
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


@runtime_checkable
class SourceAdapter(Protocol):
    """Reads one external tracker and emits IR.

    ``source_system`` is stored on every ``import_id_map`` row, so mappings from
    different systems can coexist in one Specivo database. ``source_instance``
    distinguishes two installations of the same system.

    ``credential_algorithms`` lists the password hasher ids the adapter may put
    in :attr:`IRUser.credential <specivo.importers.core.ir.IRUser.credential>`.
    An import with passwords checks every one of them is registered and enabled
    before it writes anything. Empty for an adapter that carries no passwords.
    """

    source_system: ClassVar[str]
    source_instance: str
    credential_algorithms: ClassVar[tuple[str, ...]]

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Add this source's own command-line options to *parser*.

        Called by the import command once ``--source`` has picked this adapter,
        so ``--source NAME --help`` lists them. Put them in an argument group
        named after the source; the generic run options are already there.
        """
        ...

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> SourceAdapter:
        """Build the adapter from the parsed command line.

        *args* holds this adapter's options and the generic ones, of which
        ``source_instance`` (``None`` unless given) and ``batch_size`` are the
        ones an adapter usually needs.
        """
        ...

    def content_converter(self) -> ContentConverter:
        """Return the converter that turns this source's markup into Markdown.

        Applied to issue descriptions, journal notes and wiki text. A source
        that already stores Markdown returns a :class:`PassthroughConverter`.
        """
        ...

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

    def extract_versions(self, project_ref: str) -> AsyncIterator[IRVersion]: ...

    def extract_categories(self, project_ref: str) -> AsyncIterator[IRCategory]: ...

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


async def _empty() -> AsyncIterator[Any]:
    """An async iterator that yields nothing."""
    return
    yield  # pragma: no cover - makes this an async generator


class BaseSourceAdapter:
    """A :class:`SourceAdapter` that has nothing to import until told otherwise.

    Every ``extract_*`` stream is empty, the lookups are empty, and
    ``connect``/``close`` do nothing, so a subclass overrides only what its
    source actually has. A source with users and issues needs ``extract_users``,
    ``extract_lookups`` (trackers, statuses and priorities), ``extract_projects``
    and ``extract_issues``; the phases for everything else find an empty stream
    and move on.

    Set ``source_system`` on the subclass. It names the source on the command
    line (``--source NAME``) and scopes the identifier map, so it has to be
    unique among the installed sources and stable once an import has run.
    """

    source_system: ClassVar[str]
    # One line for ``--list-sources`` and the command's help.
    description: ClassVar[str] = ""
    credential_algorithms: ClassVar[tuple[str, ...]] = ()

    def __init__(self, source_instance: str | None = None) -> None:
        # Two installations of the same system imported into one Specivo need
        # different instance names; a single one can use the system name.
        self.source_instance = source_instance or self.source_system

    @property
    def id_map_system(self) -> str:
        """The system the identifier map is keyed by: this source's own name.

        A source that carries another system's data, as a bundle does,
        overrides it with that system's name.
        """
        return self.source_system

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Add no options. Override to add the source's own."""

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> BaseSourceAdapter:
        """Build the adapter with only ``--source-instance``. Override to read more."""
        return cls(source_instance=getattr(args, "source_instance", None))

    def content_converter(self) -> ContentConverter:
        """Keep text as it is: the source is assumed to store Markdown or plain text."""
        return PassthroughConverter()

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def extract_lookups(self) -> IRLookups:
        return IRLookups()

    def extract_users(self) -> AsyncIterator[IRUser]:
        return _empty()

    def extract_groups(self) -> AsyncIterator[IRGroup]:
        return _empty()

    def extract_projects(self) -> AsyncIterator[IRProject]:
        return _empty()

    def extract_memberships(self, project_ref: str) -> AsyncIterator[IRMembership]:
        return _empty()

    def extract_custom_fields(self) -> AsyncIterator[IRCustomField]:
        return _empty()

    def extract_versions(self, project_ref: str) -> AsyncIterator[IRVersion]:
        return _empty()

    def extract_categories(self, project_ref: str) -> AsyncIterator[IRCategory]:
        return _empty()

    def extract_issues(self, project_ref: str) -> AsyncIterator[IRIssue]:
        return _empty()

    def extract_journals(self, project_ref: str) -> AsyncIterator[IRJournalEntry]:
        return _empty()

    def extract_relations(self, project_ref: str) -> AsyncIterator[IRRelation]:
        return _empty()

    def extract_watchers(self, project_ref: str) -> AsyncIterator[IRWatcher]:
        return _empty()

    def extract_attachments(self, project_ref: str) -> AsyncIterator[IRAttachment]:
        return _empty()

    def extract_wiki_pages(self, project_ref: str) -> AsyncIterator[IRWikiPage]:
        return _empty()

    def extract_time_entries(self, project_ref: str) -> AsyncIterator[IRTimeEntry]:
        return _empty()

    def resolve_attachment_path(self, attachment: IRAttachment) -> Path:
        """Refuse: a source that emits attachments must say where their files are.

        ``ValueError`` is what the attachment loader reports and skips, so an
        adapter that forgot this loses the file, not the import.
        """
        raise ValueError(f"The {self.source_system} source does not provide attachment files")


def id_map_system(adapter: object) -> str:
    """Return the system *adapter*'s rows go under in the identifier map.

    Its ``id_map_system`` when it has one, else its ``source_system``: an
    adapter written to the protocol alone need not know about the former.
    """
    system = getattr(adapter, "id_map_system", None)
    return system if isinstance(system, str) and system else str(adapter.source_system)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class UnknownSourceError(KeyError):
    """No import source is registered under the requested name."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else super().__str__()


class SourceAdapterRegistry:
    """Maps a source name to the adapter class that handles it.

    Built once per process by :func:`build_source_registry`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, type[Any]] = {}

    def register(self, name: str, factory: type[Any]) -> None:
        """Register *factory* under *name*."""
        self._entries[name] = factory

    def get(self, name: str) -> type[Any]:
        """Return the adapter class registered for *name*.

        Raises :class:`UnknownSourceError` naming the known sources when *name*
        is unregistered.
        """
        try:
            return self._entries[name]
        except KeyError:
            known = ", ".join(self.names()) or "none"
            raise UnknownSourceError(f"Unknown import source '{name}'. Registered sources: {known}") from None

    def descriptions(self) -> Mapping[str, str]:
        """Return each source's one-line description by name (read-only)."""
        return MappingProxyType(
            {name: str(getattr(factory, "description", "") or "") for name, factory in self._entries.items()}
        )

    def names(self) -> list[str]:
        """Return every registered source name, sorted."""
        return sorted(self._entries)


def build_source_registry() -> SourceAdapterRegistry:
    """Build the registry. ``bundle`` is the only source: importers write bundles."""
    from specivo.importers.bundle.adapter import BundleSourceAdapter

    registry = SourceAdapterRegistry()
    registry.register(BundleSourceAdapter.source_system, BundleSourceAdapter)
    return registry


_registry: SourceAdapterRegistry | None = None


def get_source_registry() -> SourceAdapterRegistry:
    """Return the process-wide registry, built on first use."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = build_source_registry()
    return _registry
