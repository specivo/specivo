"""Integration tests for resolve/unresolve permission checks."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import Journal
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
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
    assigned_to_id: int | None = None,
) -> dict:
    payload = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if assigned_to_id is not None:
        payload["assigned_to_id"] = assigned_to_id
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
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
    proj = ProjectFactory.build(key="RES", identifier="resolve-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="resolve_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="resolve_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def regular_token(regular_user: User, client: AsyncClient) -> str:
    return await _login(client, regular_user.login)


@pytest_asyncio.fixture
async def issue_key(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> str:
    """Create an issue authored by admin, no assignee."""
    data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Resolve test issue"
    )
    return data["key"]


@pytest_asyncio.fixture
async def issue_key_assigned(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
    regular_user: User,
) -> str:
    """Create an issue authored by admin, assigned to regular_user."""
    data = await _create_issue_via_api(
        client,
        admin_token,
        project.key,
        tracker.id,
        open_status.id,
        priority.id,
        "Assigned resolve test issue",
        assigned_to_id=regular_user.id,
    )
    return data["key"]


@pytest_asyncio.fixture
async def journal_id(client: AsyncClient, admin_token: str, issue_key: str) -> int:
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Comment to resolve"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest_asyncio.fixture
async def journal_id_assigned(client: AsyncClient, admin_token: str, issue_key_assigned: str) -> int:
    resp = await client.post(
        f"/api/v1/issues/{issue_key_assigned}/journals/",
        json={"notes": "Comment on assigned issue"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Tests — resolve/unresolve permissions
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_only_author_assignee_admin(
    client: AsyncClient,
    regular_token: str,
    issue_key: str,
    journal_id: int,
) -> None:
    """Regular user who is NOT author/assignee gets 403 on resolve."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/resolve/",
        json={"summary": "Resolved"},
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_unresolve_only_author_assignee_admin(
    client: AsyncClient,
    admin_token: str,
    regular_token: str,
    issue_key: str,
    journal_id: int,
) -> None:
    """Regular user who is NOT author/assignee gets 403 on unresolve."""
    # First resolve as admin (who is author)
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/resolve/",
        json={"summary": "Resolved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text

    # Regular user tries to unresolve
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/unresolve/",
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_author_can_resolve(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
    journal_id: int,
) -> None:
    """Issue author (admin created the issue) can resolve."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/resolve/",
        json={"summary": "Author resolved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved_summary"] == "Author resolved"


@pytest.mark.integration
async def test_assignee_can_resolve(
    client: AsyncClient,
    regular_token: str,
    issue_key_assigned: str,
    journal_id_assigned: int,
) -> None:
    """Issue assignee can resolve."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key_assigned}/journals/{journal_id_assigned}/resolve/",
        json={"summary": "Assignee resolved"},
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved_summary"] == "Assignee resolved"


# ---------------------------------------------------------------------------
# Tests — private note reply guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_private_note_reply_blocked(
    client: AsyncClient,
    regular_token: str,
    regular_user: User,
    issue_key: str,
    db_session: AsyncSession,
    admin_user: User,
    project: Project,
) -> None:
    """Non-admin cannot reply to a private journal."""
    from sqlalchemy import select

    from specivo.models.issue import Issue

    # Give regular user permission to comment on this project
    role = Role(name="Commenter", permissions=["add_issue_notes", "view_issues"], builtin=0)
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=regular_user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db_session.add(mr)
    await db_session.commit()

    # Parse display key (e.g. "RES-1") into project_key + sequence_number
    proj_key, seq_str = issue_key.rsplit("-", 1)
    result = await db_session.execute(
        select(Issue).where(Issue.project_key == proj_key, Issue.sequence_number == int(seq_str))
    )
    issue = result.scalar_one()

    # Create a private journal directly in the DB
    private_journal = Journal(
        issue_id=issue.id,
        user_id=admin_user.id,
        notes="private note",
        is_private=True,
        sequence=99,
        project_id=project.id,
    )
    db_session.add(private_journal)
    await db_session.commit()
    await db_session.refresh(private_journal)

    # Non-admin tries to reply to the private journal
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "reply to private", "reply_to_id": private_journal.id},
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 422, resp.text
