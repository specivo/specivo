"""WatcherService — manage issue watchers (subscriptions)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.user import User
from specivo.models.watcher import Watcher

logger = logging.getLogger(__name__)


class WatcherService:
    """Service layer for watching/unwatching issues."""

    async def watch(self, session: AsyncSession, issue: Issue, user: User) -> Watcher:
        """Subscribe a user to an issue.

        If the user is already watching, returns the existing watcher (idempotent).
        """
        existing = await session.execute(
            select(Watcher).where(
                Watcher.issue_id == issue.id,
                Watcher.user_id == user.id,
            )
        )
        watcher = existing.scalar_one_or_none()
        if watcher is not None:
            return watcher

        try:
            watcher = Watcher(issue_id=issue.id, wiki_page_id=None, user_id=user.id)
            session.add(watcher)
            await session.flush()
        except IntegrityError:
            await session.rollback()
            result = await session.execute(
                select(Watcher).where(Watcher.issue_id == issue.id, Watcher.user_id == user.id)
            )
            watcher = result.scalar_one()

        logger.debug("User %d is now watching issue %s", user.id, issue.display_key)
        return watcher

    async def unwatch(self, session: AsyncSession, issue: Issue, user: User) -> None:
        """Unsubscribe a user from an issue.

        No-op if the user is not watching.
        """
        result = await session.execute(
            select(Watcher).where(
                Watcher.issue_id == issue.id,
                Watcher.user_id == user.id,
            )
        )
        watcher = result.scalar_one_or_none()
        if watcher is not None:
            await session.delete(watcher)
            await session.flush()
            logger.debug("User %d unwatched issue %s", user.id, issue.display_key)

    async def is_watching(self, session: AsyncSession, issue: Issue, user: User) -> bool:
        """Return True if the user is currently watching the issue."""
        result = await session.execute(
            select(Watcher).where(
                Watcher.issue_id == issue.id,
                Watcher.user_id == user.id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def list_watchers(self, session: AsyncSession, issue: Issue) -> list[User]:
        """Return the list of users watching the issue, ordered by user id.

        Uses a single JOIN to fetch User rows directly — avoids the two-query
        pattern (watchers + selectinload user).
        """
        stmt = (
            select(User)
            .join(Watcher, Watcher.user_id == User.id)
            .where(Watcher.issue_id == issue.id)
            .order_by(User.id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def auto_watch(self, session: AsyncSession, issue: Issue, user: User) -> None:
        """Auto-subscribe a user to an issue (called on create and assignment).

        Idempotent — safe to call multiple times for the same user/issue pair.
        """
        await self.watch(session, issue, user)
