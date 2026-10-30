"""Celery task for sending notification emails."""

from __future__ import annotations

import logging
import smtplib

from specivo.core.config import get_settings
from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_EMAIL
from specivo.core.mail_headers import build_message
from specivo.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_EMAIL)
def send_notification_email(  # type: ignore[no-untyped-def]
    self,
    to_email: str,
    subject: str,
    body_html: str,
    body_plain: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Send a notification email via SMTP.

    *body_plain*, when given, becomes the text part next to the HTML one.
    *headers* are the extra headers from ``specivo.core.mail_headers``
    (threading, ``X-Specivo-*``); mail without them still gets a Message-ID,
    ``Date`` and ``Auto-Submitted``. Retries keep the same Message-ID.

    Retries up to 3 times on transient failures (connection errors, timeouts).
    """
    settings = get_settings()

    msg = build_message(
        from_addr=settings.smtp_from,
        to_addr=to_email,
        subject=subject,
        body_html=body_html,
        body_plain=body_plain,
        headers=headers,
    )

    try:
        if settings.smtp_tls:
            import ssl

            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout)

        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)

        server.sendmail(settings.smtp_from, [to_email], msg.as_string())
        server.quit()
        logger.info("Notification email sent to %s: %s", to_email, msg["Subject"])
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Failed to send email to %s: %s", to_email, exc)
        raise self.retry(exc=exc)
