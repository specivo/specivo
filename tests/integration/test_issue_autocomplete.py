"""Integration tests for issue autocomplete API.

Covers:
- GET /api/v1/issues/autocomplete/?q=...  — search by key or subject
- Authentication required (401)
- Empty query rejected (422)
- Project membership visibility (public vs private)
- Admin sees all issues regardless of membership
- SQL wildcard escaping (% and _ must not match everything)
- limit parameter capping results
- No results for nonsensical query
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="ACME", identifier="acme-app")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="ac_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autocomplete_returns_matching_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Autocomplete returns issues matching by key prefix."""
    # Create an issue first
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Automate weekly compliance report",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201, create_resp.text

    # Search by partial key
    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=ACME",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["key"] == "ACME-1"
    assert data[0]["subject"] == "Automate weekly compliance report"
    assert data[0]["project_key"] == "ACME"


@pytest.mark.asyncio
async def test_autocomplete_search_by_subject(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Autocomplete returns issues matching by subject substring."""
    await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Fix CSV parser edge case",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=CSV",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) >= 1
    assert "CSV" in data[0]["subject"]


@pytest.mark.asyncio
async def test_autocomplete_requires_auth(client: AsyncClient) -> None:
    """Unauthenticated request returns 401."""
    resp = await client.get("/api/v1/issues/autocomplete/?q=test")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_autocomplete_empty_query(
    client: AsyncClient,
    admin_token: str,
) -> None:
    """Empty query string returns 422 (min_length=1)."""
    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Additional fixtures for security and permission tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="ac_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_token(regular_user: User, client: AsyncClient) -> str:
    return await _login(client, regular_user.login)


@pytest_asyncio.fixture
async def member_role(db_session: AsyncSession) -> Role:
    """A simple assignable role for project membership.

    Uses a unique name derived from the db_session identity to avoid
    collisions with seeded roles or parallel test runs.
    """
    import uuid

    role = Role(
        name=f"AcRole-{uuid.uuid4().hex[:8]}",
        position=2,
        assignable=True,
        builtin=0,
        permissions=["view_issues", "add_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="PUB", identifier="pub-project", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="PRIV", identifier="priv-project", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


async def _create_issue(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
) -> dict:
    """Helper: create an issue via the API and return the response body."""
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
# Security and permission tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_autocomplete_respects_project_membership(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    public_project: Project,
    private_project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Non-member user sees issues in public projects but NOT in private ones."""
    await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Public project task"
    )
    await _create_issue(
        client, admin_token, private_project.key, tracker.id, open_status.id, priority.id, "Private project secret"
    )

    # Regular user is NOT a member of the private project.
    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=PUB",
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 200, resp.text
    keys = [item["key"] for item in resp.json()]
    assert any(k.startswith("PUB-") for k in keys), "Public project issue should appear"

    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=PRIV",
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 200, resp.text
    keys = [item["key"] for item in resp.json()]
    assert not any(k.startswith("PRIV-") for k in keys), "Private project issue must NOT appear"


@pytest.mark.integration
async def test_autocomplete_member_sees_private_project_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    private_project: Project,
    member_role: Role,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """User who IS a member of a private project can find its issues."""
    await _create_issue(
        client, admin_token, private_project.key, tracker.id, open_status.id, priority.id, "Member-visible private task"
    )

    # Grant membership.
    member = Member(project_id=private_project.id, user_id=regular_user.id)
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(member)

    member_role_row = MemberRole(member_id=member.id, role_id=member_role.id)
    db_session.add(member_role_row)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=PRIV",
        headers={"Authorization": f"Bearer {regular_token}"},
    )
    assert resp.status_code == 200, resp.text
    keys = [item["key"] for item in resp.json()]
    assert any(k.startswith("PRIV-") for k in keys), "Member should see private project issue"


@pytest.mark.integration
async def test_autocomplete_admin_sees_all(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    public_project: Project,
    private_project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Admin user sees issues from both public and private projects."""
    await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Admin visible public"
    )
    await _create_issue(
        client, admin_token, private_project.key, tracker.id, open_status.id, priority.id, "Admin visible private"
    )

    resp_pub = await client.get(
        "/api/v1/issues/autocomplete/?q=PUB",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_pub.status_code == 200, resp_pub.text
    assert any(item["key"].startswith("PUB-") for item in resp_pub.json())

    resp_priv = await client.get(
        "/api/v1/issues/autocomplete/?q=PRIV",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_priv.status_code == 200, resp_priv.text
    assert any(item["key"].startswith("PRIV-") for item in resp_priv.json())


@pytest.mark.integration
async def test_autocomplete_escapes_sql_wildcards(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A literal '%' or '_' in the query must not act as a SQL wildcard.

    If wildcards leak, a search for '%' would return ALL issues. We create
    one issue with a normal subject and verify the response is empty for
    '%' and '_' queries (neither the key nor subject contain these chars).
    """
    await _create_issue(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Normal issue without special chars"
    )

    for wildcard in ("%%", "__"):
        resp = await client.get(
            "/api/v1/issues/autocomplete/",
            params={"q": wildcard},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # The wildcard must not match the unrelated issue above.
        assert data == [], f"Query {wildcard!r} matched {len(data)} issue(s) — SQL wildcard was not escaped"


@pytest.mark.integration
async def test_autocomplete_limit_parameter(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """limit=2 caps results to 2 even when more matching issues exist."""
    for i in range(6):
        await _create_issue(
            client, admin_token, project.key, tracker.id, open_status.id, priority.id, f"Limit test issue {i}"
        )

    resp = await client.get(
        f"/api/v1/issues/autocomplete/?q={project.key}&limit=2",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2


@pytest.mark.integration
async def test_autocomplete_no_results(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
) -> None:
    """Query with no matching issues returns an empty array."""
    resp = await client.get(
        "/api/v1/issues/autocomplete/?q=ZZZNONEXISTENTZZZXXX",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
