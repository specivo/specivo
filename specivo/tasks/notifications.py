"""Celery task for sending notification emails."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from specivo.core.config import get_settings
from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_EMAIL
from specivo.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_EMAIL)
def send_notification_email(self, to_email: str, subject: str, body_html: str) -> None:  # type: ignore[no-untyped-def]
    """Send a notification email via SMTP.

    Retries up to 3 times on transient failures (connection errors, timeouts).
    """
    settings = get_settings()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg.attach(MIMEText(body_html, "html"))

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
        logger.info("Notification email sent to %s: %s", to_email, subject)
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Failed to send email to %s: %s", to_email, exc)
        raise self.retry(exc=exc)
