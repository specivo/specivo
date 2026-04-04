"""Integration tests for agent sessions.

Tests cover:
- Agent session auto-created on API key use
- User-Agent parsed for model name
- List sessions via project endpoint
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.services.api_key_service import ApiKeyService
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
    proj = ProjectFactory.build(**kw)
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


class TestSessionModelNameParsed:
    async def test_session_model_name_parsed(self, client: AsyncClient, db_session: AsyncSession):
        """User-Agent 'claude-code/1.0 (Claude opus-4)' should parse model_name."""
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
        assert session_obj.model_name is not None
        assert "opus" in session_obj.model_name.lower()


class TestListSessions:
    async def test_list_sessions(self, client: AsyncClient, db_session: AsyncSession):
        """GET /projects/{key}/agent-sessions should return sessions."""
        user = await _make_service_account(db_session, login="agent_list_user", status="active")
        await _make_admin(db_session, login="admin_list_user", status="active")
        project = await _make_project(db_session, key="LIST", identifier="list-proj")

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
        data = resp.json()
        assert isinstance(data, list)
        # At least one session from the agent request
        assert len(data) >= 1
        # Verify structure
        item = data[0]
        assert "id" in item
        assert "api_key_id" in item
        assert "started_at" in item
        assert "last_activity_at" in item
