"""Integration tests for watchers.

requirements:
- Watch/unwatch an issue
- Auto-watch on issue create (author)
- List watchers
- ?include=watchers works
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.watcher import Watcher
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_issue_via_api(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json={
            "project_key": project_key,
            "tracker_id": tracker_id,
            "subject": subject,
            "status_id": status_id,
            "priority_id": priority_id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=open_status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="WCH", identifier="watcher-test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="watcher_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="watcher_user2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def second_token(second_user: User, client: AsyncClient) -> str:
    return await _login(client, second_user.login)


# ---------------------------------------------------------------------------
# Tests: auto-watch on create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_author_auto_watched_on_create(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """The issue author is automatically added as a watcher on creation."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Auto-watch test"
    )

    result = await db_session.execute(
        select(Watcher).where(
            Watcher.issue_id == issue_data["id"],
            Watcher.user_id == admin_user.id,
        )
    )
    watcher = result.scalar_one_or_none()
    assert watcher is not None, "Author should be auto-watched on issue creation"


# ---------------------------------------------------------------------------
# Tests: watch / unwatch endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """POST /issues/{ref}/watchers subscribes the current user."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Watch test"
    )
    issue_key = issue_data["key"]

    resp = await client.post(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert resp.status_code == 201, resp.text

    result = await db_session.execute(
        select(Watcher).where(
            Watcher.issue_id == issue_data["id"],
            Watcher.user_id == second_user.id,
        )
    )
    assert result.scalar_one_or_none() is not None, "Second user should be watching"


@pytest.mark.asyncio
async def test_watch_issue_is_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Watching an already-watched issue is idempotent — no duplicate rows."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Idempotent watch"
    )
    issue_key = issue_data["key"]

    # Watch twice
    await client.post(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text

    result = await db_session.execute(
        select(Watcher).where(
            Watcher.issue_id == issue_data["id"],
            Watcher.user_id == admin_user.id,
        )
    )
    watchers = list(result.scalars().all())
    assert len(watchers) == 1, "Only one watcher row should exist"


@pytest.mark.asyncio
async def test_unwatch_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """DELETE /issues/{ref}/watchers unsubscribes the current user."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Unwatch test"
    )
    issue_key = issue_data["key"]

    # Admin is already watching (auto-watch on create)
    resp = await client.delete(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204, resp.text

    result = await db_session.execute(
        select(Watcher).where(
            Watcher.issue_id == issue_data["id"],
            Watcher.user_id == admin_user.id,
        )
    )
    assert result.scalar_one_or_none() is None, "Watcher should be removed"


@pytest.mark.asyncio
async def test_unwatch_not_watching_is_noop(
    client: AsyncClient,
    db_session: AsyncSession,
    second_user: User,
    second_token: str,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Unwatching an issue you are not watching returns 204 (no-op)."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "No-op unwatch"
    )
    issue_key = issue_data["key"]

    # second_user never watched this issue
    resp = await client.delete(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# Tests: list watchers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_watchers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    second_user: User,
    second_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /issues/{ref}/watchers returns list of watching users."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "List watchers"
    )
    issue_key = issue_data["key"]

    # second_user watches the issue
    await client.post(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {second_token}"},
    )

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/watchers/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    watchers = resp.json()
    assert isinstance(watchers, list)
    watcher_ids = {w["id"] for w in watchers}
    assert admin_user.id in watcher_ids  # auto-watch from creation
    assert second_user.id in watcher_ids


# ---------------------------------------------------------------------------
# Tests: ?include=watchers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_watchers_on_issue_get(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """GET /issues/{ref}?include=watchers returns watchers list inline."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Include watchers"
    )
    issue_key = issue_data["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=watchers",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "watchers" in data
    assert data["watchers"] is not None
    watcher_ids = {w["id"] for w in data["watchers"]}
    assert admin_user.id in watcher_ids


@pytest.mark.asyncio
async def test_include_watchers_not_requested_returns_none(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Without ?include=watchers, the watchers field is null."""
    issue_data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "No watcher include"
    )
    issue_key = issue_data["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("watchers") is None
