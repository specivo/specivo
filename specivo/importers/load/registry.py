"""Wire the loaders onto the pipeline's phases.

Kept next to the loaders rather than in the pipeline, so the pipeline stays
source- and entity-agnostic and this file is the one place that says what a
full import consists of.

The order is the pipeline's, not this file's: phases run in the order they are
declared on :class:`~specivo.importers.core.pipeline.ImportPhase`.
"""

from __future__ import annotations

from specivo.importers.core.pipeline import ImportPhase, ImportPipeline, PhaseContext
from specivo.importers.load.attachment_loader import load_issue_attachments, load_wiki_attachments
from specivo.importers.load.issue_loader import (
    load_issues,
    load_journals,
    load_relations,
    load_watchers,
    resolve_custom_field_references,
)
from specivo.importers.load.lookup_loader import load_lookups
from specivo.importers.load.placeholder_rewrite import rewrite_placeholders
from specivo.importers.load.project_loader import (
    load_custom_field_schemas,
    load_memberships,
    load_project_lookups,
    load_projects,
    restore_version_statuses,
)
from specivo.importers.load.public_access_loader import load_public_access
from specivo.importers.load.reference_rewrite import rewrite_issue_references
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

    The account owns anything with no resolvable author. The check comes first
    so a run that cannot import the passwords it was asked to stops before it
    writes anything.
    """
    check_credential_import(ctx)
    await ensure_import_account(ctx)
    _record_source_notes(ctx)


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


async def _after_issues(ctx: PhaseContext) -> None:
    """The fix-ups that can only be done once every issue exists.

    References because one can point into a project imported later, and version
    statuses because a locked version cannot be assigned to an issue while the
    issues are still being created.
    """
    await rewrite_issue_references(ctx)
    await restore_version_statuses(ctx)


async def _link_graph(ctx: PhaseContext) -> None:
    """Expand reference placeholders, then rebuild the wiki link graph.

    Placeholders wait until here because they can name any imported record,
    wiki pages and their attachments included. The link graph comes after,
    since an expanded placeholder can be a wiki link.
    """
    await rewrite_placeholders(ctx)
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
            ImportPhase.CUSTOM_FIELD_VALUES: resolve_custom_field_references,
            ImportPhase.JOURNALS: load_journals,
            ImportPhase.RELATIONS: load_relations,
            ImportPhase.ISSUE_ATTACHMENTS: load_issue_attachments,
            ImportPhase.ISSUE_REF_REWRITE: _after_issues,
            ImportPhase.WIKI_PAGES: _wiki,
            ImportPhase.WIKI_ATTACHMENTS: load_wiki_attachments,
            ImportPhase.TIME_ENTRIES: load_time_entries,
            ImportPhase.WIKI_LINK_GRAPH: _link_graph,
            ImportPhase.SEARCH_BACKFILL: backfill_search_index,
        }
    )
    return pipeline
