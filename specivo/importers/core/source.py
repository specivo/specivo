"""``SourceAdapter`` protocol, its no-op base class and the adapter registry.

An adapter is the only component that knows a source system's schema. It
connects to that system and emits :mod:`specivo.importers.core.ir` objects;
everything downstream works off the IR alone.

Extraction methods return async iterators so a large instance streams rather
than materialising. Implementations should page their queries (keyset
pagination on the source primary key) instead of reading a whole table.

Adapters are found through :class:`SourceAdapterRegistry`. The built-in
``bundle`` and Redmine adapters are always there; a plugin adds its own with ``get_import_sources()``,
and :func:`specivo.core.plugin_manager.bootstrap` builds the registry from
both, refusing a duplicate or reserved name.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# The name is typed on the command line and stored in import_id_map.source_system
# (30 characters), and becomes part of the import account's login.
SOURCE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,29}$")

# Names only a built-in adapter may use, so a plugin cannot take over the
# identifier map of an import that already ran.
RESERVED_SOURCE_NAMES: frozenset[str] = frozenset({"bundle", "redmine"})

ORIGIN_CORE = "core"


def plugin_origin(plugin_name: str) -> str:
    """Return the origin label for a source contributed by *plugin_name*."""
    return f"plugin:{plugin_name}"


class SourceRegistrationError(ValueError):
    """An import source has an invalid, reserved or duplicate name."""


class UnknownSourceError(KeyError):
    """No import source is registered under the requested name."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else super().__str__()


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    """An adapter class together with where it was registered from."""

    factory: type[Any]
    origin: str

    @property
    def description(self) -> str:
        return str(getattr(self.factory, "description", "") or "")


class SourceAdapterRegistry:
    """Maps a source-system name to the adapter class that handles it.

    Built once per process by :func:`build_source_registry`. Registering a name
    twice is an error rather than an override, so two plugins cannot silently
    fight over a source.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredSource] = {}

    def register(self, name: str, factory: type[Any], origin: str = ORIGIN_CORE) -> None:
        """Register *factory* under *name*.

        Raises :class:`SourceRegistrationError` for a malformed name, a reserved
        one claimed by anything but core, or a name that is already taken.
        """
        if not isinstance(name, str) or not SOURCE_NAME_PATTERN.match(name):
            raise SourceRegistrationError(
                f"Invalid import source name {name!r} from {origin}: must match {SOURCE_NAME_PATTERN.pattern}"
            )
        if name in RESERVED_SOURCE_NAMES and origin != ORIGIN_CORE:
            raise SourceRegistrationError(f"Import source name '{name}' from {origin} is reserved for the built-in one")
        if name in self._entries:
            raise SourceRegistrationError(
                f"Import source '{name}' is registered twice (by {self._entries[name].origin} and by {origin})"
            )
        self._entries[name] = RegisteredSource(factory, origin)

    def get(self, name: str) -> type[Any]:
        """Return the adapter class registered for *name*.

        Raises :class:`UnknownSourceError` naming the known sources when *name*
        is unregistered.
        """
        try:
            return self._entries[name].factory
        except KeyError:
            known = ", ".join(self.names()) or "none"
            raise UnknownSourceError(f"Unknown import source '{name}'. Registered sources: {known}") from None

    def entries(self) -> Mapping[str, RegisteredSource]:
        """Return every registration by name (read-only)."""
        return MappingProxyType(self._entries)

    def names(self) -> list[str]:
        """Return every registered source name, sorted."""
        return sorted(self._entries)


def builtin_sources() -> list[type[Any]]:
    """Return the adapter classes shipped with Specivo."""
    from specivo.importers.bundle.adapter import BundleSourceAdapter
    from specivo.importers.redmine.adapter import RedmineSourceAdapter

    return [BundleSourceAdapter, RedmineSourceAdapter]


def _check_source_shape(source: object, origin: str) -> tuple[str, type[Any]]:
    """Return ``(name, class)`` for *source*, or explain why it is not an adapter class."""
    if not isinstance(source, type):
        raise SourceRegistrationError(f"{origin} provided {source!r}, which is not an adapter class")
    name = getattr(source, "source_system", None)
    missing = [attr for attr in ("add_arguments", "from_args") if not callable(getattr(source, attr, None))]
    if not isinstance(name, str):
        missing.insert(0, "source_system")
    if missing or not isinstance(name, str):
        raise SourceRegistrationError(
            f"{origin} provided {source.__name__}, which is not an import source (missing {', '.join(missing)}). "
            "Subclass BaseSourceAdapter."
        )
    return name, source


def build_source_registry(plugin_sources: Iterable[tuple[str, Iterable[object]]] = ()) -> SourceAdapterRegistry:
    """Build the registry: the built-in adapters, then each plugin's.

    *plugin_sources* is ``(plugin_name, adapter_classes)`` per plugin. Raises
    :class:`SourceRegistrationError` on an invalid, reserved or duplicate name.
    """
    registry = SourceAdapterRegistry()
    for builtin in builtin_sources():
        name, factory = _check_source_shape(builtin, ORIGIN_CORE)
        registry.register(name, factory, ORIGIN_CORE)
    for plugin_name, sources in plugin_sources:
        origin = plugin_origin(plugin_name)
        for source in sources:
            name, factory = _check_source_shape(source, origin)
            registry.register(name, factory, origin)
    return registry


_registry: SourceAdapterRegistry | None = None


def install_source_registry(registry: SourceAdapterRegistry) -> None:
    """Make *registry* the process-wide one. Called by the bootstrap."""
    global _registry  # noqa: PLW0603
    _registry = registry


def get_source_registry() -> SourceAdapterRegistry:
    """Return the process-wide registry.

    A process that never ran the bootstrap (a script, a unit test) gets the
    built-in sources only, built on first use.
    """
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = build_source_registry()
    return _registry
