"""Nested set tree management for issues.

Implements the nested set model (lft/rgt/root_id) for Issue hierarchy.
All tree-modifying operations acquire a PostgreSQL advisory transaction lock
on the root_id to prevent concurrent corruption.

Algorithm reference: classic nested set model (Celko, 2004).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_HIERARCHY_DEPTH
from specivo.core.exceptions import ValidationError
from specivo.models.issue import Issue

logger = logging.getLogger(__name__)

MAX_DEPTH = MAX_HIERARCHY_DEPTH


class NestedSetService:
    """Nested set tree operations for Issue hierarchy."""

    # ------------------------------------------------------------------
    # Advisory lock helpers
    # ------------------------------------------------------------------

    async def _lock_tree(self, session: AsyncSession, root_id: int) -> None:
        """Acquire a PostgreSQL advisory transaction lock for a tree.

        The lock is scoped to the current transaction and released automatically
        on COMMIT or ROLLBACK. This prevents two concurrent requests from
        modifying the same tree simultaneously.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:root_id)"),
            {"root_id": root_id},
        )
        logger.debug("Acquired advisory lock for tree root_id=%d", root_id)

    # ------------------------------------------------------------------
    # Root insertion
    # ------------------------------------------------------------------

    async def insert_root(self, session: AsyncSession, issue: Issue) -> None:
        """Initialize a root issue with nested set values.

        A root issue is its own tree root: root_id = self.id, lft = 1, rgt = 2.
        The issue must already be flushed (have a real id) before calling this.
        """
        issue.root_id = issue.id
        issue.parent_id = None
        issue.lft = 1
        issue.rgt = 2
        await session.flush()
        logger.debug("Initialized root node id=%d as lft=1 rgt=2", issue.id)

    # ------------------------------------------------------------------
    # Child insertion
    # ------------------------------------------------------------------

    async def insert_child(self, session: AsyncSession, parent: Issue, child: Issue) -> None:
        """Insert child as the rightmost child of parent.

        Algorithm:
        1. Acquire advisory lock on parent's root_id
        2. child.lft = parent.rgt
        3. child.rgt = parent.rgt + 1
        4. Shift all nodes in the tree: lft >= parent.rgt → lft += 2
        5. Shift all nodes in the tree: rgt >= parent.rgt → rgt += 2
        6. Set child.parent_id = parent.id, child.root_id = parent.root_id

        The parent's rgt is updated in step 5 since parent.rgt >= parent.rgt.
        """
        root_id = parent.root_id
        if root_id is None:
            raise ValidationError(
                message=f"Parent issue {parent.id} has no root_id set. Run insert_root on the parent first.",
                field="parent_id",
            )

        await self._lock_tree(session, root_id)

        insertion_point = parent.rgt  # child slots in here

        # Shift existing nodes to make room
        await session.execute(
            update(Issue).where(Issue.root_id == root_id, Issue.lft >= insertion_point).values(lft=Issue.lft + 2)
        )
        await session.execute(
            update(Issue).where(Issue.root_id == root_id, Issue.rgt >= insertion_point).values(rgt=Issue.rgt + 2)
        )

        # Position the child
        child.lft = insertion_point
        child.rgt = insertion_point + 1
        child.parent_id = parent.id
        child.root_id = root_id

        await session.flush()
        logger.debug(
            "Inserted child id=%d under parent id=%d (lft=%d rgt=%d root_id=%d)",
            child.id,
            parent.id,
            child.lft,
            child.rgt,
            root_id,
        )

    # ------------------------------------------------------------------
    # Move to new parent
    # ------------------------------------------------------------------

    async def move_to_parent(
        self,
        session: AsyncSession,
        issue: Issue,
        new_parent: Issue | None,
    ) -> None:
        """Move issue (and its subtree) to a new parent.

        If new_parent is None, the issue becomes a new root.

        Algorithm (gap-based move — no sentinel values):
        1. Acquire advisory locks on old and new root trees.
        2. Open gap at target position (shift boundaries in target tree).
        3. Refresh issue from DB (its boundaries may have shifted if same tree).
        4. Move subtree into the gap (single UPDATE: shift lft/rgt + set root_id).
        5. Close gap at old position.
        6. Update parent_id on the moved issue.

        No sentinel root_id is used — the subtree's root_id goes directly from
        old to new in the move UPDATE, avoiding FK constraint violations.
        """
        if new_parent is not None and new_parent.id == issue.id:
            raise ValidationError(
                message="An issue cannot be its own parent.",
                field="parent_id",
            )

        if new_parent is not None:
            if await self.is_descendant_of(session, new_parent, issue):
                raise ValidationError(
                    message="Cannot move an issue to one of its own descendants. "
                    "This would create a cycle in the hierarchy.",
                    field="parent_id",
                )

        old_root_id = issue.root_id
        new_root_id = new_parent.root_id if new_parent is not None else issue.id

        # Lock both trees (may be the same tree)
        if old_root_id is not None:
            await self._lock_tree(session, old_root_id)
        if new_root_id is not None and new_root_id != old_root_id:
            await self._lock_tree(session, new_root_id)

        old_lft = issue.lft
        old_rgt = issue.rgt
        subtree_width = old_rgt - old_lft + 1

        if new_parent is None:
            # --- Moving to root ---
            # Step 1: Close gap at old position
            await session.execute(
                update(Issue)
                .where(Issue.root_id == old_root_id, Issue.lft > old_rgt)
                .values(lft=Issue.lft - subtree_width)
            )
            await session.execute(
                update(Issue)
                .where(Issue.root_id == old_root_id, Issue.rgt > old_rgt)
                .values(rgt=Issue.rgt - subtree_width)
            )

            # Step 2: Normalise subtree so lft starts at 1, and set new root_id
            lft_shift = 1 - old_lft
            await session.execute(
                update(Issue)
                .where(
                    Issue.root_id == old_root_id,
                    Issue.lft >= old_lft,
                    Issue.rgt <= old_rgt,
                )
                .values(
                    lft=Issue.lft + lft_shift,
                    rgt=Issue.rgt + lft_shift,
                    root_id=issue.id,
                )
            )

            issue.parent_id = None
            await session.flush()
            await session.refresh(issue)

        else:
            # --- Moving under new_parent ---
            same_tree = old_root_id == new_root_id

            # Step 1: Open gap at insertion point (new_parent.rgt)
            insertion_point = new_parent.rgt

            await session.execute(
                update(Issue)
                .where(
                    Issue.root_id == new_root_id,
                    Issue.lft >= insertion_point,
                )
                .values(lft=Issue.lft + subtree_width)
            )
            await session.execute(
                update(Issue)
                .where(
                    Issue.root_id == new_root_id,
                    Issue.rgt >= insertion_point,
                )
                .values(rgt=Issue.rgt + subtree_width)
            )

            # Step 2: Refresh issue — its lft/rgt may have shifted if same tree
            await session.refresh(issue)
            cur_lft = issue.lft
            cur_rgt = issue.rgt

            # Step 3: Move subtree into the gap
            lft_jump = insertion_point - cur_lft
            await session.execute(
                update(Issue)
                .where(
                    Issue.root_id == old_root_id,
                    Issue.lft >= cur_lft,
                    Issue.rgt <= cur_rgt,
                )
                .values(
                    lft=Issue.lft + lft_jump,
                    rgt=Issue.rgt + lft_jump,
                    root_id=new_root_id,
                )
            )

            # Step 4: Close gap at old position
            # After the move, the old positions may have shifted — use cur_lft/cur_rgt
            await session.execute(
                update(Issue)
                .where(Issue.root_id == (new_root_id if same_tree else old_root_id), Issue.lft > cur_rgt)
                .values(lft=Issue.lft - subtree_width)
            )
            await session.execute(
                update(Issue)
                .where(Issue.root_id == (new_root_id if same_tree else old_root_id), Issue.rgt > cur_rgt)
                .values(rgt=Issue.rgt - subtree_width)
            )

            issue.parent_id = new_parent.id
            await session.flush()
            await session.refresh(issue)

        logger.info(
            "Moved issue id=%d from root_id=%s to parent_id=%s root_id=%s",
            issue.id,
            old_root_id,
            new_parent.id if new_parent else None,
            issue.root_id,
        )

    # ------------------------------------------------------------------
    # Subtree queries
    # ------------------------------------------------------------------

    async def get_descendants(self, session: AsyncSession, issue: Issue) -> list[Issue]:
        """Get all descendants of issue (all nodes in its subtree, excluding itself).

        Uses: WHERE root_id = ? AND lft > ? AND rgt < ?
        Result is ordered by lft (depth-first traversal order).
        """
        if issue.root_id is None:
            return []
        result = await session.execute(
            select(Issue)
            .where(
                Issue.root_id == issue.root_id,
                Issue.lft > issue.lft,
                Issue.rgt < issue.rgt,
            )
            .order_by(Issue.lft)
        )
        return list(result.scalars().all())

    async def get_ancestors(self, session: AsyncSession, issue: Issue) -> list[Issue]:
        """Get all ancestors of issue (from root to direct parent).

        Uses: WHERE root_id = ? AND lft < ? AND rgt > ?
        Result is ordered by lft (root-first order).
        """
        if issue.root_id is None:
            return []
        result = await session.execute(
            select(Issue)
            .where(
                Issue.root_id == issue.root_id,
                Issue.lft < issue.lft,
                Issue.rgt > issue.rgt,
            )
            .order_by(Issue.lft)
        )
        return list(result.scalars().all())

    async def get_direct_children(self, session: AsyncSession, issue: Issue) -> list[Issue]:
        """Get direct children only (parent_id == issue.id)."""
        result = await session.execute(select(Issue).where(Issue.parent_id == issue.id).order_by(Issue.lft))
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Relationship checks
    # ------------------------------------------------------------------

    async def is_ancestor_of(self, session: AsyncSession, issue: Issue, potential_descendant: Issue) -> bool:
        """Check if issue is an ancestor of potential_descendant.

        True when issue.lft < potential_descendant.lft < potential_descendant.rgt < issue.rgt
        and they share the same root_id.
        """
        if issue.root_id is None or potential_descendant.root_id is None:
            return False
        if issue.root_id != potential_descendant.root_id:
            return False
        return issue.lft < potential_descendant.lft and potential_descendant.rgt < issue.rgt

    async def is_descendant_of(self, session: AsyncSession, issue: Issue, potential_ancestor: Issue) -> bool:
        """Check if issue is a descendant of potential_ancestor."""
        return await self.is_ancestor_of(session, potential_ancestor, issue)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate_parent(self, session: AsyncSession, issue: Issue, parent: Issue) -> None:
        """Validate that a parent assignment is valid.

        Raises ValidationError if:
        - parent is the issue itself (self-reference)
        - parent is a descendant of issue (would create a cycle)
        - the resulting depth would exceed MAX_DEPTH
        """
        if parent.id == issue.id:
            raise ValidationError(
                message="An issue cannot be its own parent.",
                field="parent_id",
            )

        # Check cycle: parent must not be a descendant of issue
        if issue.root_id is not None and await self.is_ancestor_of(session, issue, parent):
            raise ValidationError(
                message="Cannot set parent to a descendant of this issue. This would create a cycle in the hierarchy.",
                field="parent_id",
            )

        # Check max depth
        ancestors = await self.get_ancestors(session, parent)
        # New depth = parent's depth + 1 (parent's ancestors + parent itself)
        new_depth = len(ancestors) + 1
        if new_depth >= MAX_DEPTH:
            raise ValidationError(
                message=f"Maximum hierarchy depth of {MAX_DEPTH} would be exceeded.",
                field="parent_id",
                details={"max_depth": MAX_DEPTH, "current_depth": new_depth},
            )

    # ------------------------------------------------------------------
    # Parent attribute derivation
    # ------------------------------------------------------------------

    async def recalculate_parent_attributes(self, session: AsyncSession, parent: Issue) -> None:
        """Recalculate parent's derived attributes from its direct children.

        Updates:
        - done_ratio: weighted average by estimated_hours; equal weight if no estimates
        - start_date: MIN of children's start_dates (ignores None)
        - due_date: MAX of children's due_dates (ignores None)

        Only updates the parent; does NOT recurse up the tree.
        Callers are responsible for recursing if needed.
        """
        children = await self.get_direct_children(session, parent)
        if not children:
            # No children — nothing to derive
            return

        # --- done_ratio: weighted average by estimated_hours ---
        total_hours = sum((c.estimated_hours or Decimal(0)) for c in children)
        if total_hours > 0:
            weighted_sum = sum(c.done_ratio * (c.estimated_hours or Decimal(0)) for c in children)
            parent.done_ratio = int(round(weighted_sum / total_hours))
        else:
            # Equal weight when no estimates
            parent.done_ratio = int(round(sum(c.done_ratio for c in children) / len(children)))

        # --- start_date: MIN of children (exclude None) ---
        start_dates = [c.start_date for c in children if c.start_date is not None]
        if start_dates:
            parent.start_date = min(start_dates)

        # --- due_date: MAX of children (exclude None) ---
        due_dates = [c.due_date for c in children if c.due_date is not None]
        if due_dates:
            parent.due_date = max(due_dates)

        await session.flush()
        logger.debug(
            "Recalculated parent id=%d: done_ratio=%d start_date=%s due_date=%s",
            parent.id,
            parent.done_ratio,
            parent.start_date,
            parent.due_date,
        )

    async def recalculate_ancestors(self, session: AsyncSession, issue: Issue) -> None:
        """Recalculate attributes for all ancestors of issue, bottom-up."""
        ancestors = await self.get_ancestors(session, issue)
        # Reverse to process bottom-up (closest parent first)
        for ancestor in reversed(ancestors):
            await self.recalculate_parent_attributes(session, ancestor)
