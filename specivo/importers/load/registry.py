"""Wire the loaders onto the pipeline's phases.

Kept next to the loaders rather than in the pipeline, so the pipeline stays
source- and entity-agnostic and this file is the one place that says what a
full import consists of.

The order is the pipeline's, not this file's: phases run in the order they are
declared on :class:`~specivo.importers.core.pipeline.ImportPhase`.
"""

from __future__ import annotations

from specivo.importers.core.pipeline import ImportPhase, ImportPipeline, PhaseContext, PhaseFailedError
from specivo.importers.load.attachment_loader import load_issue_attachments, load_wiki_attachments
from specivo.importers.load.issue_links import load_issue_links
from specivo.importers.load.issue_loader import (
    load_issues,
    load_journals,
    load_watchers,
)
from specivo.importers.load.lookup_loader import load_lookups
from specivo.importers.load.pending_links import check_links_file, link_placeholders
from specivo.importers.load.project_loader import (
    load_custom_field_schemas,
    load_memberships,
    load_project_lookups,
    load_projects,
    record_sprint_velocity,
    restore_version_statuses,
)
from specivo.importers.load.public_access_loader import load_public_access
from specivo.importers.load.search_index import backfill_search_index, rebuild_wiki_link_graph
from specivo.importers.load.time_entry_loader import load_time_entries
from specivo.importers.load.user_loader import (
    check_credential_import,
    ensure_import_account,
    load_groups,
    load_users,
)
from specivo.importers.load.wiki_loader import load_wiki_pages, load_wiki_redirects, load_wiki_watchers


async def _bootstrap(ctx: PhaseContext) -> None:
    """Check the run can do what it was asked, then create the import account.

    The account owns anything with no resolvable author. The checks come first
    so a run that cannot import the passwords it was asked to, or was given
    another import's links file, stops before it writes anything.
    """
    check_credential_import(ctx)
    await _warn_about_an_earlier_import(ctx)
    check_links_file(ctx)
    await ensure_import_account(ctx)
    _record_source_notes(ctx)


async def _warn_about_an_earlier_import(ctx: PhaseContext) -> None:
    """Say so, loudly, when this source has been imported into this database before.

    The identifier map is keyed by source system and instance. Finding rows
    under the same key means this run adds to that earlier import: what it
    already brought over is skipped, not created again or updated. That is
    right for a resumed or a delta run, and wrong for a different installation
    that happens to have the same name, which must be given its own.

    A source whose scope comes from its own data (a bundle names it in its
    manifest) is refused instead, unless the operator passed
    ``--continue-import`` or is resuming a run.
    """
    if ctx.options.resume_run_id is not None:
        return
    existing = await ctx.id_map.count(ctx.session)
    if existing and getattr(ctx.adapter, "scope_from_source_data", False) and not ctx.options.continue_import:
        # The scope comes from the data (a bundle's manifest), not from the
        # operator, so a bundle could name another import's scope and have its
        # records skip, or attach to, rows that import created. Adding to an
        # earlier import is therefore something the operator says, every time.
        counts = await ctx.id_map.counts(ctx.session)
        held = ", ".join(f"{count} {entity_type}" for entity_type, count in sorted(counts.items()))
        scope = f"{ctx.id_map.source_system} @ {ctx.id_map.source_instance}"
        raise PhaseFailedError(
            f"The source this bundle names, {scope}, was imported into this database before "
            f"({existing} identifier-map rows: {held}). Loading it adds to that import: records it already "
            "brought over are skipped, and new ones attach to what is there. If this is a later export of the "
            "same installation, re-run with --continue-import. If it is a different installation, give it its "
            "own source.instance. Nothing was imported."
        )
    if existing:
        ctx.warn(
            "This source was imported into this database before; this run adds to that import, skipping what is "
            "already here. If it is a different installation, give it its own instance name "
            "(source.instance in a bundle's manifest, or --source-instance)",
            source=f"{ctx.id_map.source_system}:{ctx.id_map.source_instance}",
            records_already_imported=existing,
        )


def _record_source_notes(ctx: PhaseContext) -> None:
    """Copy what the source itself wants reported into the summary.

    An adapter that knows something the operator should read before anything
    is loaded (what its exporter left behind, say) offers it as
    ``(category, line)`` pairs from an optional ``source_notes()``.
    """
    reader = getattr(ctx.adapter, "source_notes", None)
    if not callable(reader):
        return
    for category, line in reader():
        ctx.summary.add_note(category, line)


async def _users_and_groups(ctx: PhaseContext) -> None:
    """Import users, then the groups they belong to.

    In that order: a group can only hold members the run has already created.
    """
    await load_users(ctx)
    await load_groups(ctx)


async def _memberships(ctx: PhaseContext) -> None:
    """Import project memberships, then what non-members and anonymous visitors could read.

    Access for people without a membership needs the projects to exist, and
    belongs with the access people with one were given.
    """
    await load_memberships(ctx)
    await load_public_access(ctx)


async def _version_and_sprint_statuses(ctx: PhaseContext) -> None:
    """Once every issue is in: give versions their real status, and completed sprints their velocity."""
    await restore_version_statuses(ctx)
    await record_sprint_velocity(ctx)


async def _link_graph(ctx: PhaseContext) -> None:
    """Expand reference placeholders, then rebuild the wiki link graph.

    Placeholders wait until here because they can name any imported record,
    wiki pages and their attachments included. Text earlier imports of the
    source left waiting for this run's records is linked in the same step
    (see :mod:`~specivo.importers.load.pending_links`). The link graph comes
    after, since an expanded placeholder can be a wiki link.
    """
    await link_placeholders(ctx)
    await rebuild_wiki_link_graph(ctx)


async def _wiki(ctx: PhaseContext) -> None:
    """Import wiki pages, then their watchers and redirects."""
    await load_wiki_pages(ctx)
    await load_wiki_watchers(ctx)
    await load_wiki_redirects(ctx)


def register_all(pipeline: ImportPipeline) -> ImportPipeline:
    """Register every loader and return the pipeline, for chaining."""
    pipeline.register_all(
        {
            ImportPhase.BOOTSTRAP: _bootstrap,
            ImportPhase.LOOKUPS: load_lookups,
            ImportPhase.USERS: _users_and_groups,
            ImportPhase.PROJECTS: load_projects,
            ImportPhase.PROJECT_LOOKUPS: load_project_lookups,
            ImportPhase.MEMBERSHIPS: _memberships,
            ImportPhase.CUSTOM_FIELD_SCHEMAS: load_custom_field_schemas,
            ImportPhase.ISSUES: load_issues,
            ImportPhase.WATCHERS: load_watchers,
            ImportPhase.JOURNALS: load_journals,
            ImportPhase.RELATIONS: load_issue_links,
            ImportPhase.ISSUE_ATTACHMENTS: load_issue_attachments,
            ImportPhase.VERSION_STATUSES: _version_and_sprint_statuses,
            ImportPhase.WIKI_PAGES: _wiki,
            ImportPhase.WIKI_ATTACHMENTS: load_wiki_attachments,
            ImportPhase.TIME_ENTRIES: load_time_entries,
            ImportPhase.WIKI_LINK_GRAPH: _link_graph,
            ImportPhase.SEARCH_BACKFILL: backfill_search_index,
        }
    )
    return pipeline
