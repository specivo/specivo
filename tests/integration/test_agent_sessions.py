"""Integration tests for agent sessions.

Tests cover:
- Agent session auto-created on API key use
- User-Agent parsed for the client name and version
- List sessions via project endpoint: pagination and filters
- Session detail and its tool calls: manage_project, project scoping
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member
from specivo.models.project import Project
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, **kw):
    user = UserFactory.build(**kw)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_admin(db: AsyncSession, **kw):
    user = AdminUserFactory.build(**kw)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_service_account(db: AsyncSession, **kw):
    user = ServiceAccountFactory.build(**kw)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(db: AsyncSession, **kw) -> Project:
    proj = ProjectFactory.build(is_public=True, **kw)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: Agent session creation
# ---------------------------------------------------------------------------


class TestAgentSessionCreatedOnApiKeyUse:
    async def test_agent_session_created_on_api_key_use(self, client: AsyncClient, db_session: AsyncSession):
        """Making a request with an API key should create/find an agent session."""
        user = await _make_service_account(db_session, login="agent_sess_user", status="active")
        project = await _make_project(db_session, key="SESS", identifier="sess-proj")

        # Create API key
        service = ApiKeyService()
        _key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="sess-key")
        await db_session.commit()

        # Make a request using the API key (any authenticated endpoint)
        resp = await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "User-Agent": "claude-code/1.0 (Claude opus-4)",
            },
        )
        assert resp.status_code == 200

        # The session list should contain at least one entry from our request
        # (the endpoint itself triggers session tracking)
        from sqlalchemy import select

        from specivo.models.agent_session import AgentSession

        result = await db_session.execute(select(AgentSession).where(AgentSession.api_key_id == _key.id))
        sessions = list(result.scalars().all())
        assert len(sessions) >= 1


class TestSessionClientParsed:
    async def test_session_client_parsed(self, client: AsyncClient, db_session: AsyncSession):
        """User-Agent 'claude-code/1.0 (Claude opus-4)' names the client, not a model."""
        user = await _make_service_account(db_session, login="agent_model_user", status="active")
        project = await _make_project(db_session, key="MODL", identifier="model-proj")

        service = ApiKeyService()
        _key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="model-key")
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "User-Agent": "claude-code/1.0 (Claude opus-4)",
            },
        )
        assert resp.status_code == 200

        from sqlalchemy import select

        from specivo.models.agent_session import AgentSession

        result = await db_session.execute(select(AgentSession).where(AgentSession.api_key_id == _key.id))
        session_obj = result.scalar_one_or_none()
        assert session_obj is not None
        assert session_obj.client_name == "claude-code"
        assert session_obj.client_version == "1.0"


class TestListSessions:
    async def test_list_sessions(self, client: AsyncClient, db_session: AsyncSession):
        """GET /projects/{key}/agent-sessions should return sessions."""
        user = await _make_service_account(db_session, login="agent_list_user", status="active")
        await _make_admin(db_session, login="admin_list_user", status="active")
        project = await _make_project(db_session, key="LIST", identifier="list-proj")
        # A project lists the sessions of its members' accounts.
        db_session.add(Member(user_id=user.id, project_id=project.id))
        await db_session.commit()

        service = ApiKeyService()
        _key, raw_key = await service.create_key(session=db_session, user_id=user.id, name="list-key")
        await db_session.commit()

        # First request — creates session
        await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "User-Agent": "claude-code/1.0",
            },
        )

        # Now list via admin JWT
        admin_token = await _login(client, "admin_list_user")
        resp = await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert isinstance(data, list)
        # At least one session from the agent request
        assert len(data) >= 1
        # Verify structure
        item = data[0]
        assert "id" in item
        assert "api_key_id" in item
        assert "started_at" in item
        assert "last_activity_at" in item
        assert item["client_name"] == "claude-code"
        assert item["client_version"] == "1.0"


class TestExternalSessionId:
    async def test_list_filters_by_external_session_id_and_shows_the_new_fields(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        from specivo.models.agent_session import AgentSession

        user = await _make_service_account(db_session, login="agent_ext_user", status="active")
        await _make_admin(db_session, login="admin_ext_user", status="active")
        project = await _make_project(db_session, key="EXTS", identifier="exts-proj")
        db_session.add(Member(user_id=user.id, project_id=project.id))
        key, _raw = await ApiKeyService().create_key(session=db_session, user_id=user.id, name="ext-key")
        root = AgentSession(api_key_id=key.id, user_id=user.id, client_type="mcp", external_session_id="sess_root")
        db_session.add(root)
        await db_session.flush()
        child = AgentSession(
            api_key_id=key.id,
            user_id=user.id,
            client_type="mcp",
            external_session_id="sess_child",
            parent_external_session_id="sess_root",
            parent_session_id=root.id,
        )
        db_session.add(child)
        await db_session.commit()

        token = await _login(client, "admin_ext_user")
        resp = await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/",
            params={"external_session_id": "sess_child"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        [item] = resp.json()["items"]
        assert item["id"] == child.id
        assert item["external_session_id"] == "sess_child"
        assert item["parent_external_session_id"] == "sess_root"
        assert item["parent_session_id"] == root.id
        assert item["client_type"] == "mcp"
        assert item["ended_at"] is None

        resp = await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/",
            params={"external_session_id": "sess_unknown"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.json()["items"] == []
        assert resp.json()["total_count"] == 0

    async def test_rest_requests_open_a_rest_session(self, client: AsyncClient, db_session: AsyncSession):
        from sqlalchemy import select

        from specivo.models.agent_session import AgentSession

        user = await _make_service_account(db_session, login="agent_rest_user", status="active")
        project = await _make_project(db_session, key="RSTS", identifier="rsts-proj")
        key, raw_key = await ApiKeyService().create_key(session=db_session, user_id=user.id, name="rest-key")
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.key}/agent-sessions/", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert resp.status_code == 200

        row = (await db_session.execute(select(AgentSession).where(AgentSession.api_key_id == key.id))).scalar_one()
        assert row.client_type == "rest"


# ---------------------------------------------------------------------------
# Tests: paginated list, filters, detail and tool-call events
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    from tests.integration.agent_session_world import build_session_world

    clear_role_cache(db_session)
    yield await build_session_world(db_session, "ASR")
    clear_role_cache(db_session)


async def _bearer(client: AsyncClient, login: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _login(client, login)}"}


class TestListPaginationAndFilters:
    async def test_list_is_paginated_newest_activity_first(self, client: AsyncClient, world):
        w = world
        headers = await _bearer(client, w.manager.login)
        url = f"/api/v1/projects/{w.p.key}/agent-sessions/"

        resp = await client.get(url, params={"limit": 2}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_count"] == 3
        assert body["limit"] == 2
        assert [i["id"] for i in body["items"]] == [w.live.id, w.hidden.id]
        assert body["items"][0]["tool_call_count"] == 3

        resp = await client.get(url, params={"limit": 2, "offset": 2}, headers=headers)
        assert [i["id"] for i in resp.json()["items"]] == [w.old.id]

    async def test_filters_account_issue_and_active(self, client: AsyncClient, world):
        w = world
        headers = await _bearer(client, w.manager.login)
        url = f"/api/v1/projects/{w.p.key}/agent-sessions/"

        async def ids(**params) -> list[int]:
            resp = await client.get(url, params=params, headers=headers)
            assert resp.status_code == 200, resp.text
            return [i["id"] for i in resp.json()["items"]]

        assert await ids(account=w.agent.login.upper()) == [w.live.id, w.hidden.id, w.old.id]
        assert await ids(account="nobody-here") == []
        assert await ids(issue=w.pub.display_key) == [w.live.id]
        assert await ids(issue="ZZNOPE-1") == []
        assert await ids(active="true") == [w.live.id]
        assert await ids(active="false") == [w.hidden.id, w.old.id]

    async def test_member_does_not_see_the_session_on_a_hidden_issue_or_filter_by_it(self, client: AsyncClient, world):
        w = world
        headers = await _bearer(client, w.member.login)
        url = f"/api/v1/projects/{w.p.key}/agent-sessions/"

        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, resp.text
        assert {i["id"] for i in resp.json()["items"]} == {w.live.id, w.old.id}

        resp = await client.get(url, params={"issue": w.priv.display_key}, headers=headers)
        assert resp.json()["items"] == []

    async def test_outsider_gets_404(self, client: AsyncClient, world):
        w = world
        resp = await client.get(
            f"/api/v1/projects/{w.p.key}/agent-sessions/", headers=await _bearer(client, w.outsider.login)
        )
        assert resp.status_code == 404


class TestDetailAndEvents:
    async def test_manager_reads_detail_with_counts(self, client: AsyncClient, db_session: AsyncSession, world):
        from sqlalchemy import select

        from specivo.models.security_audit import SecurityAuditLog

        w = world
        resp = await client.get(
            f"/api/v1/projects/{w.p.key}/agent-sessions/{w.live.id}/", headers=await _bearer(client, w.manager.login)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == w.live.id
        assert body["tool_call_count"] == 3
        # The excluded entry still belongs to the session.
        assert body["usage_entry_count"] == 2
        assert body["parent_session_id"] == w.old.id

        viewed = (
            (
                await db_session.execute(
                    select(SecurityAuditLog).where(
                        SecurityAuditLog.resource_type == "agent_session", SecurityAuditLog.resource_id == w.live.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [row.event_type for row in viewed] == ["resource_access"]
        assert viewed[0].user_id == w.manager.id

    async def test_events_keep_project_and_projectless_rows_only(self, client: AsyncClient, world):
        w = world
        resp = await client.get(
            f"/api/v1/projects/{w.p.key}/agent-sessions/{w.live.id}/events/",
            headers=await _bearer(client, w.manager.login),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_count"] == 2
        tools = [i["details"]["tool"] for i in body["items"]]
        assert tools == ["create_issue_asr", "whoami"]
        assert body["items"][0]["project_key"] == w.p.key
        assert body["items"][0]["agent_session_id"] == w.live.id
        assert body["items"][1]["project_id"] is None

    async def test_member_without_manage_project_gets_403(self, client: AsyncClient, world):
        w = world
        headers = await _bearer(client, w.member.login)
        for suffix in ("", "events/"):
            resp = await client.get(f"/api/v1/projects/{w.p.key}/agent-sessions/{w.live.id}/{suffix}", headers=headers)
            assert resp.status_code == 403, resp.text

    async def test_session_of_another_project_or_unknown_is_404(self, client: AsyncClient, db_session, world):
        from specivo.models.agent_session import AgentSession

        w = world
        stranger = await _make_service_account(db_session, login="asr_stranger", status="active")
        key, _raw = await ApiKeyService().create_key(session=db_session, user_id=stranger.id, name="asr-stranger")
        foreign = AgentSession(api_key_id=key.id, user_id=stranger.id, client_type="rest")
        db_session.add(foreign)
        await db_session.commit()

        headers = await _bearer(client, w.manager.login)
        for session_id in (foreign.id, 987654321):
            for suffix in ("", "events/"):
                resp = await client.get(
                    f"/api/v1/projects/{w.p.key}/agent-sessions/{session_id}/{suffix}", headers=headers
                )
                assert resp.status_code == 404, resp.text

    async def test_admin_reads_the_hidden_issue_session(self, client: AsyncClient, world):
        w = world
        resp = await client.get(
            f"/api/v1/projects/{w.p.key}/agent-sessions/{w.hidden.id}/", headers=await _bearer(client, w.admin.login)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["usage_entry_count"] == 1


class TestNoCrossProjectLeaks:
    async def test_projectless_rows_of_project_tools_are_left_out(self, client: AsyncClient, world):
        w = world
        resp = await client.get(
            f"/api/v1/projects/{w.p.key}/agent-sessions/{w.live.id}/events/",
            headers=await _bearer(client, w.manager.login),
        )
        assert resp.status_code == 200, resp.text
        assert "show_issue" not in {i["details"]["tool"] for i in resp.json()["items"]}
        assert w.q_issue.display_key not in resp.text

    async def test_parent_outside_the_project_is_hidden(self, client: AsyncClient, db_session: AsyncSession, world):
        from tests.integration.agent_session_world import add_hidden_parent

        w = world
        parent = await add_hidden_parent(db_session, w)
        headers = await _bearer(client, w.manager.login)

        resp = await client.get(f"/api/v1/projects/{w.p.key}/agent-sessions/{w.live.id}/", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["parent_session_id"] is None
        assert resp.json()["parent_external_session_id"] is None

        resp = await client.get(f"/api/v1/projects/{w.p.key}/agent-sessions/", headers=headers)
        [item] = [i for i in resp.json()["items"] if i["id"] == w.live.id]
        assert item["parent_session_id"] is None
        assert "far-parent" not in resp.text
        assert parent.id not in {i["id"] for i in resp.json()["items"]}
