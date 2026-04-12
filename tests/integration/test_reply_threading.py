"""Integration tests for journal reply threading and permission checks.

Covers:
- Replying to a root comment stores reply_to_id = parent.id
- Replying to a reply flattens to the root parent (no nested threading)
- Replying to a nonexistent journal returns 404
- Replying to a journal on a different issue returns 422
- Reply sequence increments correctly
- Unauthenticated reply returns 401
- Non-admin cannot reply to a private journal (422)
- Admin can reply to a private journal (201)
- User without project membership gets 404 (anti-enumeration)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
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
) -> dict:
    payload = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_comment(client: AsyncClient, token: str, issue_key: str, notes: str) -> dict:
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": notes},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _resolve_issue_db(db_session: AsyncSession, issue_key: str) -> Issue:
    """Parse display key (e.g. 'RPL-1') and return the Issue row."""
    proj_key, seq_str = issue_key.rsplit("-", 1)
    result = await db_session.execute(
        select(Issue).where(Issue.project_key == proj_key, Issue.sequence_number == int(seq_str))
    )
    return result.scalar_one()


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
    proj = ProjectFactory.build(key="RPL", identifier="reply-test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RPLP", identifier="reply-test-private", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="reply_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="reply_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def outsider_user(db_session: AsyncSession) -> User:
    """A user who is not a member of any project used in these tests."""
    user = UserFactory.build(login="reply_outsider", status="active")
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
async def outsider_token(outsider_user: User, client: AsyncClient) -> str:
    return await _login(client, outsider_user.login)


@pytest_asyncio.fixture
async def issue_key(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> str:
    data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Reply threading test issue"
    )
    return data["key"]


@pytest_asyncio.fixture
async def regular_user_with_comment_permission(
    db_session: AsyncSession,
    regular_user: User,
    project: Project,
) -> User:
    """Grant regular_user add_issue_notes + view_issues on the public project."""
    role = Role(name="Commenter", permissions=["add_issue_notes", "view_issues"], builtin=0)
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=regular_user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db_session.add(mr)
    await db_session.commit()
    return regular_user


# ---------------------------------------------------------------------------
# Tests — threading behavior
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reply_to_root_comment(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
) -> None:
    """Replying to a root comment stores reply_to_id = parent.id. Returns 201."""
    root = await _add_comment(client, admin_token, issue_key, "Root comment")

    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Reply to root", "reply_to_id": root["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["reply_to_id"] == root["id"]


@pytest.mark.integration
async def test_reply_to_reply_flattens_to_root(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
) -> None:
    """Replying to a reply (B) flattens to the root (A): C.reply_to_id == A.id."""
    root = await _add_comment(client, admin_token, issue_key, "Root comment A")

    # B replies to A
    resp_b = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Reply B to A", "reply_to_id": root["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_b.status_code == 201, resp_b.text
    reply_b = resp_b.json()
    assert reply_b["reply_to_id"] == root["id"]

    # C replies to B — must be flattened to A
    resp_c = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Reply C to B (should flatten to A)", "reply_to_id": reply_b["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_c.status_code == 201, resp_c.text
    reply_c = resp_c.json()
    assert reply_c["reply_to_id"] == root["id"], (
        f"Expected reply_to_id={root['id']} (root A), got {reply_c['reply_to_id']} (B.id={reply_b['id']})"
    )


@pytest.mark.integration
async def test_reply_to_nonexistent_journal(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
) -> None:
    """Replying to a journal ID that does not exist returns 404."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Reply to ghost", "reply_to_id": 99999},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.integration
async def test_reply_to_journal_on_different_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Replying to a journal belonging to a different issue returns 422."""
    issue1 = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Issue 1"
    )
    issue2 = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Issue 2"
    )

    comment_on_issue1 = await _add_comment(client, admin_token, issue1["key"], "Comment on issue 1")

    # Try to reply to issue1's comment via issue2's endpoint
    resp = await client.post(
        f"/api/v1/issues/{issue2['key']}/journals/",
        json={"notes": "Cross-issue reply attempt", "reply_to_id": comment_on_issue1["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_reply_creates_with_correct_sequence(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
) -> None:
    """A reply gets the next sequence number after its root comment."""
    root = await _add_comment(client, admin_token, issue_key, "Root comment")
    root_sequence = root["sequence"]

    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Reply text", "reply_to_id": root["id"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    reply = resp.json()
    assert reply["sequence"] == root_sequence + 1


# ---------------------------------------------------------------------------
# Tests — permissions
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reply_requires_auth(
    client: AsyncClient,
    issue_key: str,
) -> None:
    """POST to journals endpoint without a token returns 401."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Unauthorized reply"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.integration
async def test_reply_to_private_note_blocked_for_non_admin(
    client: AsyncClient,
    db_session: AsyncSession,
    regular_user_with_comment_permission: User,
    regular_token: str,
    issue_key: str,
    admin_user: User,
    project: Project,
) -> None:
    """Non-admin user cannot reply to a private journal; expects 422."""
    issue = await _resolve_issue_db(db_session, issue_key)

    private_journal = Journal(
        issue_id=issue.id,
        user_id=admin_user.id,
        notes="private admin note",
        is_private=True,
        sequence=99,
        project_id=project.id,
    )
    db_session.add(private_journal)
    await db_session.commit()
    await db_session.refresh(private_journal)

    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "reply to private note", "reply_to_id": private_journal.id},
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_admin_can_reply_to_private_note(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    admin_token: str,
    issue_key: str,
    project: Project,
) -> None:
    """Admin user can reply to a private journal; expects 201."""
    issue = await _resolve_issue_db(db_session, issue_key)

    private_journal = Journal(
        issue_id=issue.id,
        user_id=admin_user.id,
        notes="private admin note",
        is_private=True,
        sequence=98,
        project_id=project.id,
    )
    db_session.add(private_journal)
    await db_session.commit()
    await db_session.refresh(private_journal)

    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "admin reply to private note", "reply_to_id": private_journal.id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["reply_to_id"] == private_journal.id


@pytest.mark.integration
async def test_reply_permission_check(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    outsider_token: str,
    private_project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """User without membership in a private project gets 404 (anti-enumeration).

    The get_by_display_key lookup returns 404 for issues in projects the
    requesting user cannot see, so the outsider never reaches the journal logic.
    """
    # Admin creates an issue in the private project
    issue_data = await _create_issue_via_api(
        client, admin_token, private_project.key, tracker.id, open_status.id, priority.id, "Private project issue"
    )
    issue_key = issue_data["key"]
    comment = await _add_comment(client, admin_token, issue_key, "Comment on private project issue")

    # Outsider (no membership) tries to reply
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "outsider reply attempt", "reply_to_id": comment["id"]},
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404, resp.text
