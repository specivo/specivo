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
def send_notification_email(
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

    Retries up to 3 times on transient failures (connection errors, timeouts,
    4xx replies). Permanent failures fail the task at once, and a failure
    after the server accepted the mail is only logged, so it is never sent twice.
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

    sent = False
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout) as server:
            if settings.smtp_tls:
                import ssl

                server.ehlo()
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_from, [to_email], msg.as_string())
            sent = True
    except (smtplib.SMTPException, OSError) as exc:
        if sent:
            # The server accepted the mail; only closing the session failed.
            # Retrying would deliver it a second time.
            logger.warning("Email to %s sent, but closing the SMTP session failed: %s", to_email, exc)
            return
        if not _is_transient(exc):
            logger.error("Failed to send email to %s, not retrying: %s", to_email, exc)
            raise
        logger.warning("Failed to send email to %s: %s", to_email, exc)
        raise self.retry(exc=exc)  # noqa: B904
    logger.info("Notification email sent to %s: %s", to_email, msg["Subject"])


def _is_transient(exc: BaseException) -> bool:
    """Whether a send failure may succeed on retry: a connection problem, a timeout or a 4xx reply.

    Permanent failures (bad credentials, a refused recipient, a rejected
    message, a server that lacks a needed extension) return False.
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _message in exc.recipients.values()]
        return bool(codes) and all(400 <= code < 500 for code in codes)
    if isinstance(exc, smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPException):
        return False
    # Socket-level errors: refused or reset connections, timeouts, DNS failures.
    return isinstance(exc, OSError)
