"""Email delivery channel — wraps the existing Celery SMTP task."""

from __future__ import annotations

import logging

from specivo.services.channels.base import NotificationPayload
from specivo.tasks.notifications import send_notification_email

logger = logging.getLogger(__name__)


class EmailChannel:
    """Email delivery channel."""

    @property
    def channel_key(self) -> str:
        return "email"

    def is_configured_for_user(self, user_channel_config: dict | None) -> bool:
        if user_channel_config is None:
            return False
        return bool(user_channel_config.get("email"))

    def dispatch(self, payload: NotificationPayload, user_channel_config: dict) -> None:
        to_email = user_channel_config["email"]
        subject = payload.title
        body_html = payload.body_html or f"<p>{payload.body_plain}</p>"
        send_notification_email.delay(to_email, subject, body_html)
        logger.debug("Queued email to %s: %s", to_email, subject)
