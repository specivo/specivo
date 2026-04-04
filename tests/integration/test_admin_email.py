"""Integration tests for admin email API endpoint.

Tests cover:
- Send test email (admin only, non-admin gets 403)
- SMTP success and failure scenarios (mocked)
- Validation (missing fields, invalid email)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

TEST_EMAIL_URL = "/api/v1/admin/test-email/"


class TestSendTestEmail:
    async def test_non_admin_gets_403(self, auth_client: AsyncClient):
        resp = await auth_client.post(
            TEST_EMAIL_URL,
            json={
                "to": "test@example.com",
                "subject": "Test",
                "body": "Hello",
            },
        )
        assert resp.status_code == 403

    async def test_unauthenticated_gets_401(self, client: AsyncClient):
        resp = await client.post(
            TEST_EMAIL_URL,
            json={
                "to": "test@example.com",
                "subject": "Test",
                "body": "Hello",
            },
        )
        assert resp.status_code == 401

    async def test_success(self, admin_client: AsyncClient):
        mock_smtp = MagicMock()
        with patch("specivo.api.v1.admin.email.smtplib.SMTP", return_value=mock_smtp):
            resp = await admin_client.post(
                TEST_EMAIL_URL,
                json={
                    "to": "recipient@example.com",
                    "subject": "Specivo test email",
                    "body": "Test body",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["error"] is None
        mock_smtp.sendmail.assert_called_once()
        mock_smtp.quit.assert_called_once()

    async def test_smtp_failure_returns_error(self, admin_client: AsyncClient):
        import smtplib

        mock_smtp = MagicMock()
        mock_smtp.sendmail.side_effect = smtplib.SMTPConnectError(421, "Connection refused")
        with patch("specivo.api.v1.admin.email.smtplib.SMTP", return_value=mock_smtp):
            resp = await admin_client.post(
                TEST_EMAIL_URL,
                json={
                    "to": "recipient@example.com",
                    "subject": "Specivo test email",
                    "body": "Test body",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "Connection refused" in data["error"]

    async def test_smtp_connect_timeout(self, admin_client: AsyncClient):
        with patch(
            "specivo.api.v1.admin.email.smtplib.SMTP",
            side_effect=OSError("Connection timed out"),
        ):
            resp = await admin_client.post(
                TEST_EMAIL_URL,
                json={
                    "to": "recipient@example.com",
                    "subject": "Test",
                    "body": "Body",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "timed out" in data["error"]

    async def test_invalid_email_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            TEST_EMAIL_URL,
            json={
                "to": "not-an-email",
                "subject": "Test",
                "body": "Hello",
            },
        )
        assert resp.status_code == 422

    async def test_missing_fields_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            TEST_EMAIL_URL,
            json={"to": "test@example.com"},
        )
        assert resp.status_code == 422
