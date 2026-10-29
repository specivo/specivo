"""Relations and parents between issues, also when the other issue is in another bundle.

A source exported in parts (one project at a time) links issues across the
parts: a relation from one project to another, a sub-task whose parent is in
another project. A bundle may carry such a link with one end outside it (see
``docs/import-bundle/v1.md``). This module makes those links once both issues
exist, whichever part is loaded first.

**This run's links.** In the relations phase, when every issue of the run
exists, each relation of the bundle and each parent the issues phase could not
set at once (the parent was not created by this run, or comes later in load
order) is looked up in the identifier map of the scope, which holds every
earlier import of the source. One that resolves is made through the same
checks as any relation or parent; one that is refused is reported and skipped.
One whose other issue is not imported is kept in the links file
(:mod:`specivo.importers.core.links_file`).

**Earlier runs' links.** Every later load of the same source, and
``admin import-map resolve-pending``, takes the relations and parents waiting
in the file and makes those whose issues the map now holds. Parents go first,
then relations, so a relation is checked against the hierarchy it will live
in.

**Idempotent.** A relation is keyed by its ``source_ref`` in the identifier
map, so one carried by two bundles (a tool writes a link between projects into
both) is made by whichever load first has both issues; the other finds it
mapped and skips it, and a waiting row for it is dropped. A parent is set only
on an issue that has none: one with the parent the source names is left alone,
and one with a different parent, given after the import, keeps it and the
waiting row is dropped with a warning.

Setting a parent is not an edit: both issues keep their ``updated_at``, and no
history entry is written, as for a parent set while the issue is created.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, ValidationError
from specivo.importers.core.backdate import backdate
from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import EntityType, IRRelation
from specivo.importers.core.links_file import LinksStore, PendingParent, PendingRelation
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.issue import Issue
from specivo.services.nested_set_service import MAX_DEPTH, NestedSetService
from specivo.services.relation_service import RelationService

logger = logging.getLogger(__name__)

_nested_set = NestedSetService()
_relation_service = RelationService()

# Rows read per query when checking the issues of a run.
_BATCH = 500
# Links named in a warning about those still waiting.
_MAX_EXAMPLES = 10

WAITING_WARNING = (
    "Relations and parents naming an issue that was not imported, in this run or an earlier one of the same "
    "source, are left out for now; the links file keeps them for a later import that brings the issue"
)
WAITING_NOTE = "Relations and parents waiting for issues a later import may bring"
LINKED_NOTE = "Relations and parents from earlier imports now linked"
RELATION_REFUSED = "Relation was rejected and skipped"
PARENT_REFUSED = "Issue's parent was rejected; it stays where it is"
OTHER_PARENT = "Issue already has a different parent; the one the source names was not set"
UNTRUSTED = (
    "Rows of the links file that this installation did not write were ignored and removed (no valid signature, or "
    "not a row an import writes): a links file only works with the Specivo, and the SECRET_KEY, that wrote it"
)
FOREIGN = "Rows of the links file naming records this source did not import were ignored and removed"


@dataclass(slots=True)
class LinkProblem:
    """A relation or parent that was refused or left alone, with what to report about it."""

    message: str
    context: dict[str, Any]

    def line(self) -> str:
        """One line for a command's output: ``relation r-1: reason``."""
        if "relation" in self.context:
            what = f"relation {self.context['relation']}"
        else:
            what = f"parent of issue {self.context['issue']}"
        return f"{what}: {self.context.get('reason') or self.message}"


@dataclass(slots=True)
class LinkResult:
    """What making relations and parents did, and what is left waiting."""

    relations_linked: int = 0
    parents_linked: int = 0
    problems: list[LinkProblem] = field(default_factory=list)
    untrusted: int = 0
    foreign: int = 0
    waiting_relations: list[PendingRelation] = field(default_factory=list)
    waiting_parents: list[PendingParent] = field(default_factory=list)


# --------------------------------------------------------------------------
# Making one link
# --------------------------------------------------------------------------


async def make_relation(
    session: AsyncSession, id_map: ImportIdMap, relation: IRRelation | PendingRelation, from_id: int, to_id: int
) -> LinkProblem | None:
    """Create *relation* between two imported issues and map it; return the problem if it is refused.

    The relation service normalises and validates it the way it does any
    relation: Specivo refuses some a source allows (between an issue and its
    own descendant, for one).
    """
    issue_from = await session.get(Issue, from_id)
    issue_to = await session.get(Issue, to_id)
    if issue_from is None or issue_to is None:
        return LinkProblem(RELATION_REFUSED, {"relation": relation.source_ref, "reason": "an issue is gone"})
    try:
        created = await _relation_service.create(
            session, issue_from, issue_to, relation.relation_type, delay=relation.delay
        )
    except AppError as exc:
        return LinkProblem(
            RELATION_REFUSED,
            {"relation": relation.source_ref, "type": relation.relation_type, "reason": str(exc)},
        )
    await id_map.put(session, EntityType.RELATION, relation.source_ref, "issue_relations", created.id)
    return None


async def set_parent(
    session: AsyncSession, child_ref: str, parent_ref: str, child_id: int, parent_id: int
) -> tuple[bool, LinkProblem | None]:
    """Make issue *parent_id* the parent of *child_id*; return whether it was set, and any problem.

    Nothing is done to an issue that already has that parent (``False, None``)
    or another one (``False`` and a problem): a parent given after the import
    is never replaced. Otherwise the nested-set rules apply, as when the
    parent is changed in Specivo: not the issue itself, not one of its own
    descendants, and the hierarchy, the issue's own sub-tasks included, no
    deeper than Specivo allows. Both issues keep their ``updated_at``.
    """
    context = {"issue": child_ref, "parent": parent_ref}
    child = await session.get(Issue, child_id)
    parent = await session.get(Issue, parent_id)
    if child is None or parent is None:
        return False, LinkProblem(PARENT_REFUSED, {**context, "reason": "an issue is gone"})
    await session.refresh(child)
    if child.parent_id == parent.id:
        return False, None
    if child.parent_id is not None:
        return False, LinkProblem(OTHER_PARENT, context)

    child_updated, parent_updated = child.updated_at, parent.updated_at
    try:
        # Every check runs before anything moves, so a refusal leaves the tree as it was.
        await _nested_set.validate_parent(session, child, parent)
        await _check_subtree_depth(session, child, parent)
    except AppError as exc:
        return False, LinkProblem(PARENT_REFUSED, {**context, "reason": str(exc)})
    await _nested_set.move_to_parent(session, child, parent)
    await session.refresh(parent)
    await _nested_set.recalculate_parent_attributes(session, parent)
    await session.flush()
    await backdate(session, Issue, child.id, updated_at=child_updated)
    await backdate(session, Issue, parent.id, updated_at=parent_updated)
    return True, None


async def _check_subtree_depth(session: AsyncSession, child: Issue, parent: Issue) -> None:
    """Refuse a parent under which the child's own sub-tasks would sit deeper than Specivo allows."""
    depth = {child.id: 0}
    for descendant in await _nested_set.get_descendants(session, child):
        depth[descendant.id] = depth.get(descendant.parent_id or child.id, 0) + 1
    deepest = len(await _nested_set.get_ancestors(session, parent)) + 1 + max(depth.values())
    if deepest >= MAX_DEPTH:
        raise ValidationError(
            message=f"Maximum hierarchy depth of {MAX_DEPTH} would be exceeded.",
            field="parent_id",
            details={"max_depth": MAX_DEPTH, "current_depth": deepest},
        )


# --------------------------------------------------------------------------
# The links file's rows
# --------------------------------------------------------------------------


async def resolve_waiting_parents(
    session: AsyncSession, id_map: ImportIdMap, store: LinksStore, result: LinkResult
) -> None:
    """Set the parents waiting in *store* whose issues the identifier map now holds.

    The file is untrusted: a row is used only when it is well formed and
    signed by this installation, and its issues are found only through the
    map of the scope. A row whose child this scope did not import is dropped
    and counted. A row that was set, found already set, or refused leaves the
    file; one whose parent is still missing stays.
    """
    for row in store.parents():
        if not store.is_trusted(row):
            store.delete_parent(row.child_ref)
            result.untrusted += 1
            continue
        child_id = await id_map.get(session, EntityType.ISSUE, row.child_ref)
        if child_id is None:
            store.delete_parent(row.child_ref)
            result.foreign += 1
            continue
        parent_id = await id_map.get(session, EntityType.ISSUE, row.parent_ref)
        if parent_id is None:
            continue
        done, problem = await set_parent(session, row.child_ref, row.parent_ref, child_id, parent_id)
        result.parents_linked += done
        if problem is not None:
            result.problems.append(problem)
        store.delete_parent(row.child_ref)


async def resolve_waiting_relations(
    session: AsyncSession, id_map: ImportIdMap, store: LinksStore, result: LinkResult
) -> None:
    """Make the relations waiting in *store* whose two issues the identifier map now holds.

    A relation already mapped (another bundle carried it too, and that load
    made it) leaves the file without anything being made again.
    """
    for row in store.relations():
        if not store.is_trusted(row):
            store.delete_relation(row.source_ref)
            result.untrusted += 1
            continue
        if await id_map.get(session, EntityType.RELATION, row.source_ref) is not None:
            store.delete_relation(row.source_ref)
            continue
        found = await id_map.get_many(session, EntityType.ISSUE, [row.from_ref, row.to_ref])
        if row.from_ref not in found or row.to_ref not in found:
            continue
        problem = await make_relation(session, id_map, row, found[row.from_ref], found[row.to_ref])
        if problem is None:
            result.relations_linked += 1
        else:
            result.problems.append(problem)
        store.delete_relation(row.source_ref)


async def resolve_waiting_links(session: AsyncSession, id_map: ImportIdMap, store: LinksStore) -> LinkResult:
    """Set the waiting parents, then make the waiting relations: outside an import, on demand."""
    result = LinkResult()
    await resolve_waiting_parents(session, id_map, store, result)
    await resolve_waiting_relations(session, id_map, store, result)
    return result


# --------------------------------------------------------------------------
# This run's links
# --------------------------------------------------------------------------


async def link_parents(ctx: PhaseContext, result: LinkResult) -> None:
    """Set the parents of this run's issues that the issues phase could not set at once.

    Read from the bundle again rather than remembered, so a resumed run (which
    keeps its run id) finds them as well. Issues an earlier import created are
    left alone: their parents, if any wait, are in the links file.
    """
    created = await ctx.id_map.written_by_run(ctx.session, EntityType.ISSUE)
    wanted: list[tuple[str, str]] = []
    for project_ref in ctx.project_refs:
        async for ir in ctx.adapter.extract_issues(project_ref):
            if ir.parent_ref is not None and ir.source_ref in created:
                wanted.append((ir.source_ref, ir.parent_ref))

    for start in range(0, len(wanted), _BATCH):
        batch = wanted[start : start + _BATCH]
        children = await ctx.id_map.get_many(ctx.session, EntityType.ISSUE, [child for child, _ in batch])
        parents = await ctx.id_map.get_many(ctx.session, EntityType.ISSUE, [parent for _, parent in batch])
        current = dict(
            (
                await ctx.session.execute(
                    select(Issue.id, Issue.parent_id).where(Issue.id.in_(list(children.values())))
                )
            ).all()
        )
        for child_ref, parent_ref in batch:
            child_id = children.get(child_ref)
            if child_id is None or child_id not in current:
                continue
            parent_id = parents.get(parent_ref)
            if parent_id is None:
                result.waiting_parents.append(PendingParent(child_ref, parent_ref))
                continue
            if current[child_id] == parent_id:
                continue
            done, problem = await set_parent(ctx.session, child_ref, parent_ref, child_id, parent_id)
            result.parents_linked += done
            if problem is not None:
                result.problems.append(problem)
            ctx.tick()


async def link_relations(ctx: PhaseContext, result: LinkResult) -> None:
    """Make the run's relations whose two issues exist; keep the others waiting."""
    for project_ref in ctx.project_refs:
        async for ir in ctx.adapter.extract_relations(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.RELATION, ir.source_ref):
                ctx.summary.record_skipped(EntityType.RELATION)
                continue
            found = await ctx.id_map.get_many(ctx.session, EntityType.ISSUE, [ir.from_ref, ir.to_ref])
            if ir.from_ref not in found or ir.to_ref not in found:
                result.waiting_relations.append(
                    PendingRelation(ir.source_ref, ir.from_ref, ir.to_ref, ir.relation_type, ir.delay)
                )
                continue
            problem = await make_relation(ctx.session, ctx.id_map, ir, found[ir.from_ref], found[ir.to_ref])
            if problem is not None:
                ctx.warn(problem.message, **problem.context)
                continue
            ctx.summary.record_created(EntityType.RELATION)
            ctx.tick()


async def load_issue_links(ctx: PhaseContext) -> None:
    """The relations phase: parents, then relations, this run's and those earlier runs left waiting.

    The links file is opened (or created, when something has to wait) for the
    phase and committed only after the phase's transaction has.
    """
    from specivo.importers.load import pending_links

    path = pending_links.links_path(ctx)
    usable = path is not None and pending_links.usable(ctx)
    store: LinksStore | None = None
    if usable and path is not None and os.path.lexists(path):
        store = pending_links.open_for_phase(ctx, path, has_new=False)

    earlier = LinkResult()
    if store is not None:
        await resolve_waiting_parents(ctx.session, ctx.id_map, store, earlier)
    current = LinkResult()
    await link_parents(ctx, current)
    await link_relations(ctx, current)
    if store is not None:
        await resolve_waiting_relations(ctx.session, ctx.id_map, store, earlier)

    waiting = current.waiting_relations or current.waiting_parents
    if waiting and store is None and usable and path is not None and not os.path.lexists(path):
        store = pending_links.open_for_phase(ctx, path, has_new=True)
    if store is not None:
        for relation in current.waiting_relations:
            store.put_relation(relation)
        for parent in current.waiting_parents:
            store.put_parent(parent)

    _report(ctx, current, earlier, store)


def _report(ctx: PhaseContext, current: LinkResult, earlier: LinkResult, store: LinksStore | None) -> None:
    for problem in [*current.problems, *earlier.problems]:
        ctx.warn(problem.message, **problem.context)
    for _ in range(earlier.relations_linked):
        ctx.summary.record_created(EntityType.RELATION)
    if earlier.relations_linked or earlier.parents_linked:
        ctx.summary.add_note(LINKED_NOTE, f"{earlier.relations_linked} relation(s), {earlier.parents_linked} parent(s)")
    if current.waiting_relations or current.waiting_parents:
        examples = [f"relation {r.source_ref}" for r in current.waiting_relations] + [
            f"parent of {p.child_ref}" for p in current.waiting_parents
        ]
        ctx.warn(
            WAITING_WARNING,
            relations=len(current.waiting_relations),
            parents=len(current.waiting_parents),
            examples=", ".join(examples[:_MAX_EXAMPLES]),
        )
    if earlier.untrusted:
        ctx.warn(UNTRUSTED, count=earlier.untrusted, path=str(store.path) if store else None)
    if earlier.foreign:
        ctx.warn(FOREIGN, count=earlier.foreign, path=str(store.path) if store else None)
    if store is not None:
        counts = store.waiting_links()
        if counts:
            parts = [f"{counts[kind]} {kind}(s)" for kind in ("relation", "parent") if kind in counts]
            ctx.summary.add_note(WAITING_NOTE, f"{', '.join(parts)}, kept in {store.path}")


__all__ = [
    "LINKED_NOTE",
    "WAITING_NOTE",
    "WAITING_WARNING",
    "LinkProblem",
    "LinkResult",
    "load_issue_links",
    "make_relation",
    "resolve_waiting_links",
    "set_parent",
]
