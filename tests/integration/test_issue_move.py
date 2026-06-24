"""Integration tests for cross-project issue move.

Covers: renumbering + stable internal id, old reference still resolving,
history/journal preservation and project_id re-sync, clearing of project-scoped
fields, recomputation of project-derived metadata, and the hierarchy / same-project
guards.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.computed_metadata_service import COMPUTED_METADATA_SETTINGS_KEY
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=open_status.id)
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
async def inbox(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(
        key="INB",
        identifier="inbox",
        settings={COMPUTED_METADATA_SETTINGS_KEY: {"Area": "Inbox"}},
    )
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def home(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(
        key="HOME",
        identifier="home",
        settings={COMPUTED_METADATA_SETTINGS_KEY: {"Area": "Home"}},
    )
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="move_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


async def _create(client, token, project, tracker, status, prio, subject="Issue", description="body"):
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": subject,
            "description": description,
            "status_id": status.id,
            "priority_id": prio.id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _move(client, token, issue_ref, target_key, notes=None):
    return await client.post(
        f"/api/v1/issues/{issue_ref}/move/",
        json={"target_project_key": target_key, "notes": notes},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_move_renumbers_keeps_id_and_old_ref_resolves(
    client, db_session, admin_token, inbox, home, tracker, open_status, priority
):
    created = await _create(client, admin_token, inbox, tracker, open_status, priority, subject="Triage me")
    issue_id = created["id"]
    assert created["key"] == "INB-1"

    resp = await _move(client, admin_token, "INB-1", "HOME")
    assert resp.status_code == 200, resp.text
    moved = resp.json()
    assert moved["id"] == issue_id  # internal id unchanged
    assert moved["key"] == "HOME-1"  # new per-project number
    assert moved["project_key"] == "HOME"

    # Old reference still resolves to the same issue.
    old = await client.get("/api/v1/issues/INB-1/", headers={"Authorization": f"Bearer {admin_token}"})
    assert old.status_code == 200, old.text
    assert old.json()["id"] == issue_id
    assert old.json()["key"] == "HOME-1"

    # New reference resolves too.
    new = await client.get("/api/v1/issues/HOME-1/", headers={"Authorization": f"Bearer {admin_token}"})
    assert new.status_code == 200
    assert new.json()["id"] == issue_id


@pytest.mark.asyncio
async def test_move_preserves_and_resyncs_journals(
    client, db_session, admin_token, inbox, home, tracker, open_status, priority
):
    created = await _create(client, admin_token, inbox, tracker, open_status, priority)
    issue_id = created["id"]

    pre = await db_session.scalar(select(func.count()).select_from(Journal).where(Journal.issue_id == issue_id))
    assert pre >= 1  # initial description journal

    resp = await _move(client, admin_token, "INB-1", "HOME", notes="moving to home")
    assert resp.status_code == 200, resp.text

    # All journals (old + the new move entry) now carry the target project_id.
    rows = (await db_session.execute(select(Journal).where(Journal.issue_id == issue_id))).scalars().all()
    db_issue = await db_session.get(Issue, issue_id)
    assert len(rows) >= pre + 1  # history preserved + move recorded
    assert all(j.project_id == home.id for j in rows)
    assert db_issue.project_id == home.id


@pytest.mark.asyncio
async def test_move_recomputes_project_derived_metadata(
    client, db_session, admin_token, inbox, home, tracker, open_status, priority
):
    created = await _create(client, admin_token, inbox, tracker, open_status, priority)
    assert created["metadata"].get("Area") == "Inbox"

    resp = await _move(client, admin_token, "INB-1", "HOME")
    assert resp.status_code == 200, resp.text
    assert resp.json()["metadata"].get("Area") == "Home"  # recomputed for the new project


@pytest.mark.asyncio
async def test_move_clears_project_scoped_fields(
    db_session, inbox, home, tracker, open_status, priority, admin_user
):
    """Service-level: fixed_version/sprint/category/tags are cleared on move."""
    from specivo.models.tag import Tag, TagLink
    from specivo.models.version import Version
    from specivo.schemas.issue import IssueCreate
    from specivo.services.issue_service import IssueService

    svc = IssueService()
    issue = await svc.create(
        db_session,
        inbox,
        IssueCreate(project_key="INB", tracker_id=tracker.id, subject="With extras"),
        admin_user,
    )
    # Attach project-scoped data in the source project.
    version = Version(project_id=inbox.id, name="v1")
    db_session.add(version)
    tag = Tag(project_id=inbox.id, name="urgent")
    db_session.add(tag)
    await db_session.flush()
    issue.fixed_version_id = version.id
    db_session.add(TagLink(tag_id=tag.id, issue_id=issue.id))
    await db_session.flush()

    await svc.move(db_session, issue, home, admin_user)
    await db_session.flush()

    refreshed = await db_session.get(Issue, issue.id)
    assert refreshed.fixed_version_id is None
    assert refreshed.sprint_id is None
    assert refreshed.category_id is None
    tag_links = await db_session.scalar(
        select(func.count()).select_from(TagLink).where(TagLink.issue_id == issue.id)
    )
    assert tag_links == 0


@pytest.mark.asyncio
async def test_move_to_same_project_rejected(
    client, db_session, admin_token, inbox, tracker, open_status, priority
):
    await _create(client, admin_token, inbox, tracker, open_status, priority)
    resp = await _move(client, admin_token, "INB-1", "INB")
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_move_rejects_issue_with_children(
    client, db_session, admin_token, inbox, home, tracker, open_status, priority
):
    parent = await _create(client, admin_token, inbox, tracker, open_status, priority, subject="Parent")
    # Create a child under the parent.
    child_resp = await client.post(
        f"/api/v1/projects/{inbox.key}/issues/",
        json={
            "project_key": inbox.key,
            "tracker_id": tracker.id,
            "subject": "Child",
            "status_id": open_status.id,
            "priority_id": priority.id,
            "parent_id": parent["id"],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert child_resp.status_code == 201, child_resp.text

    resp = await _move(client, admin_token, parent["key"], "HOME")
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_mcp_move_issue_tool(
    db_session, inbox, home, tracker, open_status, priority, admin_user
):
    """The MCP specivo_move_issue tool moves and reports both refs."""
    from specivo.mcp.tools import _move_issue
    from specivo.schemas.issue import IssueCreate
    from specivo.services.issue_service import IssueService

    issue = await IssueService().create(
        db_session,
        inbox,
        IssueCreate(project_key="INB", tracker_id=tracker.id, subject="Via MCP"),
        admin_user,
    )
    await db_session.flush()
    old_ref = issue.display_key

    msg = await _move_issue(db_session, admin_user, old_ref, "home")  # case-insensitive key
    assert "HOME-1" in msg
    assert old_ref in msg

    refreshed = await db_session.get(Issue, issue.id)
    assert refreshed.project_id == home.id
