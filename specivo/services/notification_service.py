"""NotificationService — orchestrate multi-channel and in-app notifications for issue events."""

from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.notification_templates import (
    ASSIGNMENT_IN_APP_TITLE,
    COMMENT_IN_APP_TITLE,
    ISSUE_UPDATED_IN_APP_TITLE,
)
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.notification import Notification, NotificationPreference
from specivo.models.user import User
from specivo.services.channels.base import NotificationPayload
from specivo.services.channels.registry import get_all_channels
from specivo.services.watcher_service import WatcherService

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


class NotificationService:
    """Determine recipients and dispatch notifications via registered channels.

    Core rules:
    - **No self-notification**: the actor (who made the change) never receives
      a notification for their own action.
    - **Dedup**: if a user qualifies via multiple channels (e.g. watcher AND
      assignee), they receive only one dispatch.
    - **Preferences**: ``NotificationPreference.channels`` JSONB can disable
      notifications per event type, optionally scoped to a project.
      No record = default enabled.
    """

    def __init__(self) -> None:
        self._watcher_service = WatcherService()

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    async def notify_assignment(
        self,
        session: AsyncSession,
        issue: Issue,
        old_assignee_id: int | None,
        new_assignee_id: int | None,
        actor: User,
    ) -> None:
        """Notify the new assignee about the assignment change."""
        if new_assignee_id is None or new_assignee_id == actor.id:
            return

        result = await session.execute(select(User).where(User.id == new_assignee_id))
        assignee = result.scalar_one_or_none()
        if assignee is None:
            return

        payload = self._build_assignment_payload(issue=issue, actor=actor)
        await self._dispatch_to_channels(
            session,
            user=assignee,
            project_id=issue.project_id,
            event_type="assignment",
            payload=payload,
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
        """Notify all watchers of an issue about an event, excluding the actor."""
        watchers = await self._watcher_service.list_watchers(session, issue)
        skip = {actor.id}
        if exclude_user_ids:
            skip |= exclude_user_ids

        payload = self._build_watcher_payload(issue=issue, event_type=event_type, actor=actor)

        for watcher_user in watchers:
            if watcher_user.id in skip:
                continue
            await self._dispatch_to_channels(
                session,
                user=watcher_user,
                project_id=issue.project_id,
                event_type=event_type,
                payload=payload,
            )

    async def notify_comment(
        self,
        session: AsyncSession,
        issue: Issue,
        journal: Journal,
        actor: User,
    ) -> None:
        """Notify watchers and assignee about a new comment, with dedup."""
        notified_ids: set[int] = {actor.id}
        payload = self._build_comment_payload(issue=issue, journal=journal, actor=actor)

        # Notify assignee first (if different from actor)
        if issue.assigned_to_id and issue.assigned_to_id not in notified_ids:
            result = await session.execute(select(User).where(User.id == issue.assigned_to_id))
            assignee = result.scalar_one_or_none()
            if assignee is not None:
                await self._dispatch_to_channels(
                    session,
                    user=assignee,
                    project_id=issue.project_id,
                    event_type="comment",
                    payload=payload,
                )
                notified_ids.add(assignee.id)

        # Notify watchers (skip already-notified)
        watchers = await self._watcher_service.list_watchers(session, issue)
        for watcher_user in watchers:
            if watcher_user.id in notified_ids:
                continue
            await self._dispatch_to_channels(
                session,
                user=watcher_user,
                project_id=issue.project_id,
                event_type="comment",
                payload=payload,
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
        """List notifications for a user, newest first."""
        base = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            base = base.where(Notification.is_read.is_(False))

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_stmt)).scalar_one()

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
    # Channel dispatch
    # ------------------------------------------------------------------

    async def _dispatch_to_channels(
        self,
        session: AsyncSession,
        *,
        user: User,
        project_id: int,
        event_type: str,
        payload: NotificationPayload,
    ) -> None:
        """Dispatch a notification to all enabled and configured channels for a user."""
        prefs = await self._get_prefs(session, user.id, project_id, event_type)

        # In-app (not a channel — direct DB write)
        if self._is_channel_enabled(prefs, "in_app"):
            await self.create_notification(
                session,
                user_id=user.id,
                event_type=payload.event_type,
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                project_id=payload.project_id,
                actor_id=payload.actor_id,
                title=payload.title,
                body=payload.body_plain,
            )

        # External channels
        for key, channel in get_all_channels().items():
            if not self._is_channel_enabled(prefs, key):
                continue

            # Email config is special-cased from User.email
            if key == "email":
                user_config: dict = {"email": user.email}
            else:
                user_config = {}  # Future: load from NotificationChannelConfig

            if not channel.is_configured_for_user(user_config):
                continue
            try:
                channel.dispatch(payload, user_config)
            except Exception:
                logger.exception("Failed to dispatch %s notification to user %s", key, user.id)

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    def _build_assignment_payload(self, *, issue: Issue, actor: User) -> NotificationPayload:
        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
        }
        return NotificationPayload(
            event_type="assignment",
            entity_type="issue",
            entity_id=issue.id,
            project_id=issue.project_id,
            project_key=issue.display_key.split("-")[0],
            actor_name=actor.display_name,
            actor_id=actor.id,
            title=str(ASSIGNMENT_IN_APP_TITLE.format(**ctx)),
            body_plain=f"Issue {issue.display_key} has been assigned to you by {actor.display_name}.",
            body_html=_render_email("assignment.html", **ctx),
            issue_key=issue.display_key,
            issue_subject=issue.subject,
        )

    def _build_watcher_payload(self, *, issue: Issue, event_type: str, actor: User) -> NotificationPayload:
        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
        }
        return NotificationPayload(
            event_type=event_type,
            entity_type="issue",
            entity_id=issue.id,
            project_id=issue.project_id,
            project_key=issue.display_key.split("-")[0],
            actor_name=actor.display_name,
            actor_id=actor.id,
            title=str(ISSUE_UPDATED_IN_APP_TITLE.format(**ctx)),
            body_plain=f"Issue {issue.display_key} was updated by {actor.display_name}.",
            body_html=_render_email("issue_updated.html", **ctx),
            issue_key=issue.display_key,
            issue_subject=issue.subject,
        )

    def _build_comment_payload(self, *, issue: Issue, journal: Journal, actor: User) -> NotificationPayload:
        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
            "comment_text": journal.notes,
        }
        return NotificationPayload(
            event_type="comment",
            entity_type="issue",
            entity_id=issue.id,
            project_id=issue.project_id,
            project_key=issue.display_key.split("-")[0],
            actor_name=actor.display_name,
            actor_id=actor.id,
            title=str(COMMENT_IN_APP_TITLE.format(**ctx)),
            body_plain=journal.notes or "",
            body_html=_render_email("comment.html", **ctx),
            issue_key=issue.display_key,
            issue_subject=issue.subject,
            comment_text=journal.notes,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_channel_enabled(self, prefs: dict, channel_key: str) -> bool:
        """Check if a channel is enabled. Missing key = enabled (default-on)."""
        return prefs.get(channel_key, True)

    async def _get_prefs(
        self,
        session: AsyncSession,
        user_id: int,
        project_id: int,
        event_type: str,
    ) -> dict:
        """Resolve notification preferences for a (user, project, event_type).

        Returns the ``channels`` JSONB dict from the preference record.
        Empty dict means all channels are enabled (default).

        Lookup order:
        1. Project-specific preference for (user, project, event_type)
        2. Global preference for (user, None, event_type)
        3. Default: {} (all enabled)
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
            return pref.channels

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
            return pref.channels

        # Default: all enabled
        return {}
