"""RelationService — create, list, and delete issue relations."""

from __future__ import annotations

import logging
from collections import deque
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.models.issue import Issue
from specivo.models.relation import IssueRelation
from specivo.models.user import User
from specivo.services.nested_set_service import NestedSetService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Relation type metadata
# ---------------------------------------------------------------------------

# Maps every user-facing type label to:
#   "sym"     — the label shown when looking at the relation from the other side
#   "reverse" — the canonical DB type to store when this is used as the "from" side
#               (None means it IS the canonical type and is stored as-is)
#   "canonical" — the DB-stored form
RELATION_TYPES: dict[str, dict[str, str | None]] = {
    "relates": {"sym": "relates", "reverse": None, "canonical": "relates"},
    "duplicates": {"sym": "duplicated", "reverse": None, "canonical": "duplicates"},
    "duplicated": {"sym": "duplicates", "reverse": "duplicates", "canonical": "duplicates"},
    "blocks": {"sym": "blocked", "reverse": None, "canonical": "blocks"},
    "blocked": {"sym": "blocks", "reverse": "blocks", "canonical": "blocks"},
    "precedes": {"sym": "follows", "reverse": None, "canonical": "precedes"},
    "follows": {"sym": "precedes", "reverse": "precedes", "canonical": "precedes"},
    "copied_to": {"sym": "copied_from", "reverse": None, "canonical": "copied_to"},
    "copied_from": {"sym": "copied_to", "reverse": "copied_to", "canonical": "copied_to"},
}

# Canonical types that support circular-dependency checking
_CIRCULAR_CHECK_TYPES = frozenset({"blocks", "precedes"})

_nested_set = NestedSetService()


class RelationService:
    """Service layer for issue relation operations."""

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        issue_from: Issue,
        issue_to: Issue,
        relation_type: str,
        delay: int | None = None,
    ) -> IssueRelation:
        """Create an issue relation with normalisation and validation.

        Steps:
        1. Normalise reverse types → canonical form, swapping from/to if needed.
        2. For ``relates``: ensure lower ID is always ``issue_from_id``.
        3. Validate: not same issue, no parent-descendant relationship.
        4. Check for duplicate relation.
        5. Check circular dependency for ``blocks`` and ``precedes``.
        6. Persist and, for ``precedes`` with delay, reschedule successor.
        """
        meta = RELATION_TYPES[relation_type]

        # Step 1: normalise reverse types
        if meta["reverse"] is not None:
            # Swap from/to and use the canonical type
            issue_from, issue_to = issue_to, issue_from
            canonical_type = meta["reverse"]
        else:
            canonical_type = relation_type

        # Step 2: for 'relates', canonical order is lower ID first
        if canonical_type == "relates" and issue_from.id > issue_to.id:
            issue_from, issue_to = issue_to, issue_from

        from_id = issue_from.id
        to_id = issue_to.id

        # Step 3a: self-relation
        if from_id == to_id:
            raise ValidationError(
                message="An issue cannot be related to itself.",
                field="issue_to_key",
            )

        # Step 3b: parent-descendant check — relations between ancestors and
        #          descendants are disallowed to avoid contradictory semantics
        if await _nested_set.is_ancestor_of(session, issue_from, issue_to):
            raise ValidationError(
                message="Cannot create a relation between a parent issue and its descendant.",
                field="issue_to_key",
            )
        if await _nested_set.is_ancestor_of(session, issue_to, issue_from):
            raise ValidationError(
                message="Cannot create a relation between a parent issue and its descendant.",
                field="issue_to_key",
            )

        # Step 4: duplicate check
        existing = await session.execute(
            select(IssueRelation).where(
                IssueRelation.issue_from_id == from_id,
                IssueRelation.issue_to_id == to_id,
                IssueRelation.relation_type == canonical_type,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(
                message=f"A '{canonical_type}' relation between these issues already exists.",
                field="relation_type",
            )

        # Step 5: circular dependency check for blocking / scheduling chains
        if canonical_type in _CIRCULAR_CHECK_TYPES:
            if await self._check_circular(session, from_id, to_id, canonical_type):
                raise ValidationError(
                    message=(f"Creating this '{canonical_type}' relation would introduce a circular dependency."),
                    field="relation_type",
                )

        # Step 6: persist
        relation = IssueRelation(
            issue_from_id=from_id,
            issue_to_id=to_id,
            relation_type=canonical_type,
            delay=delay,
        )
        session.add(relation)
        await session.flush()

        logger.info(
            "Created relation id=%d: %d %s %d (delay=%s)",
            relation.id,
            from_id,
            canonical_type,
            to_id,
            delay,
        )

        # Step 7: auto-reschedule for precedes with delay
        if canonical_type == "precedes" and delay is not None:
            await self._reschedule_successor(session, issue_from, issue_to, delay)

        return relation

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, session: AsyncSession, relation_id: int, user: User) -> None:
        """Delete a relation by ID.

        Permission: user must be admin or have access to at least one
        of the two linked issues (via project membership or public project).
        Raises ``NotFoundError`` when the relation does not exist.
        Raises ``PermissionDeniedError`` when the user has no access.
        """

        result = await session.execute(select(IssueRelation).where(IssueRelation.id == relation_id))
        relation = result.scalar_one_or_none()
        if relation is None:
            raise NotFoundError(f"Relation {relation_id} not found")

        if not user.is_admin:
            from specivo.services.permission_service import check_permission

            # Check user has edit_issues permission on at least one of the related projects
            issue_ids = [relation.issue_from_id, relation.issue_to_id]
            issues_result = await session.execute(select(Issue.project_id).where(Issue.id.in_(issue_ids)))
            project_ids = {row[0] for row in issues_result.all()}

            has_permission = False
            for pid in project_ids:
                if await check_permission(user, pid, "edit_issues", session):
                    has_permission = True
                    break

            if not has_permission:
                raise PermissionDeniedError("You do not have permission to delete this relation")

        await session.delete(relation)
        await session.flush()
        logger.info("Deleted relation id=%d by user=%d", relation_id, user.id)

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_for_issue(self, session: AsyncSession, issue: Issue) -> list[dict]:
        """Return all relations for an issue, labelled from the issue's perspective.

        For each relation the ``relation_type`` field reflects the label
        appropriate for the queried issue:
        - If this issue is ``issue_from``, the stored canonical type is used.
        - If this issue is ``issue_to``, the symmetric (reverse) label is used.

        The returned dicts conform to the ``RelationOut`` schema.
        """
        result = await session.execute(
            select(IssueRelation).where(
                or_(
                    IssueRelation.issue_from_id == issue.id,
                    IssueRelation.issue_to_id == issue.id,
                )
            )
        )
        relations = list(result.scalars().all())

        # Bulk-load all referenced issue keys in two queries
        related_ids: set[int] = set()
        for r in relations:
            related_ids.add(r.issue_from_id)
            related_ids.add(r.issue_to_id)

        key_map = await self._load_display_keys(session, related_ids)

        out: list[dict] = []
        for r in relations:
            if r.issue_from_id == issue.id:
                # Issue is the "from" side — use canonical type as-is
                shown_type = r.relation_type
                from_key = key_map.get(r.issue_from_id, str(r.issue_from_id))
                to_key = key_map.get(r.issue_to_id, str(r.issue_to_id))
            else:
                # Issue is the "to" side — use the symmetric (reverse) label
                shown_type = RELATION_TYPES[r.relation_type]["sym"]
                # Swap keys so from_key is always the queried issue
                from_key = key_map.get(r.issue_to_id, str(r.issue_to_id))
                to_key = key_map.get(r.issue_from_id, str(r.issue_from_id))

            out.append(
                {
                    "id": r.id,
                    "issue_from_key": from_key,
                    "issue_to_key": to_key,
                    "relation_type": shown_type,
                    "delay": r.delay,
                }
            )

        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _check_circular(
        self,
        session: AsyncSession,
        from_id: int,
        to_id: int,
        relation_type: str,
    ) -> bool:
        """BFS traversal to detect circular dependencies.

        For ``blocks``:  checks whether ``to_id`` eventually blocks ``from_id``.
        For ``precedes``: checks whether ``to_id`` eventually precedes ``from_id``.

        Returns True if adding (from_id → to_id) would create a cycle.
        """
        visited: set[int] = set()
        queue: deque[int] = deque([to_id])

        while queue:
            current = queue.popleft()
            if current == from_id:
                return True
            if current in visited:
                continue
            visited.add(current)

            # Find all nodes that current points TO via the same relation type
            result = await session.execute(
                select(IssueRelation.issue_to_id).where(
                    IssueRelation.issue_from_id == current,
                    IssueRelation.relation_type == relation_type,
                )
            )
            for (next_id,) in result.all():
                if next_id not in visited:
                    queue.append(next_id)

        return False

    async def _load_display_keys(self, session: AsyncSession, issue_ids: set[int]) -> dict[int, str]:
        """Load display keys (project_key-sequence_number) for a set of issue IDs."""
        if not issue_ids:
            return {}
        result = await session.execute(
            select(Issue.id, Issue.project_key, Issue.sequence_number).where(Issue.id.in_(issue_ids))
        )
        return {row.id: f"{row.project_key}-{row.sequence_number}" for row in result.all()}

    async def _reschedule_successor(
        self,
        session: AsyncSession,
        predecessor: Issue,
        successor: Issue,
        delay: int,
    ) -> None:
        """Set successor.start_date = predecessor.due_date + delay + 1 day.

        Only applied when the predecessor has a ``due_date``.
        No cascade — callers can add cascading logic later.
        """
        if predecessor.due_date is None:
            return

        new_start: date = predecessor.due_date + timedelta(days=delay + 1)
        successor.start_date = new_start
        await session.flush()
        logger.info(
            "Rescheduled issue id=%d start_date → %s (predecessor id=%d due=%s delay=%d)",
            successor.id,
            new_start,
            predecessor.id,
            predecessor.due_date,
            delay,
        )
