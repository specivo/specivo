"""Email delivery channel — wraps the existing Celery SMTP task."""

from __future__ import annotations

import logging
from html import escape

from specivo.core.mail_headers import issue_email_subject, notification_headers
from specivo.services.channels.base import NotificationPayload
from specivo.tasks.notifications import send_notification_email

logger = logging.getLogger(__name__)


class EmailChannel:
    """Email delivery channel.

    Every mail about one issue has the same subject and refers to the same
    thread root, so mail clients group them into one conversation; what
    happened goes on the first line of the body instead.
    """

    @property
    def channel_key(self) -> str:
        return "email"

    def is_configured_for_user(self, user_channel_config: dict | None) -> bool:
        if user_channel_config is None:
            return False
        return bool(user_channel_config.get("email"))

    def dispatch(self, payload: NotificationPayload, user_channel_config: dict) -> None:
        to_email = user_channel_config["email"]
        body_html = payload.body_html or f"<p>{escape(payload.body_plain)}</p>"
        headers: dict[str, str] | None = None
        if payload.entity_type == "issue" and payload.issue_key:
            subject = issue_email_subject(payload.issue_key, payload.issue_subject or "")
            headers = notification_headers(
                issue_id=payload.entity_id,
                issue_key=payload.issue_key,
                project_key=payload.project_key,
                event_type=payload.event_type,
            )
            paragraphs = [payload.event_text or payload.title, subject, payload.comment_text]
        else:
            subject = payload.title
            paragraphs = [payload.event_text or payload.title, payload.body_plain]
        paragraphs.append(payload.entity_url)
        body_plain = "\n\n".join(p for p in paragraphs if p) + "\n"
        send_notification_email.delay(to_email, subject, body_html, body_plain, headers)
        logger.debug("Queued email to %s: %s", to_email, subject)
