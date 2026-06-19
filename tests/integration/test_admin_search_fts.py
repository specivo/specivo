"""Integration tests for the FTS language admin/project API and reindex service.

Covers:
- `reindex_fts` rebuilds stored vectors with the (changed) per-project language.
- Instance default language API (admin-gated, validated, reindex-needed flag).
- Per-project override API (manage_project-gated, inherit semantics).
- Reindex dispatch returns a task id (Celery task .delay stubbed).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.search_reindex_service import reindex_fts
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _add_manager(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(name=f"Mgr-{project.key}-{user.id}", permissions=["*"], builtin=0)
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="FTS", name="FTS Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="fts_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def member(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="fts_member", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def status_open(db_session: AsyncSession):
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status_open):
    t = TrackerFactory.build(name="Bug", default_status_id=status_open.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession):
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# reindex_fts service — the core correctness guarantee
# ---------------------------------------------------------------------------


class TestReindexService:
    async def test_reindex_applies_new_language(self, db_session, project, tracker, priority, status_open, admin):
        # Issue created while the project inherits the english default.
        svc = IssueService()
        issue = await svc.create(
            db_session,
            project,
            IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Защита диссертации"),
            admin,
        )
        await db_session.commit()

        async def _matches_russian() -> bool:
            row = await db_session.execute(
                text(
                    "SELECT search_vector @@ plainto_tsquery('russian', 'диссертация') "
                    "FROM issues WHERE id = :id"
                ),
                {"id": issue.id},
            )
            return bool(row.scalar_one())

        # English-indexed: a russian-stemmed query does NOT match.
        assert await _matches_russian() is False

        # Switch the project to russian and reindex its rows.
        project.fts_language = "russian"
        db_session.add(project)
        await db_session.commit()
        counts = await reindex_fts(db_session, project_id=project.id)
        await db_session.commit()

        assert counts["issues"] >= 1
        # Now russian-indexed: the stemmed query matches.
        assert await _matches_russian() is True


# ---------------------------------------------------------------------------
# Instance default API
# ---------------------------------------------------------------------------


class TestInstanceFtsApi:
    async def test_set_and_get_default(self, client, db_session, admin):
        token = await _login(client, admin.login)
        resp = await client.put(
            "/api/v1/admin/search/fts/language/", headers=_auth(token), json={"language": "russian"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["language"] == "russian"
        assert resp.json()["reindex_needed"] is True

        resp = await client.get("/api/v1/admin/search/fts/", headers=_auth(token))
        assert resp.json()["language"] == "russian"

    async def test_non_admin_forbidden(self, client, db_session, member):
        token = await _login(client, member.login)
        resp = await client.put(
            "/api/v1/admin/search/fts/language/", headers=_auth(token), json={"language": "russian"}
        )
        assert resp.status_code == 403

    async def test_invalid_language_rejected(self, client, db_session, admin):
        token = await _login(client, admin.login)
        resp = await client.put(
            "/api/v1/admin/search/fts/language/", headers=_auth(token), json={"language": "klingon"}
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Per-project override API
# ---------------------------------------------------------------------------


class TestProjectFtsApi:
    async def test_manager_sets_override_and_inherits(self, client, db_session, project, member):
        await _add_manager(db_session, project, member)
        token = await _login(client, member.login)

        resp = await client.put(
            f"/api/v1/projects/{project.key}/search/fts/language/",
            headers=_auth(token),
            json={"language": "russian"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["language"] == "russian"
        assert resp.json()["effective"] == "russian"

        # Clear the override -> inherit instance default.
        resp = await client.put(
            f"/api/v1/projects/{project.key}/search/fts/language/",
            headers=_auth(token),
            json={"language": "inherit"},
        )
        assert resp.status_code == 200
        assert resp.json()["language"] is None
        assert resp.json()["effective"] == resp.json()["instance_default"]

    async def test_non_manager_forbidden(self, client, db_session, project, member):
        token = await _login(client, member.login)  # member, not a manager
        resp = await client.put(
            f"/api/v1/projects/{project.key}/search/fts/language/",
            headers=_auth(token),
            json={"language": "russian"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Reindex dispatch (Celery .delay stubbed)
# ---------------------------------------------------------------------------


class TestReindexDispatch:
    async def test_instance_reindex_returns_task_id(self, client, db_session, admin, monkeypatch):
        class _FakeAsync:
            id = "fake-task-123"

        class _FakeTask:
            def delay(self, *args, **kwargs):
                return _FakeAsync()

        monkeypatch.setattr("specivo.api.v1.search_admin.reindex_fts_task", _FakeTask())
        token = await _login(client, admin.login)
        resp = await client.post("/api/v1/admin/search/reindex/", headers=_auth(token))
        assert resp.status_code == 202, resp.text
        assert resp.json()["task_id"] == "fake-task-123"
