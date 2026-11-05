"""The security audit log is on in every install, keeps its noise down and can be read by admins.

- Every event goes to ``security_audit_logs`` without a plugin: ``log_event``,
  ``record_event`` (SSO, SCIM, sessions) and ``audit_scope`` all persist.
- What is not written: ``access_granted`` (one row per page view), a request
  that carries no credentials, an expired cookie token; a refused credential
  is written once per address and reason per minute.
- A client's ``X-Request-ID`` and ``User-Agent`` are cut to the column widths,
  so an oversized header loses no event and breaks no request.
- ``/api/v1/admin/audit-logs/`` and ``/admin/audit-log/`` are for global admins
  only (not a project-limited key), with filters, pagination and escaped details.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService, audit_scope, record_event
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory
from tests.integration.test_issue_visibility_listings import World, build_world, go_live
from tests.integration.test_mcp_transport import mcp_key  # noqa: F401

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_LONG_REQUEST_ID = "r" * 200
_LONG_USER_AGENT = "Agent/" + "x" * 3000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _rows(db: AsyncSession, event: AuditEvent | None = None) -> list[SecurityAuditLog]:
    stmt = select(SecurityAuditLog).order_by(SecurityAuditLog.id)
    if event is not None:
        stmt = stmt.where(SecurityAuditLog.event_type == event.value)
    return list((await db.execute(stmt)).scalars().all())


async def _count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(SecurityAuditLog)) or 0)


async def _user(db: AsyncSession, factory: Any = UserFactory, **fields: Any) -> User:
    fields.setdefault("email", f"{fields['login']}@example.com")
    user = factory.build(status="active", **fields)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _token(client: AsyncClient, login: str, **headers: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD}, headers=headers or None
    )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return resp.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _limited_admin_key(db: AsyncSession, admin: User, project_key: str) -> str:
    key, raw = await ApiKeyService().create_key(session=db, user_id=admin.id, name="limited-admin")
    # Stored directly: a stored limit holds whatever creates it.
    key.scopes = {"projects": [project_key]}
    await db.commit()
    return raw


def _row(event: AuditEvent, *, at: datetime | None = None, **fields: Any) -> SecurityAuditLog:
    fields.setdefault("details", {})
    row = SecurityAuditLog(event_type=event.value, **fields)
    if at is not None:
        row.created_at = at
    return row


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    clear_role_cache(db_session)
    w = await build_world(db_session, "AUD")
    yield w
    clear_role_cache(db_session)


# ---------------------------------------------------------------------------
# Recording: everything persists without a plugin
# ---------------------------------------------------------------------------


async def test_attachment_upload_is_audited_without_registering_anything(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    token = await _token(client, world.manager.login)
    resp = await client.post(
        "/api/v1/attachments/",
        files={"file": ("notes.txt", b"plain text", "text/plain")},
        data={"container_type": "Issue", "container_id": str(world.pub.id)},
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text

    [row] = await _rows(db_session, AuditEvent.ATTACHMENT_UPLOADED)
    assert row.user_id == world.manager.id
    assert row.resource_id == resp.json()["id"]
    assert row.project_id == world.p.id
    assert row.ip_address is not None


async def test_admin_user_action_is_audited(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await admin_client.post(
        "/api/v1/admin/users/",
        json={"login": "aud_created", "email": "aud_created@example.com", "display_name": "Created"},
    )
    assert resp.status_code == 201, resp.text

    rows = [r for r in await _rows(db_session, AuditEvent.MEMBER_CHANGE) if r.details.get("action") == "user_created"]
    assert len(rows) == 1
    assert rows[0].user_id == admin_client.state.user.id  # type: ignore[attr-defined]
    assert rows[0].details["target_login"] == "aud_created"


async def test_sso_logout_event_is_persisted(db_session: AsyncSession) -> None:
    from specivo.services.sso import events

    user = await _user(db_session, login="aud_sso_logout")
    async with audit_scope(db_session):
        events.logout(
            provider_slug="corp", issuer="https://idp.example.com", user_id=user.id, subject="s-1", idp_redirect=True
        )

    [row] = await _rows(db_session, AuditEvent.SSO_LOGOUT)
    assert row.user_id == user.id
    assert row.details["provider"] == "corp"
    assert "s-1" not in str(row.details)


async def test_audit_scope_writes_outside_a_request(db_session: AsyncSession) -> None:
    async with audit_scope(db_session):
        assert record_event(AuditEvent.SESSIONS_REVOKED, details={"reason": "test", "session_count": 2})

    [row] = await _rows(db_session, AuditEvent.SESSIONS_REVOKED)
    assert row.details == {"reason": "test", "session_count": 2}
    assert row.ip_address is None


async def test_invalid_api_key_writes_auth_failure(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.get("/api/v1/my/api-keys/", headers=_bearer("spv_" + "z" * 40))
    assert resp.status_code == 401

    [row] = await _rows(db_session, AuditEvent.AUTH_FAILURE)
    assert row.details["reason"] == "api_key_invalid"
    assert row.user_id is None
    assert row.ip_address is not None


async def test_mcp_tool_row_has_ip_address_when_context_is_bound(db_session: AsyncSession) -> None:
    from specivo.mcp.tools import _whoami
    from specivo.services.security_audit_service import bind_audit_context, reset_audit_context

    user = await _user(db_session, ServiceAccountFactory, login="aud_mcp_agent")
    token = bind_audit_context([], ip_address="203.0.113.7", user_agent="agent/1.0", state={"request_id": "req-mcp-1"})
    try:
        await _whoami(db_session, user)
    finally:
        reset_audit_context(token)

    [row] = await _rows(db_session, AuditEvent.RESOURCE_ACCESS)
    assert row.details["source"] == "mcp"
    assert row.ip_address == "203.0.113.7"
    assert row.request_id == "req-mcp-1"
    assert row.user_agent == "agent/1.0"


# ---------------------------------------------------------------------------
# Noise rules
# ---------------------------------------------------------------------------


async def test_page_view_writes_no_access_granted(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    cookie = {"access_token": _make_access_token(world.manager, get_settings())}
    wiki = await client.get(f"/projects/{world.p.key}/wiki/{world.wiki_slug}/", cookies=cookie)
    assert wiki.status_code == 200, wiki.text
    time_page = await client.get(f"/projects/{world.p.key}/time-entries/", cookies=cookie)
    assert time_page.status_code == 200

    assert await _rows(db_session, AuditEvent.ACCESS_GRANTED) == []


async def test_access_denied_is_still_written(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    cookie = {"access_token": _make_access_token(world.reporter, get_settings())}
    resp = await client.get(f"/projects/{world.p.key}/wiki/new/", cookies=cookie)
    assert resp.status_code in (403, 404)

    [row] = await _rows(db_session, AuditEvent.ACCESS_DENIED)
    assert row.user_id == world.reporter.id
    assert row.permission == "manage_wiki"
    # Request facts live in their columns only.
    assert "request_id" not in row.details
    assert "user_agent" not in row.details


async def test_request_without_credentials_writes_no_row(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.get("/api/v1/my/api-keys/")
    assert resp.status_code == 401
    assert await _rows(db_session, AuditEvent.AUTH_FAILURE) == []


async def test_expired_cookie_token_writes_no_row(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, login="aud_expired")
    expired = _make_access_token(user, get_settings().model_copy(update={"access_token_expire_minutes": -5}))
    resp = await client.get("/api/v1/my/api-keys/", cookies={"access_token": expired})
    assert resp.status_code == 401
    assert await _rows(db_session, AuditEvent.AUTH_FAILURE) == []


async def test_login_failure_for_unknown_login_stores_no_text(client: AsyncClient, db_session: AsyncSession) -> None:
    known = await _user(db_session, login="aud_known")
    typed = "correct-horse-battery-staple"

    unknown = await client.post("/api/v1/auth/login/", json={"login": typed, "password": "whatever-1"})
    assert unknown.status_code == 401
    wrong = await client.post("/api/v1/auth/login/", json={"login": known.login, "password": "not-the-password"})
    assert wrong.status_code == 401

    rows = await _rows(db_session, AuditEvent.LOGIN_FAILURE)
    assert len(rows) == 2
    assert typed not in str(rows[0].details)
    assert rows[0].details["login_known"] is False
    assert "login_hint" not in rows[0].details
    assert rows[1].details["login_hint"] == known.login


async def test_search_query_text_is_capped(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, login="aud_search_cap")
    token = await _token(client, user.login)
    resp = await client.get("/api/v1/search/", params={"q": "marmot " * 200, "mode": "keyword"}, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    rows = await _rows(db_session, AuditEvent.SEARCH_QUERY)
    assert len(rows[0].details["query"]) == 500


# ---------------------------------------------------------------------------
# Oversized headers
# ---------------------------------------------------------------------------


async def test_oversized_request_id_and_user_agent_keep_the_event(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = {"X-Request-ID": _LONG_REQUEST_ID, "User-Agent": _LONG_USER_AGENT}
    created = await admin_client.post(
        "/api/v1/admin/users/",
        json={"login": "aud_long_headers", "email": "aud_long_headers@example.com", "display_name": "Long"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    refused = await admin_client.get("/api/v1/my/api-keys/", headers={**headers, **_bearer("spv_" + "q" * 40)})
    assert refused.status_code == 401

    members = await _rows(db_session, AuditEvent.MEMBER_CHANGE)
    [member] = [r for r in members if r.details.get("action") == "user_created"]
    [failure] = await _rows(db_session, AuditEvent.AUTH_FAILURE)
    for row in (member, failure):
        assert row.request_id == _LONG_REQUEST_ID[:36]
        assert row.user_agent is not None and len(row.user_agent) == 512


async def test_login_survives_oversized_request_id_and_user_agent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, login="aud_long_login")
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
        headers={"X-Request-ID": _LONG_REQUEST_ID, "User-Agent": _LONG_USER_AGENT},
    )
    assert resp.status_code == 200, resp.text

    [row] = await _rows(db_session, AuditEvent.LOGIN_SUCCESS)
    assert row.user_id == user.id
    assert row.request_id == _LONG_REQUEST_ID[:36]
    assert row.user_agent is not None and len(row.user_agent) == 512


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


async def test_anonymous_read_scope_is_not_broken_by_audit(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    await go_live(db_session, world)
    before = await _count(db_session)
    issue = await client.get(f"/api/v1/issues/{world.pub.display_key}/")
    assert issue.status_code == 200, issue.text
    wiki = await client.get(f"/api/v1/projects/{world.p.key}/wiki/{world.wiki_slug}/")
    assert wiki.status_code == 200, wiki.text
    assert await _count(db_session) == before


async def test_anonymous_wiki_page_writes_no_rows(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    await go_live(db_session, world)
    before = await _count(db_session)
    page = await client.get(f"/projects/{world.p.key}/wiki/{world.wiki_slug}/")
    assert page.status_code == 200, page.text
    assert await _count(db_session) == before


async def test_failed_flush_does_not_fail_the_response(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _broken(_event: dict[str, Any]) -> SecurityAuditLog:
        raise RuntimeError("audit storage unavailable")

    # The batch flush imports this when it runs, so patching the service module reaches it.
    monkeypatch.setattr("specivo.services.security_audit_service.bounded_log", _broken)
    with caplog.at_level("WARNING", logger="specivo.core.middleware"):
        resp = await admin_client.post(
            "/api/v1/admin/users/",
            json={"login": "aud_flush_fails", "email": "aud_flush_fails@example.com", "display_name": "Flush"},
        )
    assert resp.status_code == 201, resp.text
    assert "Batch audit flush failed" in caplog.text


async def test_nul_characters_do_not_break_the_audit_write(db_session: AsyncSession) -> None:
    """JSONB and text columns refuse NUL, and a failed flush would poison the caller's transaction."""
    await SecurityAuditService().log_event(
        db_session,
        AuditEvent.SEARCH_QUERY,
        user_agent="agent\x00name",
        details={"query": "before\x00after", "nested": [{"name": "file\x00.txt"}]},
    )
    row = (await _rows(db_session, AuditEvent.SEARCH_QUERY))[-1]
    assert row.user_agent == "agentname"
    assert row.details == {"query": "beforeafter", "nested": [{"name": "file.txt"}]}


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


async def test_admin_can_list_audit_logs_api(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await admin_client.get("/api/v1/admin/audit-logs/", params={"event_type": "login_success"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] >= 1
    item = body["items"][0]
    assert item["event_type"] == "login_success"
    assert item["user_login"] == "admin_fixture_user"
    assert item["project_key"] is None


async def test_audit_logs_api_refused_for_non_admin(auth_client: AsyncClient) -> None:
    assert (await auth_client.get("/api/v1/admin/audit-logs/")).status_code == 403


async def test_audit_logs_api_refused_for_project_limited_admin_key(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    raw = await _limited_admin_key(db_session, world.admin, world.p.key)
    resp = await client.get("/api/v1/admin/audit-logs/", headers=_bearer(raw))
    assert resp.status_code == 403
    page = await client.get("/admin/audit-log/", headers=_bearer(raw))
    assert page.status_code == 403


async def test_audit_logs_api_rejects_unknown_values(admin_client: AsyncClient) -> None:
    assert (await admin_client.get("/api/v1/admin/audit-logs/", params={"event_type": "nope"})).status_code == 422
    assert (await admin_client.get("/api/v1/admin/audit-logs/", params={"event_types": "nope"})).status_code == 422
    assert (await admin_client.get("/api/v1/admin/audit-logs/", params={"login": "nobody-here"})).status_code == 422


async def test_deleted_user_shows_as_id(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    db_session.add(_row(AuditEvent.SESSIONS_REVOKED, user_id=987654, project_id=876543))
    await db_session.commit()

    resp = await admin_client.get("/api/v1/admin/audit-logs/", params={"event_type": "sessions_revoked"})
    [item] = resp.json()["items"]
    assert item["user_id"] == 987654
    assert item["user_login"] is None
    assert item["project_key"] is None

    page = await admin_client.get("/admin/audit-log/", params={"event_type": "sessions_revoked"})
    assert page.status_code == 200
    assert "#987654" in page.text
    assert "#876543" in page.text


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------


async def test_audit_log_page_renders_for_admin(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await _user(db_session, AdminUserFactory, login="aud_thai_admin", language="th", timezone="Asia/Bangkok")
    at = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=2)
    db_session.add(_row(AuditEvent.SSO_LOGOUT, at=at, user_id=admin.id, details={"provider": "corp"}))
    await db_session.commit()
    token = await _token(client, admin.login)

    page = await client.get("/admin/audit-log/", cookies={"access_token": token, "specivo_lang": "th"})
    assert page.status_code == 200, page.text
    assert 'lang="th"' in page.text
    assert "sso_logout" in page.text
    assert at.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M") in page.text
    assert "aud_thai_admin" in page.text

    dashboard = await client.get("/admin/", cookies={"access_token": token})
    assert 'href="/admin/audit-log/"' in dashboard.text


async def test_audit_log_page_refused_for_non_admin(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, login="aud_not_admin")
    token = await _token(client, user.login)
    assert (await client.get("/admin/audit-log/", cookies={"access_token": token})).status_code == 403
    client.cookies.clear()
    anonymous = await client.get("/admin/audit-log/", follow_redirects=False)
    assert anonymous.status_code in (303, 307)
    # The sidebar no longer offers a dead "Audit Log" link to anybody.
    home = await client.get("/", cookies={"access_token": token})
    assert "Audit Log" not in home.text


async def test_details_are_escaped_in_the_page(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    db_session.add(_row(AuditEvent.SEARCH_QUERY, details={"query": "<script>alert('x')</script> **bold**"}))
    await db_session.commit()

    page = await admin_client.get("/admin/audit-log/", params={"event_type": "search_query"})
    assert page.status_code == 200
    assert "<script>alert(" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "<strong>bold</strong>" not in page.text


async def test_audit_log_filters_by_category_login_project_and_dates(
    admin_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    now = datetime.now(UTC)
    db_session.add_all(
        [
            _row(
                AuditEvent.SSO_LOGIN_SUCCESS,
                at=now - timedelta(hours=1),
                user_id=world.manager.id,
                details={"m": "m-sso"},
            ),
            _row(AuditEvent.SCIM_USER_CREATED, at=now - timedelta(hours=1), details={"m": "m-scim"}),
            _row(
                AuditEvent.ATTACHMENT_UPLOADED,
                at=now - timedelta(hours=1),
                user_id=world.reporter.id,
                project_id=world.p.id,
                ip_address="198.51.100.4",
                details={"m": "m-attach"},
            ),
            _row(AuditEvent.SSO_LOGIN_FAILURE, at=now - timedelta(days=45), details={"m": "m-old"}),
        ]
    )
    await db_session.commit()

    async def page(**params: str) -> str:
        resp = await admin_client.get("/admin/audit-log/", params=params)
        assert resp.status_code == 200, resp.text
        return resp.text

    sso = await page(category="sso")
    assert "m-sso" in sso and "m-scim" not in sso and "m-attach" not in sso
    # Older than the default 30 days: hidden until the range asks for it.
    assert "m-old" not in sso
    older = await page(category="sso", date_from=(now - timedelta(days=60)).date().isoformat())
    assert "m-old" in older
    by_login = await page(login=world.reporter.login)
    assert "m-attach" in by_login and "m-sso" not in by_login
    by_project = await page(project=world.p.key)
    assert "m-attach" in by_project and "m-scim" not in by_project
    by_ip = await page(ip="198.51.100.4")
    assert "m-attach" in by_ip and "m-sso" not in by_ip
    until = await page(category="sso", date_from="2000-01-01", date_to=(now - timedelta(days=40)).date().isoformat())
    assert "m-old" in until and "m-sso" not in until

    api = await admin_client.get(
        "/api/v1/admin/audit-logs/", params=[("event_types", "sso_login_success"), ("event_types", "scim_user_created")]
    )
    assert {i["event_type"] for i in api.json()["items"]} == {"sso_login_success", "scim_user_created"}
    api_login = await admin_client.get("/api/v1/admin/audit-logs/", params={"login": world.reporter.login})
    [item] = api_login.json()["items"]
    assert item["project_key"] == world.p.key
    assert item["user_login"] == world.reporter.login
    api_ip = await admin_client.get("/api/v1/admin/audit-logs/", params={"ip_address": "198.51.100.4"})
    assert [i["event_type"] for i in api_ip.json()["items"]] == ["attachment_uploaded"]

    unknown = await admin_client.get("/admin/audit-log/", params={"category": "nope"})
    assert unknown.status_code == 200


async def test_audit_log_page_paginates_and_keeps_the_filters(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add_all(_row(AuditEvent.SCIM_USER_UPDATED, details={"n": i}) for i in range(12))
    await db_session.commit()

    first = await admin_client.get("/admin/audit-log/", params={"category": "provisioning", "limit": "10"})
    assert first.status_code == 200
    assert "sp-pagination" in first.text
    assert "category=provisioning" in first.text and "offset=10" in first.text


# ---------------------------------------------------------------------------
# Auth failure throttle and the MCP transport (serial: Redis, a real MCP app)
# ---------------------------------------------------------------------------


@pytest.mark.serial
async def test_repeated_bad_token_from_one_ip_writes_one_row(client: AsyncClient, db_session: AsyncSession) -> None:
    for _ in range(3):
        assert (await client.get("/api/v1/my/api-keys/", headers=_bearer("not-a-jwt"))).status_code == 401
    for _ in range(2):
        assert (await client.get("/api/v1/my/api-keys/", headers=_bearer("spv_" + "y" * 40))).status_code == 401

    rows = await _rows(db_session, AuditEvent.AUTH_FAILURE)
    assert sorted(r.details["reason"] for r in rows) == ["api_key_invalid", "invalid_jwt"]


async def test_auth_failure_is_written_when_redis_is_down(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import specivo.core.redis as redis_module

    async def _down() -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(redis_module, "get_redis", _down)
    for _ in range(2):
        assert (await client.get("/api/v1/my/api-keys/", headers=_bearer("not-a-jwt"))).status_code == 401

    assert len(await _rows(db_session, AuditEvent.AUTH_FAILURE)) == 2


@pytest.mark.serial
async def test_mcp_tool_call_over_http_records_where_it_came_from(mcp_key, db_engine) -> None:  # noqa: F811
    from sqlalchemy import delete

    from specivo.main import create_app
    from specivo.mcp.mount import get_mcp_session_manager
    from tests.integration.test_mcp_protocol import _ACCEPT, _sse_json
    from tests.integration.test_mcp_transport import _INIT_PARAMS

    raw_key, user_id, _key_id = mcp_key
    app = create_app()
    manager = get_mcp_session_manager()
    assert manager is not None
    headers = {"Authorization": f"Bearer {raw_key}", "Accept": _ACCEPT, "User-Agent": "audit-agent/2"}
    try:
        async with manager.run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
                init = await http.post(
                    "/mcp/",
                    headers={**headers, "X-Request-ID": "mcp-init-request"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _INIT_PARAMS},
                )
                assert init.status_code == 200, init.text
                headers = {**headers, "Mcp-Session-Id": init.headers["mcp-session-id"]}
                headers["Mcp-Protocol-Version"] = "2025-03-26"
                await http.post(
                    "/mcp/", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                called = await http.post(
                    "/mcp/",
                    headers={**headers, "X-Request-ID": "mcp-call-request"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "specivo_whoami", "arguments": {}},
                    },
                )
                assert _sse_json(called.text)["result"]["isError"] is False

        async with db_engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(SecurityAuditLog.ip_address, SecurityAuditLog.request_id, SecurityAuditLog.user_agent).where(
                        SecurityAuditLog.user_id == user_id, SecurityAuditLog.details["tool"].astext == "whoami"
                    )
                )
            ).all()
        assert rows == [("127.0.0.1", "mcp-call-request", "audit-agent/2")]
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(delete(SecurityAuditLog).where(SecurityAuditLog.user_id == user_id))
