"""NotificationService — orchestrate multi-channel and in-app notifications for issue events."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.app_url import public_url
from specivo.core.i18n import LazyString, format_datetime_for, get_locale, recipient_locale
from specivo.core.notification_templates import (
    ASSIGNMENT_EMAIL_LEAD,
    ASSIGNMENT_IN_APP_TITLE,
    COMMENT_EMAIL_LEAD,
    COMMENT_IN_APP_TITLE,
    EMAIL_VIEW_COMMENT,
    EMAIL_VIEW_ISSUE,
    ISSUE_UPDATED_EMAIL_LEAD,
    ISSUE_UPDATED_IN_APP_TITLE,
    MENTION_EMAIL_LEAD,
    MENTION_IN_APP_TITLE,
)
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.notification import Notification, NotificationPreference
from specivo.models.user import User
from specivo.services.api_key_scope import scope_clause
from specivo.services.channels.base import NotificationPayload
from specivo.services.channels.registry import get_all_channels
from specivo.services.issue_service import IssueService
from specivo.services.watcher_service import WatcherService

logger = logging.getLogger(__name__)


def _inbox_clause(user_id: int) -> ColumnElement[bool]:
    """Select *user_id*'s in-app notifications the current call may read.

    A project-limited API key reads only the notifications of its projects
    (``specivo.services.api_key_scope``); project-less ones are outside it.
    """
    own: ColumnElement[bool] = Notification.user_id == user_id
    limit = scope_clause(Notification.project_id)
    return own if limit is None else and_(own, limit)


_EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "_shared"
_email_env = Environment(
    loader=FileSystemLoader(str(_EMAIL_TEMPLATES_DIR)),
    # HTML bodies are escaped, plain-text (.txt) bodies are not.
    autoescape=select_autoescape(default_for_string=True),
)


def _render_email(template_name: str, **context: object) -> str:
    """Render an email body from a Jinja2 template in ``_shared/email/``."""
    template = _email_env.get_template(f"email/{template_name}")
    return template.render(**context)


def issue_url(issue_key: str, journal_id: int | None = None) -> str | None:
    """The public link to an issue, or to one of its comments, built from ``APP_URL``.

    ``None`` when ``APP_URL`` has no host (see ``specivo.core.app_url.public_url``).
    """
    anchor = f"#journal-{int(journal_id)}" if journal_id is not None else ""
    return public_url(f"/issue/{quote(issue_key, safe='')}/{anchor}")


# The channel keys a notification went to, by recipient user id.
Delivered = dict[int, set[str]]


class NotificationService:
    """Determine recipients and dispatch notifications via registered channels.

    Core rules:
    - **No self-notification**: the actor (who made the change) never receives
      a notification for their own action.
    - **Visibility**: only users who can see the issue when the notification
      is built receive it (``IssueService.users_who_can_view``).
    - **Dedup**: if a user qualifies via multiple channels (e.g. watcher AND
      assignee), they receive only one dispatch. Someone mentioned in a
      comment gets the mention instead of the comment notification, on every
      channel the mention used.
    - **Preferences**: ``NotificationPreference.channels`` JSONB can disable
      notifications per event type, optionally scoped to a project.
      No record = default enabled.
    - **Per recipient**: the payload is rendered for each recipient in their
      own language (their preference, else the instance default) and with
      times in their timezone, never in the actor's request locale.
    """

    def __init__(self) -> None:
        self._watcher_service = WatcherService()
        self._issue_service = IssueService()

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
        if new_assignee_id is None:
            return

        occurred_at = utcnow()
        for assignee in await self._recipients(session, issue, [new_assignee_id], actor):
            await self._notify(
                session,
                user=assignee,
                issue=issue,
                event_type="assignment",
                build=lambda user: self._build_assignment_payload(
                    issue=issue, actor=actor, recipient=user, occurred_at=occurred_at
                ),
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
        skip = exclude_user_ids or set()
        candidates = [watcher.id for watcher in watchers if watcher.id not in skip]

        occurred_at = utcnow()
        for watcher_user in await self._recipients(session, issue, candidates, actor):
            await self._notify(
                session,
                user=watcher_user,
                issue=issue,
                event_type=event_type,
                build=lambda user: self._build_watcher_payload(
                    issue=issue, event_type=event_type, actor=actor, recipient=user, occurred_at=occurred_at
                ),
            )

    async def notify_comment(
        self,
        session: AsyncSession,
        issue: Issue,
        journal: Journal,
        actor: User,
        *,
        mentioned_user_ids: Iterable[int] = (),
    ) -> None:
        """Notify about a new comment: the people it mentions, then the assignee and the watchers.

        Each user hears about the comment once per channel. Someone who is
        mentioned and also watches (or is assigned) gets the mention; a channel
        their mention preferences turned off still carries the comment
        notification when their comment preferences allow it.
        """
        occurred_at = utcnow()
        delivered = await self.notify_mentions(
            session, issue, journal, mentioned_user_ids, actor, occurred_at=occurred_at
        )

        candidates = [issue.assigned_to_id] if issue.assigned_to_id else []
        candidates += [watcher.id for watcher in await self._watcher_service.list_watchers(session, issue)]

        for user in await self._recipients(session, issue, candidates, actor):
            await self._notify(
                session,
                user=user,
                issue=issue,
                event_type="comment",
                build=lambda recipient: self._build_comment_payload(
                    issue=issue, journal=journal, actor=actor, recipient=recipient, occurred_at=occurred_at
                ),
                skip_channels=delivered.get(user.id, set()),
            )

    async def notify_mentions(
        self,
        session: AsyncSession,
        issue: Issue,
        journal: Journal,
        user_ids: Iterable[int],
        actor: User,
        *,
        occurred_at: datetime | None = None,
    ) -> Delivered:
        """Tell the users in *user_ids* that *actor* mentioned them in *journal*.

        Only those who can see the issue now hear about it, never the actor,
        and each one's ``mention`` preferences decide the channels. Returns the
        channels used per user, so the comment notification can skip them.
        """
        when = occurred_at or utcnow()
        delivered: Delivered = {}
        for user in await self._recipients(session, issue, user_ids, actor):
            delivered[user.id] = await self._notify(
                session,
                user=user,
                issue=issue,
                event_type="mention",
                build=lambda recipient: self._build_mention_payload(
                    issue=issue, journal=journal, actor=actor, recipient=recipient, occurred_at=when
                ),
            )
        return delivered

    async def _notify(
        self,
        session: AsyncSession,
        *,
        user: User,
        issue: Issue,
        event_type: str,
        build: Callable[[User], NotificationPayload],
        skip_channels: Collection[str] = (),
    ) -> set[str]:
        """Build the payload in *user*'s language, then dispatch it; returns the channels used."""
        with recipient_locale(user.language):
            payload = build(user)
        delivered: set[str] = await self._dispatch_to_channels(
            session,
            user=user,
            project_id=issue.project_id,
            event_type=event_type,
            payload=payload,
            skip_channels=skip_channels,
        )
        return delivered

    async def _recipients(
        self, session: AsyncSession, issue: Issue, user_ids: Iterable[int], actor: User
    ) -> list[User]:
        """Return the users among *user_ids* to notify about *issue*, each once, in order.

        Drops the actor and everyone who cannot see the issue right now
        (``IssueService.users_who_can_view``), with one check for all of them.
        Watchers who lost access stay watchers and are notified again once
        their access comes back.
        """
        candidates = list(dict.fromkeys(user_id for user_id in user_ids if user_id != actor.id))
        if not candidates:
            return []
        return await self._issue_service.users_who_can_view(session, issue, candidates)

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

        Like every inbox read here, narrowed to the authenticating API key's
        project limit (``_inbox_clause``).
        """
        base = select(Notification).where(_inbox_clause(user_id))
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
            _inbox_clause(user_id),
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
                _inbox_clause(user_id),
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
                _inbox_clause(user_id),
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
        skip_channels: Collection[str] = (),
    ) -> set[str]:
        """Dispatch a notification to all enabled and configured channels for a user.

        Channels in *skip_channels* (``"in_app"`` included) are left out.
        Returns the keys of the channels the notification went to.
        """
        delivered: set[str] = set()
        prefs = await self._get_prefs(session, user.id, project_id, event_type)

        # In-app (not a channel — direct DB write)
        if "in_app" not in skip_channels and self._is_channel_enabled(prefs, "in_app"):
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
            delivered.add("in_app")

        # External channels
        for key, channel in get_all_channels().items():
            if key in skip_channels or not self._is_channel_enabled(prefs, key):
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
            else:
                delivered.add(key)
        return delivered

    # ------------------------------------------------------------------
    # Payload builders
    #
    # Called inside ``recipient_locale``: the lazy templates translate into
    # the active locale, which is then the recipient's.
    # ------------------------------------------------------------------

    def _build_assignment_payload(
        self,
        *,
        issue: Issue,
        actor: User,
        recipient: User | None = None,
        occurred_at: datetime | None = None,
    ) -> NotificationPayload:
        return self._build_issue_payload(
            event_type="assignment",
            issue=issue,
            actor=actor,
            recipient=recipient,
            occurred_at=occurred_at,
            lead=ASSIGNMENT_EMAIL_LEAD,
            title=ASSIGNMENT_IN_APP_TITLE,
        )

    def _build_watcher_payload(
        self,
        *,
        issue: Issue,
        event_type: str,
        actor: User,
        recipient: User | None = None,
        occurred_at: datetime | None = None,
    ) -> NotificationPayload:
        return self._build_issue_payload(
            event_type=event_type,
            issue=issue,
            actor=actor,
            recipient=recipient,
            occurred_at=occurred_at,
            lead=ISSUE_UPDATED_EMAIL_LEAD,
            title=ISSUE_UPDATED_IN_APP_TITLE,
        )

    def _build_comment_payload(
        self,
        *,
        issue: Issue,
        journal: Journal,
        actor: User,
        recipient: User | None = None,
        occurred_at: datetime | None = None,
    ) -> NotificationPayload:
        return self._build_issue_payload(
            event_type="comment",
            issue=issue,
            actor=actor,
            recipient=recipient,
            occurred_at=occurred_at,
            lead=COMMENT_EMAIL_LEAD,
            title=COMMENT_IN_APP_TITLE,
            journal=journal,
        )

    def _build_mention_payload(
        self,
        *,
        issue: Issue,
        journal: Journal,
        actor: User,
        recipient: User | None = None,
        occurred_at: datetime | None = None,
    ) -> NotificationPayload:
        return self._build_issue_payload(
            event_type="mention",
            issue=issue,
            actor=actor,
            recipient=recipient,
            occurred_at=occurred_at,
            lead=MENTION_EMAIL_LEAD,
            title=MENTION_IN_APP_TITLE,
            journal=journal,
        )

    def _build_issue_payload(
        self,
        *,
        event_type: str,
        issue: Issue,
        actor: User,
        recipient: User | None,
        occurred_at: datetime | None,
        lead: LazyString,
        title: LazyString,
        journal: Journal | None = None,
    ) -> NotificationPayload:
        """One recipient's notification about *issue*: in-app title and body, and the mail.

        Everything user-supplied (subject, comment, names) goes into the HTML
        through the autoescaping template; the plain-text part is rendered from
        its own template. The mail links to the issue, or to the comment when
        there is one.
        """
        ctx = {
            "issue_key": issue.display_key,
            "issue_subject": issue.subject,
            "actor_name": actor.display_name,
        }
        event_text = str(lead.format(**ctx))
        comment_text = journal.notes if journal is not None else None
        url = issue_url(issue.display_key, journal.id if journal is not None else None)
        mail = {
            **ctx,
            "lead": event_text,
            "occurred_at": format_datetime_for(
                occurred_at or utcnow(), getattr(recipient, "timezone", None), get_locale()
            ),
            "comment_text": comment_text,
            "url": url,
            "link_label": str(EMAIL_VIEW_COMMENT if journal is not None else EMAIL_VIEW_ISSUE),
        }
        return NotificationPayload(
            event_type=event_type,
            entity_type="issue",
            entity_id=issue.id,
            project_id=issue.project_id,
            project_key=issue.display_key.split("-")[0],
            actor_name=actor.display_name,
            actor_id=actor.id,
            title=str(title.format(**ctx)),
            # The in-app body under the title: the comment, else the issue subject.
            body_plain=(comment_text or "") if journal is not None else issue.subject,
            body_html=_render_email("issue_event.html", **mail),
            body_text=_render_email("issue_event.txt", **mail),
            issue_key=issue.display_key,
            issue_subject=issue.subject,
            comment_text=comment_text,
            entity_url=url,
            event_text=event_text,
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
