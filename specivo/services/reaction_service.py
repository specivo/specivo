"""Reaction service — add, remove, toggle, and bulk-list emoji reactions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.constants import REACTION_EMOJI
from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.models.reaction import Reaction

if TYPE_CHECKING:
    from specivo.models.user import User

logger = logging.getLogger(__name__)


class ReactionGroup(NamedTuple):
    emoji: str
    count: int
    reacted_by_me: bool


class ReactionListGroup(NamedTuple):
    """Group used by list_reactions (per-journal, with user details)."""

    emoji: str
    count: int
    users: list[dict[str, object]]


class ReactionService:
    """Service for managing emoji reactions on journal entries."""

    async def add_reaction(
        self,
        session: AsyncSession,
        journal_id: int,
        user: User,
        emoji: str,
    ) -> Reaction:
        """Add a reaction. Returns the created Reaction row.

        Race-safe via ON CONFLICT DO NOTHING.
        """
        if emoji not in REACTION_EMOJI:
            raise ValidationError(f"Invalid emoji: {emoji}. Allowed: {', '.join(REACTION_EMOJI.keys())}")

        stmt = (
            insert(Reaction)
            .values(journal_id=journal_id, user_id=user.id, emoji=emoji)
            .on_conflict_do_nothing(constraint="uq_reaction")
            .returning(Reaction.id)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            # Already exists — return the existing reaction
            existing = await session.execute(
                select(Reaction).where(
                    Reaction.journal_id == journal_id,
                    Reaction.user_id == user.id,
                    Reaction.emoji == emoji,
                )
            )
            reaction = existing.scalar_one()
            return reaction

        await session.flush()
        reaction_obj = await session.get(Reaction, row)
        assert reaction_obj is not None
        return reaction_obj

    async def remove_reaction(
        self,
        session: AsyncSession,
        journal_id: int,
        user: User,
        emoji: str,
    ) -> None:
        """Remove a reaction."""
        result = await session.execute(
            delete(Reaction).where(
                Reaction.journal_id == journal_id,
                Reaction.user_id == user.id,
                Reaction.emoji == emoji,
            )
        )
        if result.rowcount == 0:
            raise NotFoundError("Reaction not found")

    async def list_reactions(
        self,
        session: AsyncSession,
        journal_id: int,
    ) -> list[ReactionListGroup]:
        """List reactions for a single journal, grouped by emoji with user details."""
        stmt = (
            select(Reaction)
            .where(Reaction.journal_id == journal_id)
            .options(selectinload(Reaction.user))
            .order_by(Reaction.emoji, Reaction.created_at)
        )
        rows = list((await session.execute(stmt)).scalars().all())

        groups: dict[str, ReactionListGroup] = {}
        for r in rows:
            if r.emoji not in groups:
                groups[r.emoji] = ReactionListGroup(emoji=r.emoji, count=0, users=[])
            grp = groups[r.emoji]
            groups[r.emoji] = ReactionListGroup(
                emoji=r.emoji,
                count=grp.count + 1,
                users=[*grp.users, {"id": r.user_id, "login": r.user.login, "display_name": r.user.display_name}],
            )
        return list(groups.values())

    async def toggle_reaction(
        self,
        session: AsyncSession,
        journal_id: int,
        user_id: int,
        emoji: str,
    ) -> bool:
        """Toggle a reaction. Returns True if added, False if removed.

        Race-safe: delete-first then insert with ON CONFLICT DO NOTHING.
        """
        if emoji not in REACTION_EMOJI:
            raise ValidationError(f"Invalid emoji: {emoji}. Allowed: {', '.join(REACTION_EMOJI.keys())}")

        result = await session.execute(
            delete(Reaction).where(
                Reaction.journal_id == journal_id,
                Reaction.user_id == user_id,
                Reaction.emoji == emoji,
            )
        )
        if result.rowcount > 0:
            return False  # removed

        await session.execute(
            insert(Reaction)
            .values(journal_id=journal_id, user_id=user_id, emoji=emoji)
            .on_conflict_do_nothing(constraint="uq_reaction")
        )
        return True  # added

    async def list_reactions_bulk(
        self,
        session: AsyncSession,
        journal_ids: list[int],
        current_user_id: int | None = None,
    ) -> dict[int, list[ReactionGroup]]:
        """Batch-load reactions for multiple journals in a single query.

        Returns {journal_id: [ReactionGroup(emoji, count, reacted_by_me)]}.
        """
        if not journal_ids:
            return {}

        # Build the reacted_by_me expression
        if current_user_id:
            reacted_col = func.bool_or(Reaction.user_id == current_user_id).label("reacted_by_me")
        else:
            from sqlalchemy import literal

            reacted_col = literal(False).label("reacted_by_me")

        stmt = (
            select(
                Reaction.journal_id,
                Reaction.emoji,
                func.count().label("cnt"),
                reacted_col,
            )
            .where(Reaction.journal_id.in_(journal_ids))
            .group_by(Reaction.journal_id, Reaction.emoji)
            .order_by(Reaction.journal_id, Reaction.emoji)
        )

        rows = (await session.execute(stmt)).all()
        result: dict[int, list[ReactionGroup]] = {}
        for row in rows:
            jid = row[0]
            result.setdefault(jid, []).append(ReactionGroup(emoji=row[1], count=row[2], reacted_by_me=bool(row[3])))
        return result
