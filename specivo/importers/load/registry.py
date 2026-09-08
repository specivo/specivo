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
from specivo.importers.load.project_loader import (
    load_custom_field_schemas,
    load_memberships,
    load_project_lookups,
    load_projects,
)
from specivo.importers.load.reference_rewrite import rewrite_issue_references
from specivo.importers.load.search_index import backfill_search_index, rebuild_wiki_link_graph
from specivo.importers.load.time_entry_loader import load_time_entries
from specivo.importers.load.user_loader import ensure_import_account, load_groups, load_users
from specivo.importers.load.wiki_loader import load_wiki_pages, load_wiki_redirects, load_wiki_watchers


async def _bootstrap(ctx: PhaseContext) -> None:
    """Create the account that owns anything with no resolvable author."""
    await ensure_import_account(ctx)


async def _users_and_groups(ctx: PhaseContext) -> None:
    """Import users, then collect group membership for the memberships phase."""
    await load_users(ctx)
    await load_groups(ctx)


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
            ImportPhase.MEMBERSHIPS: load_memberships,
            ImportPhase.CUSTOM_FIELD_SCHEMAS: load_custom_field_schemas,
            ImportPhase.ISSUES: load_issues,
            ImportPhase.WATCHERS: load_watchers,
            ImportPhase.CUSTOM_FIELD_VALUES: resolve_custom_field_references,
            ImportPhase.JOURNALS: load_journals,
            ImportPhase.RELATIONS: load_relations,
            ImportPhase.ISSUE_ATTACHMENTS: load_issue_attachments,
            ImportPhase.ISSUE_REF_REWRITE: rewrite_issue_references,
            ImportPhase.WIKI_PAGES: _wiki,
            ImportPhase.WIKI_ATTACHMENTS: load_wiki_attachments,
            ImportPhase.TIME_ENTRIES: load_time_entries,
            ImportPhase.WIKI_LINK_GRAPH: rebuild_wiki_link_graph,
            ImportPhase.SEARCH_BACKFILL: backfill_search_index,
        }
    )
    return pipeline
