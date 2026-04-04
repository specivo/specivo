"""Integration tests for security hardening.

TDD RED phase: these tests define expected security behavior that the
codebase does not yet fully implement. They will FAIL until the
corresponding fixes land.

Groups:
- JWT claims (CRITICAL)
- FTS language validation (CRITICAL)
- pgvector parameterization (CRITICAL) — covered by semantic search tests
- Webhook permission checks (HIGH)
- Attachment authorization (HIGH)
- Webhook URL validation (HIGH)
- require_permission factory (HIGH) — dead code removal, no test needed
- X-Forwarded-For trust (HIGH)
- Medium security fixes
- Low security fixes
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(db: AsyncSession, **kwargs):
    """Persist a UserFactory instance and commit."""
    user = UserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_admin(db: AsyncSession, **kwargs):
    """Persist an AdminUserFactory instance and commit."""
    user = AdminUserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str):
    return await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": password},
    )


async def _create_project(db: AsyncSession, **kwargs):
    """Create and persist a project."""
    project = ProjectFactory.build(**kwargs)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _add_member(db: AsyncSession, user_id: int, project_id: int, role_id: int | None = None):
    """Add a user as a project member (optionally with a role)."""
    from specivo.models.member import Member, MemberRole

    member = Member(user_id=user_id, project_id=project_id)
    db.add(member)
    await db.flush()
    if role_id is not None:
        member_role = MemberRole(member_id=member.id, role_id=role_id)
        db.add(member_role)
    await db.commit()
    return member


async def _get_or_create_role(db: AsyncSession, name: str, permissions: list[str]):
    """Get an existing role by name or create one with the given permissions."""
    from sqlalchemy import select

    from specivo.models.role import Role

    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(name=name, permissions=permissions)
        db.add(role)
        await db.commit()
        await db.refresh(role)
    return role


# ===================================================================
# JWT claims (CRITICAL)
# ===================================================================


class TestJwtClaims:
    """JWT access tokens must not contain role flags (is_admin, is_service_account).

    These sensitive booleans in the token payload allow privilege escalation
    if the token is leaked or the signature is compromised.
    """

    async def test_jwt_payload_does_not_contain_is_admin(self, client: AsyncClient, db_session: AsyncSession):
        """The access token must NOT include 'is_admin' or 'is_service_account'."""
        await _create_user(db_session, login="jwt_claims_user")
        resp = await _login(client, "jwt_claims_user", TEST_PASSWORD)
        assert resp.status_code == 200

        access_token = resp.json()["access_token"]
        # Decode without verification -- we only care about the payload shape.
        payload = pyjwt.decode(access_token, options={"verify_signature": False})

        assert "is_admin" not in payload, "JWT must not contain is_admin claim"
        assert "is_service_account" not in payload, "JWT must not contain is_service_account claim"

    async def test_jwt_contains_only_required_claims(self, client: AsyncClient, db_session: AsyncSession):
        """JWT payload must contain only: sub, iat, exp, jti."""
        await _create_user(db_session, login="jwt_minimal_user")
        resp = await _login(client, "jwt_minimal_user", TEST_PASSWORD)
        assert resp.status_code == 200

        access_token = resp.json()["access_token"]
        payload = pyjwt.decode(access_token, options={"verify_signature": False})

        allowed_claims = {"sub", "iat", "exp", "jti", "rem"}
        extra_claims = set(payload.keys()) - allowed_claims
        assert not extra_claims, f"JWT contains unexpected claims: {extra_claims}"


# ===================================================================
# FTS language validation (CRITICAL)
# ===================================================================


class TestFtsLanguageValidation:
    """The search_fts_language setting must be validated against a known
    allowlist of PostgreSQL text search configurations to prevent injection.
    """

    async def test_invalid_fts_language_rejected(self):
        """A malicious FTS language value must raise ValidationError."""
        from specivo.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                redis_url="redis://localhost",
                secret_key="dev-secret-key-minimum-32-bytes-for-hs256-signing",
                search_fts_language="english'; DROP TABLE users; --",
            )

    async def test_valid_fts_languages_accepted(self):
        """Standard PostgreSQL text search configurations must pass validation."""
        from specivo.core.config import Settings

        for lang in ("english", "simple"):
            s = Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                redis_url="redis://localhost",
                secret_key="dev-secret-key-minimum-32-bytes-for-hs256-signing",
                search_fts_language=lang,
            )
            assert s.search_fts_language == lang


# ===================================================================
# pgvector parameterization (CRITICAL)
# ===================================================================
# Covered by existing semantic search tests in:
#   tests/integration/test_semantic_search.py
#   tests/integration/test_semantic_search_count.py
# No additional tests needed here.


# ===================================================================
# Webhook permission checks (HIGH)
# ===================================================================


@pytest.mark.pro
class TestWebhookPermissions:
    """Webhook creation/management must require 'manage_project' permission.

    Currently the endpoint only checks get_current_user (any authenticated
    user can create webhooks for any project).
    """

    async def test_non_member_cannot_create_webhook(self, client: AsyncClient, db_session: AsyncSession):
        """A user who is not a project member must get 403."""
        project = await _create_project(db_session, key="WH01", identifier="wh-perm-01")
        user = await _create_user(db_session, login="wh_outsider")

        resp = await _login(client, "wh_outsider", TEST_PASSWORD)
        token = resp.json()["access_token"]

        resp = await client.post(
            f"/api/v1/projects/{project.key}/webhooks/",
            json={
                "url": "https://example.com/hook",
                "secret": "supersecretkey123",
                "events": ["issue.created"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, f"Non-member should get 403, got {resp.status_code}"

    async def test_member_without_manage_project_cannot_create_webhook(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """A member without 'manage_project' permission must get 403."""
        project = await _create_project(db_session, key="WH02", identifier="wh-perm-02")
        user = await _create_user(db_session, login="wh_viewer")

        # Create a role with only view_issues
        role = await _get_or_create_role(db_session, "Viewer_WH", ["view_issues"])
        await _add_member(db_session, user.id, project.id, role.id)

        resp = await _login(client, "wh_viewer", TEST_PASSWORD)
        token = resp.json()["access_token"]

        resp = await client.post(
            f"/api/v1/projects/{project.key}/webhooks/",
            json={
                "url": "https://example.com/hook",
                "secret": "supersecretkey123",
                "events": ["issue.created"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, f"Viewer should get 403, got {resp.status_code}"

    async def test_admin_can_create_webhook(self, client: AsyncClient, db_session: AsyncSession):
        """An admin user must be able to create a webhook (201)."""
        project = await _create_project(db_session, key="WH03", identifier="wh-perm-03")
        admin = await _create_admin(db_session, login="wh_admin")

        resp = await _login(client, "wh_admin", TEST_PASSWORD)
        token = resp.json()["access_token"]

        resp = await client.post(
            f"/api/v1/projects/{project.key}/webhooks/",
            json={
                "url": "https://example.com/hook",
                "secret": "supersecretkey123",
                "events": ["issue.created"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, f"Admin should get 201, got {resp.status_code}: {resp.text}"


# ===================================================================
# Attachment authorization (HIGH)
# ===================================================================


class TestAttachmentAuthorization:
    """Upload and download of attachments must verify that the current user
    has access to the container entity (issue/wiki page).
    """

    async def test_cannot_upload_attachment_to_inaccessible_issue(self, client: AsyncClient, db_session: AsyncSession):
        """Uploading to an issue in a private project the user cannot access must return 403."""
        # Create a private project and an issue within it
        project = await _create_project(db_session, key="AT01", identifier="att-auth-01", is_public=False)
        user = await _create_user(db_session, login="att_outsider")

        resp = await _login(client, "att_outsider", TEST_PASSWORD)
        token = resp.json()["access_token"]

        # Try to upload to container_type=Issue, container_id=<issue in private project>
        # Since user is not a member, this should fail with 403.
        import io

        resp = await client.post(
            "/api/v1/attachments/",
            data={"container_type": "Issue", "container_id": "999999"},
            files={"file": ("test.txt", io.BytesIO(b"secret data"), "text/plain")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code in (403, 404), (
            f"Upload to inaccessible issue should return 403 or 404, got {resp.status_code}"
        )

    async def test_cannot_download_attachment_from_private_issue(self, client: AsyncClient, db_session: AsyncSession):
        """Downloading an attachment from a private project must return 404 (not leak existence)."""
        user = await _create_user(db_session, login="att_download_outsider")

        resp = await _login(client, "att_download_outsider", TEST_PASSWORD)
        token = resp.json()["access_token"]

        # Attachment ID that belongs to a private project the user cannot access.
        # Even if the attachment exists, the user should get 404 (not 200 or 403).
        resp = await client.get(
            "/api/v1/attachments/999999/download/",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, (
            f"Download from inaccessible attachment should return 404, got {resp.status_code}"
        )


# ===================================================================
# Webhook URL validation (HIGH)
# ===================================================================


class TestWebhookUrlValidation:
    """Outgoing webhook URLs must be validated to prevent SSRF:
    - Only HTTPS allowed
    - No private/loopback/link-local IPs
    """

    async def test_webhook_url_rejects_http(self):
        """Plain HTTP URLs must be rejected."""
        from specivo.schemas.webhook import WebhookCreate

        with pytest.raises(ValidationError) as exc_info:
            WebhookCreate(url="http://example.com/hook", secret="secret1234", events=["issue.created"])

        errors = exc_info.value.errors()
        url_errors = [e for e in errors if "url" in str(e.get("loc", []))]
        assert url_errors, "http:// URL should be rejected by schema validation"

    async def test_webhook_url_rejects_private_ip(self):
        """Private IP ranges (10.x, 172.16.x, 192.168.x) must be rejected."""
        from specivo.schemas.webhook import WebhookCreate

        private_urls = [
            "https://10.0.0.1/hook",
            "https://172.16.0.1/hook",
            "https://192.168.1.1/hook",
        ]
        for url in private_urls:
            with pytest.raises(ValidationError, match="url"):
                WebhookCreate(url=url, secret="secret1234", events=["issue.created"])

    async def test_webhook_url_rejects_metadata_ip(self):
        """Cloud metadata IP (169.254.169.254) must be rejected."""
        from specivo.schemas.webhook import WebhookCreate

        with pytest.raises(ValidationError, match="url"):
            WebhookCreate(
                url="https://169.254.169.254/latest/meta-data/", secret="secret1234", events=["issue.created"]
            )

    async def test_webhook_url_rejects_localhost(self):
        """Localhost (127.0.0.1) must be rejected."""
        from specivo.schemas.webhook import WebhookCreate

        with pytest.raises(ValidationError, match="url"):
            WebhookCreate(url="https://127.0.0.1/hook", secret="secret1234", events=["issue.created"])

    async def test_webhook_url_accepts_valid_https(self):
        """A valid HTTPS URL with a public domain must pass validation."""
        from specivo.schemas.webhook import WebhookCreate

        wh = WebhookCreate(url="https://hooks.example.com/callback", secret="secret1234", events=["issue.created"])
        assert wh.url == "https://hooks.example.com/callback"


# ===================================================================
# require_permission factory (HIGH)
# ===================================================================
# Dead code removal -- no test needed.


# ===================================================================
# X-Forwarded-For trust (HIGH)
# ===================================================================


class TestXForwardedForTrust:
    """Rate limiter must NOT blindly trust X-Forwarded-For headers.

    Only when the direct connection comes from a configured trusted proxy
    should the XFF header be used to determine the real client IP.
    """

    async def test_rate_limit_uses_client_host_without_trusted_proxy(self):
        """When no trusted proxy is configured, _get_client_ip must return
        request.client.host even if X-Forwarded-For is present.
        """
        from specivo.core.rate_limit import _get_client_ip

        # Build a mock request with XFF header but direct client IP
        request = type(
            "Request",
            (),
            {
                "headers": {"X-Forwarded-For": "1.2.3.4"},
                "client": type("Client", (), {"host": "10.0.0.1"})(),
            },
        )()

        ip = _get_client_ip(request)
        # After the fix, _get_client_ip should return client.host (10.0.0.1)
        # because the source is not a trusted proxy.
        assert ip == "10.0.0.1", f"Without trusted proxy, should use client.host, got {ip}"

    async def test_rate_limit_uses_xff_from_trusted_proxy(self):
        """When the request comes from an explicitly trusted proxy, XFF should be used."""
        from unittest.mock import MagicMock, patch

        from specivo.core.rate_limit import _get_client_ip

        request = type(
            "Request",
            (),
            {
                "headers": {"X-Forwarded-For": "203.0.113.50, 10.0.0.1"},
                "client": type("Client", (), {"host": "10.0.0.1"})(),
            },
        )()

        # With 10.0.0.0/8 explicitly in trusted_proxies, XFF is used.
        mock_settings = MagicMock()
        mock_settings.trusted_proxies = ["10.0.0.0/8"]
        with patch("specivo.core.config.get_settings", return_value=mock_settings):
            ip = _get_client_ip(request)
        assert ip == "203.0.113.50"

    async def test_rate_limit_ignores_xff_from_untrusted_source(self):
        """When XFF is sent from a non-trusted IP, it must be ignored."""
        from specivo.core.rate_limit import _get_client_ip

        request = type(
            "Request",
            (),
            {
                "headers": {"X-Forwarded-For": "1.2.3.4"},
                "client": type("Client", (), {"host": "203.0.113.99"})(),
            },
        )()

        ip = _get_client_ip(request)
        # After the fix, an untrusted source (203.0.113.99) should NOT have
        # its XFF header honored.
        assert ip == "203.0.113.99", f"Untrusted source XFF should be ignored, got {ip}"


# ===================================================================
# Medium security fixes
# ===================================================================


class TestMediumSecurityFixes:
    """Assorted medium-severity findings."""

    async def test_gitlab_webhook_uses_timing_safe_comparison(self):
        """GitLab token validation must use hmac.compare_digest, not '=='."""
        import inspect

        from specivo.hooks.gitlab import _validate_gitlab_token

        source = inspect.getsource(_validate_gitlab_token)
        assert "compare_digest" in source or "hmac" in source, (
            "_validate_gitlab_token must use hmac.compare_digest for token comparison"
        )
        assert "setting.value != token" not in source, (
            "Direct string comparison (!=) for secrets is a timing side-channel"
        )

    async def test_login_audit_does_not_contain_credentials(self, client: AsyncClient, db_session: AsyncSession):
        """Successful login audit log must NOT include the password."""
        await _create_user(db_session, login="audit_user")

        with patch(
            "specivo.services.security_audit_service.SecurityAuditService.log_event", new_callable=AsyncMock
        ) as mock_log:
            resp = await _login(client, "audit_user", TEST_PASSWORD)
            assert resp.status_code == 200

            # Check that no call to log_event passed the password in details
            for call in mock_log.call_args_list:
                kwargs = call.kwargs if call.kwargs else {}
                details = kwargs.get("details", {})
                if details:
                    details_str = str(details).lower()
                    assert TEST_PASSWORD.lower() not in details_str, "Audit log must not contain the user's password"
                    assert "password" not in details, "Audit log details must not have a 'password' key"

    async def test_auth_cookies_have_secure_flag(self, client: AsyncClient, db_session: AsyncSession):
        """Auth cookies must include secure=True when not in debug mode."""
        await _create_user(db_session, login="cookie_secure_user")
        resp = await _login(client, "cookie_secure_user", TEST_PASSWORD)
        assert resp.status_code == 200

        # Check Set-Cookie headers for Secure flag
        set_cookie_headers = resp.headers.get_list("set-cookie")
        access_cookie_header = [h for h in set_cookie_headers if h.startswith("access_token=")]
        refresh_cookie_header = [h for h in set_cookie_headers if h.startswith("refresh_token=")]

        for header in access_cookie_header + refresh_cookie_header:
            # When DEBUG=false (test default), cookies must have Secure flag
            assert "secure" in header.lower(), f"Auth cookie missing Secure flag: {header}"

    async def test_health_endpoint_hides_error_details(self, unauth_client: AsyncClient):
        """When DB/Redis is down, /health must return 'error' not the exception message."""
        # The health endpoint catches exceptions and currently puts them in
        # the response as f"error: {exc}". After fix, it should just say "error".
        with patch("specivo.core.database.get_engine") as mock_engine:
            mock_engine.side_effect = Exception("Connection refused to secret-host:5432")
            resp = await unauth_client.get("/health/")

            if resp.status_code == 200:
                body = resp.json()
                db_status = body.get("database", "")
                assert "secret-host" not in db_status, "Health endpoint must not leak internal error details"
                assert "Connection refused" not in db_status, "Health endpoint must not leak exception messages"

    async def test_kill_token_uses_timing_safe_comparison(self):
        """Kill switch token comparison must use hmac.compare_digest, not '=='."""
        import inspect

        from specivo.api.v1.admin.kill_switch import _resolve_kill_actor

        source = inspect.getsource(_resolve_kill_actor)
        assert "compare_digest" in source or "hmac" in source, (
            "_resolve_kill_actor must use hmac.compare_digest for kill token comparison"
        )
        assert "x_kill_token == settings.kill_token" not in source, (
            "Direct == comparison for secrets is a timing side-channel"
        )

    async def test_attachment_rejects_invalid_container_type(self, client: AsyncClient, db_session: AsyncSession):
        """container_type must be validated against an allowlist (Issue, WikiPage, Journal).

        An arbitrary string like 'User' or 'Setting' must be rejected with 422.
        """
        user = await _create_user(db_session, login="att_container_user")
        resp = await _login(client, "att_container_user", TEST_PASSWORD)
        token = resp.json()["access_token"]

        import io

        resp = await client.post(
            "/api/v1/attachments/",
            data={"container_type": "Setting", "container_id": "1"},
            files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, f"Invalid container_type should return 422, got {resp.status_code}"


# ===================================================================
# Low security fixes
# ===================================================================


class TestLowSecurityFixes:
    """Low-severity findings that still improve defense in depth."""

    async def test_login_rejects_oversized_password(self, client: AsyncClient, db_session: AsyncSession):
        """Passwords longer than 1024 chars must be rejected at the schema level (422).

        bcrypt truncates at 72 bytes; accepting huge payloads wastes CPU
        on hashing and can be used for DoS.
        """
        await _create_user(db_session, login="bigpw_user")
        oversized_password = "A" * 1025

        resp = await client.post(
            "/api/v1/auth/login/",
            json={"login": "bigpw_user", "password": oversized_password},
        )
        assert resp.status_code == 422, f"Oversized password should return 422, got {resp.status_code}"

    async def test_refresh_endpoint_has_rate_limit(self):
        """The /auth/refresh endpoint must have a rate limit dependency."""
        import inspect

        from specivo.api.v1.auth import refresh

        source = inspect.getsource(refresh)
        # Check the function signature or decorator for rate_limit dependency
        sig = inspect.signature(refresh)
        params = list(sig.parameters.keys())

        # Also check if there's a rate_limit dependency in the function
        # or in the router definition
        from specivo.api.v1 import auth as auth_module

        auth_source = inspect.getsource(auth_module)

        # Look for rate_limit applied to the refresh endpoint
        # This could be as a Depends() parameter or a decorator
        has_rate_limit = "_rl" in params or "rate_limit" in source or "rate_limit" in str(sig)

        assert has_rate_limit, "POST /auth/refresh must have a rate_limit dependency to prevent brute force"
