"""Admin email API — send test emails to verify SMTP configuration."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr

from specivo.api.v1.admin import require_admin_api
from specivo.core.config import get_settings
from specivo.models.user import User

router = APIRouter(tags=["admin"])
logger = logging.getLogger(__name__)


class TestEmailRequest(BaseModel):
    to: EmailStr
    subject: str
    body: str


class TestEmailResponse(BaseModel):
    ok: bool
    error: str | None = None


@router.post("/admin/test-email/", response_model=TestEmailResponse)
async def send_test_email(
    payload: TestEmailRequest,
    current_user: User = Depends(require_admin_api),  # noqa: B008
) -> TestEmailResponse:
    """Send a test email synchronously to verify SMTP configuration (admin only)."""
    settings = get_settings()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = payload.subject
    msg["From"] = settings.smtp_from
    msg["To"] = payload.to
    msg.attach(MIMEText(payload.body, "plain"))

    try:
        if settings.smtp_tls:
            server = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout
            )
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout
            )

        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)

        server.sendmail(settings.smtp_from, [payload.to], msg.as_string())
        server.quit()
        logger.info(
            "Test email sent by admin %s to %s", current_user.login, payload.to
        )
        return TestEmailResponse(ok=True)
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Test email failed (admin %s): %s", current_user.login, exc)
        return TestEmailResponse(ok=False, error=str(exc))
