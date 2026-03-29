"""NotificationService — orchestrate email and in-app notifications for issue events."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.notification_templates import (
    ASSIGNMENT_EMAIL_SUBJECT,
    ASSIGNMENT_IN_APP_TITLE,
    COMMENT_EMAIL_SUBJECT,
    COMMENT_IN_APP_TITLE,
    ISSUE_UPDATED_EMAIL_SUBJECT,
    ISSUE_UPDATED_IN_APP_TITLE,
)
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.notification import Notification, NotificationPreference
from specivo.models.user import User
from specivo.services.watcher_service import WatcherService
from specivo.tasks.notifications import send_notification_email

logger = logging.getLogger(__name__)

_EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "_shared"
_email_env = Environment(
    loader=FileSystemLoader(str(_EMAIL_TEMPLATES_DIR)),
    autoescape=True,
)


def _render_email(template_name: str, **context: object) -> str:
    """Render an email body from a Jinja2 template in ``_shared/email/``."""
    template = _email_env.get_template(f"email/{template_name}")
    return template.render(**context)


@dataclass(frozen=True)
class _NotifPrefs:
    """Resolved notification preferences for a (user, project, event_type)."""

    email_enabled: bool = True
    in_app_enabled: bool = True


class NotificationService:
    """Determine recipients and queue notification emails.

    Core rules:
    - **No self-notification**: the actor (who made the change) never receives
      a notification for their own action.
    - **Dedup**: if a user qualifies via multiple channels (e.g. watcher AND
      assignee), they receive only one email.
    - **Preferences**: ``NotificationPreference`` can disable notifications per
      event type, optionally scoped to a project.  No record = default enabled.
    """

    def __init__(self) -> None:
        self._watcher_service = WatcherService()

    async def notify_assignment(
        self,
        session: AsyncSession,
        issue: Issue,
        old_assignee_id: int | None,
        new_assignee_id: int | None,
        actor: User,
    ) -> None:
        """Notify the new assignee about the assignment change.

        Skipped when: assignee is the actor, or preferences disable it.
        """
        if new_assignee_id is None or new_assignee_id == actor.id:
            return

        prefs = await self._get_prefs(session, new_assignee_id, issue.project_id, "assignment")

        # Load assignee user for email
        result = await session.execute(select(User).where(User.id == new_assignee_id))
        assignee = result.scalar_one_or_none()
        if assignee is None:
            return

        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
        }

        if prefs.email_enabled:
            subject = ASSIGNMENT_EMAIL_SUBJECT.format(**ctx)
            body = _render_email("assignment.html", **ctx)
            await self._queue_email(assignee.email, subject, body)

        if prefs.in_app_enabled:
            title = ASSIGNMENT_IN_APP_TITLE.format(**ctx)
            await self.create_notification(
                session,
                user_id=new_assignee_id,
                event_type="assignment",
                entity_type="issue",
                entity_id=issue.id,
                project_id=issue.project_id,
                actor_id=actor.id,
                title=title,
                body=f"{issue.subject}",
            )

    async def notify_watchers(
        self,
        session: AsyncSession,
        issue: Issue,
        event_type: str,
        actor: User,
        *,
        exclude_user_ids: set[int] | None = None,
    ) -> None:
        """Notify all watchers of an issue about an event, excluding the actor.

        Parameters
        ----------
        exclude_user_ids:
            Additional user IDs to skip (e.g. the assignee who was already
            notified separately).
        """
        watchers = await self._watcher_service.list_watchers(session, issue)
        skip = {actor.id}
        if exclude_user_ids:
            skip |= exclude_user_ids

        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
        }

        for watcher_user in watchers:
            if watcher_user.id in skip:
                continue
            prefs = await self._get_prefs(session, watcher_user.id, issue.project_id, event_type)

            if prefs.email_enabled:
                subject = ISSUE_UPDATED_EMAIL_SUBJECT.format(**ctx)
                body = _render_email("issue_updated.html", **ctx)
                await self._queue_email(watcher_user.email, subject, body)

            if prefs.in_app_enabled:
                title = ISSUE_UPDATED_IN_APP_TITLE.format(**ctx)
                await self.create_notification(
                    session,
                    user_id=watcher_user.id,
                    event_type=event_type,
                    entity_type="issue",
                    entity_id=issue.id,
                    project_id=issue.project_id,
                    actor_id=actor.id,
                    title=title,
                    body=f"{issue.subject}",
                )

    async def notify_comment(
        self,
        session: AsyncSession,
        issue: Issue,
        journal: Journal,
        actor: User,
    ) -> None:
        """Notify watchers and assignee about a new comment, with dedup.

        The actor is always excluded. If the assignee is also a watcher,
        they receive only one notification.
        """
        notified_ids: set[int] = {actor.id}

        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
            "comment_text": journal.notes,
        }

        # Notify assignee first (if different from actor)
        if issue.assigned_to_id and issue.assigned_to_id not in notified_ids:
            prefs = await self._get_prefs(session, issue.assigned_to_id, issue.project_id, "comment")
            result = await session.execute(select(User).where(User.id == issue.assigned_to_id))
            assignee = result.scalar_one_or_none()
            if assignee is not None:
                if prefs.email_enabled:
                    subject = COMMENT_EMAIL_SUBJECT.format(**ctx)
                    body_html = _render_email("comment.html", **ctx)
                    await self._queue_email(assignee.email, subject, body_html)
                if prefs.in_app_enabled:
                    title = COMMENT_IN_APP_TITLE.format(**ctx)
                    await self.create_notification(
                        session,
                        user_id=assignee.id,
                        event_type="comment",
                        entity_type="issue",
                        entity_id=issue.id,
                        project_id=issue.project_id,
                        actor_id=actor.id,
                        title=title,
                        body=journal.notes,
                    )
                notified_ids.add(assignee.id)

        # Notify watchers (skip already-notified)
        watchers = await self._watcher_service.list_watchers(session, issue)
        for watcher_user in watchers:
            if watcher_user.id in notified_ids:
                continue
            prefs = await self._get_prefs(session, watcher_user.id, issue.project_id, "comment")

            if prefs.email_enabled:
                subject = COMMENT_EMAIL_SUBJECT.format(**ctx)
                body_html = _render_email("comment.html", **ctx)
                await self._queue_email(watcher_user.email, subject, body_html)
            if prefs.in_app_enabled:
                title = COMMENT_IN_APP_TITLE.format(**ctx)
                await self.create_notification(
                    session,
                    user_id=watcher_user.id,
                    event_type="comment",
                    entity_type="issue",
                    entity_id=issue.id,
                    project_id=issue.project_id,
                    actor_id=actor.id,
                    title=title,
                    body=journal.notes,
                )
            notified_ids.add(watcher_user.id)

    # ------------------------------------------------------------------
    # In-app notification CRUD
    # ------------------------------------------------------------------

    async def create_notification(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        event_type: str,
        entity_type: str,
        entity_id: int,
        project_id: int,
        actor_id: int,
        title: str,
        body: str | None = None,
    ) -> Notification:
        """Create an in-app notification record."""
        notif = Notification(
            user_id=user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            project_id=project_id,
            actor_id=actor_id,
            title=title,
            body=body,
        )
        session.add(notif)
        await session.flush()
        await session.refresh(notif)
        logger.debug("Created in-app notification %s for user %s: %s", notif.id, user_id, title)
        return notif

    async def list_notifications(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        unread_only: bool = False,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[Notification], int]:
        """List notifications for a user, newest first.

        Returns (items, total_count).
        """
        base = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            base = base.where(Notification.is_read.is_(False))

        # Total count
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_stmt)).scalar_one()

        # Items — unread first, then by created_at descending
        items_stmt = (
            base.order_by(
                Notification.is_read.asc(),
                Notification.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(items_stmt)
        items = list(result.scalars().all())

        return items, total

    async def get_unread_count(self, session: AsyncSession, user_id: int) -> int:
        """Count unread notifications for a user."""
        stmt = select(func.count()).where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
        return (await session.execute(stmt)).scalar_one()

    async def mark_read(
        self,
        session: AsyncSession,
        notification_id: int,
        user_id: int,
    ) -> Notification | None:
        """Mark a single notification as read. Returns None if not found."""
        result = await session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        )
        notif = result.scalar_one_or_none()
        if notif is None:
            return None
        if not notif.is_read:
            notif.is_read = True
            notif.read_at = utcnow()
            await session.flush()
            await session.refresh(notif)
        return notif

    async def mark_all_read(self, session: AsyncSession, user_id: int) -> int:
        """Mark all unread notifications as read for a user. Returns count."""
        now = utcnow()
        stmt = (
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=now)
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount  # type: ignore[no-any-return, attr-defined]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_prefs(
        self,
        session: AsyncSession,
        user_id: int,
        project_id: int,
        event_type: str,
    ) -> _NotifPrefs:
        """Resolve notification preferences for a (user, project, event_type).

        Lookup order:
        1. Project-specific preference for (user, project, event_type)
        2. Global preference for (user, None, event_type)
        3. Default: both email and in_app enabled
        """
        # Check project-specific first
        result = await session.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.project_id == project_id,
                NotificationPreference.event_type == event_type,
            )
        )
        pref = result.scalar_one_or_none()
        if pref is not None:
            return _NotifPrefs(email_enabled=pref.email_enabled, in_app_enabled=pref.in_app_enabled)

        # Check global preference
        result = await session.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.project_id.is_(None),
                NotificationPreference.event_type == event_type,
            )
        )
        pref = result.scalar_one_or_none()
        if pref is not None:
            return _NotifPrefs(email_enabled=pref.email_enabled, in_app_enabled=pref.in_app_enabled)

        # Default: both enabled
        return _NotifPrefs()

    async def _queue_email(self, to_email: str, subject: str, body_html: str) -> None:
        """Enqueue an email for async delivery via Celery."""
        send_notification_email.delay(to_email, subject, body_html)
        logger.debug("Queued notification email to %s: %s", to_email, subject)
