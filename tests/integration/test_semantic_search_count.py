"""Integration tests for semantic search count visibility bug .

The current semantic_search() count query ignores visibility filters:
it counts ALL matching chunks regardless of whether the user can see the
source entities. This leads to total_count being higher than the number
of results the user can actually access.

These tests verify that total_count respects the same visibility rules
applied to the result rows.
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
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
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


async def _make_project(
    db: AsyncSession,
    key: str,
    identifier: str,
    is_public: bool = True,
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=is_public)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, is_closed=False)
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add(priority)
    await db.commit()
    await db.refresh(status)
    await db.refresh(tracker)
    await db.refresh(priority)
    return tracker, status, priority


async def _add_member(
    db: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str] | None = None,
) -> None:
    if permissions is None:
        permissions = ["*"]
    role = Role(
        name=f"Role-{project.key}-{user.id}",
        permissions=permissions,
        builtin=0,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


async def _create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    subject: str,
    description: str | None = None,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if description is not None:
        payload["description"] = description
    resp = await client.post(f"/api/v1/projects/{project_key}/issues/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _search(client: AsyncClient, q: str, **params) -> dict:
    resp = await client.get(SEARCH_URL, params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, key="SEM", identifier="semantic-priv", is_public=False)


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="sem_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def outsider_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="sem_outsider")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_count_respects_visibility(
    db_session: AsyncSession,
    client: AsyncClient,
    private_project: Project,
    admin_user: User,
    outsider_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Create issues in a private project, search as non-member.

    The total_count should be 0 (not the total number of matching chunks),
    because the outsider cannot see any issues in the private project.

    BUG: The current semantic_search() count query does not apply visibility
    filters, so it returns the raw chunk count instead of 0.
    """
    tracker, _, _ = lookups
    await _add_member(db_session, private_project, admin_user)

    # Admin creates issues with distinctive text for semantic search
    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"

    await _create_issue(
        client,
        private_project.key,
        tracker.id,
        "Quantum entanglement in distributed systems",
        description="Exploring quantum computing approaches to consensus algorithms",
    )
    await _create_issue(
        client,
        private_project.key,
        tracker.id,
        "Quantum key distribution protocol",
        description="Implementing QKD for secure communication channels",
    )

    # Outsider searches — should see nothing from the private project
    outsider_token = await _login(client, outsider_user.login)
    client.headers["Authorization"] = f"Bearer {outsider_token}"

    data = await _search(client, "quantum computing consensus", mode="semantic")

    # The critical assertion: total_count must be 0 for a non-member
    assert data["total_count"] == 0, (
        f"Expected total_count=0 for non-member, got {data['total_count']}. "
        "The semantic search count query is not applying visibility filters."
    )
    assert len(data["items"]) == 0


@pytest.mark.asyncio
async def test_semantic_search_count_matches_visible_results(
    db_session: AsyncSession,
    client: AsyncClient,
    private_project: Project,
    admin_user: User,
    outsider_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """The total_count should equal the number of results the user can
    actually see.

    Create issues in both a public and private project. Search as a user
    who only has access to the public project. The total_count should match
    the number of visible results, not the total chunk count across all
    projects.
    """
    tracker, _, _ = lookups

    # Create a public project accessible to everyone
    public_project = await _make_project(
        db_session,
        key="SPB",
        identifier="semantic-pub",
        is_public=True,
    )
    await _add_member(db_session, public_project, admin_user)
    await _add_member(db_session, private_project, admin_user)

    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"

    # Issue in public project — visible to outsider
    await _create_issue(
        client,
        public_project.key,
        tracker.id,
        "Photosynthesis optimization algorithm",
        description="Bio-inspired approach to energy harvesting in sensor networks",
    )

    # Issue in private project — invisible to outsider
    await _create_issue(
        client,
        private_project.key,
        tracker.id,
        "Photosynthesis simulation model",
        description="High-fidelity simulation of photosynthetic processes",
    )

    # Outsider searches — should only see the public project's issue
    outsider_token = await _login(client, outsider_user.login)
    client.headers["Authorization"] = f"Bearer {outsider_token}"

    data = await _search(client, "photosynthesis algorithm energy", mode="semantic")

    # total_count must match the number of actually visible results
    visible_count = len(data["items"])
    assert data["total_count"] == visible_count, (
        f"total_count ({data['total_count']}) does not match visible "
        f"results ({visible_count}). The count query is not applying "
        "the same visibility filters as the result query."
    )
    # The outsider should only see results from the public project
    for item in data["items"]:
        assert item["project_key"] == public_project.key, (
            f"Non-member should not see results from private project, but got project_key={item['project_key']}"
        )
