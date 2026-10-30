"""MentionService — parse @mentions and create notification records."""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.reaction import Mention
from specivo.models.user import User
from specivo.services.anonymous_user_service import real_users_clause
from specivo.services.issue_service import IssueService
from specivo.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

# Regex to find @mentions: matches @username where username is alphanumeric, dots, hyphens, underscores
_MENTION_RE = re.compile(r"@([\w.-]+)")


class MentionService:
    """Service layer for @mention parsing and notification."""

    def __init__(self) -> None:
        self._notification_service = NotificationService()
        self._issue_service = IssueService()

    def parse_mentions(self, text: str) -> list[str]:
        """Extract unique usernames from @mentions in text.

        Returns a list of unique usernames (without the @ prefix).
        """
        return list(dict.fromkeys(_MENTION_RE.findall(text)))

    async def process_mentions(
        self,
        session: AsyncSession,
        journal: Journal,
        text: str,
        actor: User,
    ) -> list[Mention]:
        """Parse @mentions from text, create Mention records, and trigger notifications.

        - Only creates mentions for users that actually exist in the database.
        - Does not create a mention for the actor (no self-mention).
        - Silently ignores nonexistent usernames.
        - Notifies only the mentioned users who can see the issue.

        Returns the list of created Mention records.
        """
        usernames = self.parse_mentions(text)
        if not usernames:
            return []

        # Batch-resolve usernames to user IDs
        result = await session.execute(
            select(User).where(User.login.in_(usernames), User.status == "active", real_users_clause())
        )
        users_by_login: dict[str, User] = {u.login: u for u in result.scalars().all()}

        created_mentions: list[Mention] = []

        for username in usernames:
            mentioned_user = users_by_login.get(username)
            if mentioned_user is None:
                continue
            # Skip self-mention
            if mentioned_user.id == actor.id:
                continue

            mention = Mention(
                journal_id=journal.id,
                user_id=mentioned_user.id,
            )
            session.add(mention)
            created_mentions.append(mention)

        # Only users who can see the issue hear about it; the mention itself is
        # still recorded.
        issue = await session.get(Issue, journal.issue_id) if journal.issue_id is not None else None
        if issue is not None and created_mentions:
            mentioned_ids = [mention.user_id for mention in created_mentions]
            title = f"You were mentioned by {actor.display_name}"
            for mentioned_user in await self._issue_service.users_who_can_view(session, issue, mentioned_ids):
                await self._notification_service.create_notification(
                    session,
                    user_id=mentioned_user.id,
                    event_type="mention",
                    entity_type="issue",
                    entity_id=issue.id,
                    project_id=issue.project_id,
                    actor_id=actor.id,
                    title=title,
                    body=text,
                )

        if created_mentions:
            await session.flush()
            logger.debug(
                "Processed %d mention(s) in journal %d",
                len(created_mentions),
                journal.id,
            )

        return created_mentions
