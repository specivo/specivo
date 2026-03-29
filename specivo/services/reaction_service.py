"""ReactionService — add, remove, and list emoji reactions on journals."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.models.journal import Journal
from specivo.models.reaction import Reaction
from specivo.models.user import User

logger = logging.getLogger(__name__)


@dataclass
class ReactionGroup:
    """A group of reactions for a single emoji."""

    emoji: str
    count: int
    users: list[dict]


class ReactionService:
    """Service layer for journal reactions."""

    async def add_reaction(
        self,
        session: AsyncSession,
        journal_id: int,
        user: User,
        emoji: str,
    ) -> Reaction:
        """Add a reaction to a journal entry.

        Raises ``NotFoundError`` if the journal does not exist.
        Raises ``ConflictError`` if the user already reacted with this emoji.
        """
        # Check journal exists
        result = await session.execute(select(Journal).where(Journal.id == journal_id))
        if result.scalar_one_or_none() is None:
            raise NotFoundError(f"Journal {journal_id} not found")

        # Check duplicate
        result = await session.execute(
            select(Reaction).where(
                Reaction.journal_id == journal_id,
                Reaction.user_id == user.id,
                Reaction.emoji == emoji,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise ConflictError(f"You already reacted with {emoji}")

        reaction = Reaction(
            journal_id=journal_id,
            user_id=user.id,
            emoji=emoji,
        )
        session.add(reaction)
        await session.flush()
        await session.refresh(reaction)
        logger.debug(
            "Added reaction %s on journal %d by user %d",
            emoji,
            journal_id,
            user.id,
        )
        return reaction

    async def remove_reaction(
        self,
        session: AsyncSession,
        journal_id: int,
        user: User,
        emoji: str,
    ) -> None:
        """Remove a reaction from a journal entry.

        Raises ``NotFoundError`` if the reaction does not exist.
        """
        result = await session.execute(
            select(Reaction).where(
                Reaction.journal_id == journal_id,
                Reaction.user_id == user.id,
                Reaction.emoji == emoji,
            )
        )
        reaction = result.scalar_one_or_none()
        if reaction is None:
            raise NotFoundError("Reaction not found")

        await session.delete(reaction)
        await session.flush()
        logger.debug(
            "Removed reaction %s on journal %d by user %d",
            emoji,
            journal_id,
            user.id,
        )

    async def list_reactions(
        self,
        session: AsyncSession,
        journal_id: int,
    ) -> list[ReactionGroup]:
        """List reactions on a journal, grouped by emoji with user lists."""
        # Get all reactions for this journal with user info
        from sqlalchemy.orm import selectinload

        result = await session.execute(
            select(Reaction)
            .where(Reaction.journal_id == journal_id)
            .options(selectinload(Reaction.user))
            .order_by(Reaction.emoji, Reaction.created_at)
        )
        reactions = list(result.scalars().all())

        # Group by emoji
        groups: dict[str, list[Reaction]] = {}
        for r in reactions:
            groups.setdefault(r.emoji, []).append(r)

        return [
            ReactionGroup(
                emoji=emoji,
                count=len(rxns),
                users=[{"id": r.user_id, "login": r.user.login, "display_name": r.user.display_name} for r in rxns],
            )
            for emoji, rxns in groups.items()
        ]
