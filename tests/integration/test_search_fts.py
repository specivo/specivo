"""Integration tests for full-text search API.

Covers:
- Issue search by subject and description
- Wiki page search by title and text
- Cross-entity search (scope=all)
- Ranking (subject/title weight A > description/text weight B)
- Project scoping
- Trigger updates on issue update
- Pagination (offset/limit with total_count)
- Snippet highlighting (<mark> tags)
- Scope filtering (issues only, wiki only)
- Validation (empty query -> 422)
- Auth required (401 without token)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str = "search_user") -> User:
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


async def _make_project(db: AsyncSession, key: str = "SRCH", identifier: str = "search-project") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=True)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, category="backlog")
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


async def _enable_wiki(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="wiki"))
    await db.commit()


async def _add_manager(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(
        name=f"Manager-{project.key}-{user.id}",
        permissions=["*"],
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


async def _create_wiki_page(
    client: AsyncClient,
    project_key: str,
    title: str,
    text: str,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/wiki/",
        json={"title": title, "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def search_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="search_test_user")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
) -> AsyncClient:
    """Client authenticated as a manager with wiki module enabled."""
    await _enable_wiki(db_session, project)
    await _add_manager(db_session, project, search_user)
    token = await _login(client, search_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_issues_by_subject(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    await _create_issue(authed_client, project.key, tracker.id, "JWT authentication system")

    resp = await authed_client.get(SEARCH_URL, params={"q": "authentication"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    types = {item["result_type"] for item in data["items"]}
    assert "issue" in types
    titles = [item["subtitle"] for item in data["items"] if item["result_type"] == "issue"]
    assert any("JWT authentication system" in t for t in titles)


@pytest.mark.asyncio
async def test_search_issues_by_description(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Planning document",
        description="The database migration strategy should be documented thoroughly",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "migration"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    issue_items = [i for i in data["items"] if i["result_type"] == "issue"]
    assert len(issue_items) >= 1


@pytest.mark.asyncio
async def test_search_wiki_pages(
    authed_client: AsyncClient,
    project: Project,
):
    await _create_wiki_page(
        authed_client,
        project.key,
        "Architecture Design Patterns",
        "This page describes the architecture of the system.",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "architecture"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    wiki_items = [i for i in data["items"] if i["result_type"] == "wiki"]
    assert len(wiki_items) >= 1


@pytest.mark.asyncio
async def test_search_across_issues_and_wiki(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Performance optimization plan",
    )
    await _create_wiki_page(
        authed_client,
        project.key,
        "Performance Guide",
        "Tips for performance optimization in production.",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "performance"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_types = {item["result_type"] for item in data["items"]}
    assert "issue" in result_types
    assert "wiki" in result_types


@pytest.mark.asyncio
async def test_search_ranking_subject_higher(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Issue with search term in subject should rank higher than one with term only in description."""
    tracker, _, _ = lookups
    # Issue 1: term in subject (weight A)
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Authentication module refactoring",
    )
    # Issue 2: term only in description (weight B)
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Unrelated task for sprint",
        description="We need to review the authentication flow in detail",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "authentication", "scope": "issues"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 2
    items = data["items"]
    # First result should be the one with the term in the subject
    assert "Authentication module refactoring" in items[0]["subtitle"]


@pytest.mark.asyncio
async def test_search_no_results(
    authed_client: AsyncClient,
):
    resp = await authed_client.get(SEARCH_URL, params={"q": "xyznonexistent"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_search_respects_project_scope(
    db_session: AsyncSession,
    authed_client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Search with project_key filter returns only results from that project."""
    tracker, _, _ = lookups

    # Create a second project and add the user as a manager
    proj2 = await _make_project(db_session, key="OTH", identifier="other-project")
    await _add_manager(db_session, proj2, search_user)

    # Create issues in both projects
    await _create_issue(authed_client, project.key, tracker.id, "Caching strategy for search")
    await _create_issue(authed_client, proj2.key, tracker.id, "Caching strategy for other")

    # Search with project_key filter — should only find the first project's issue
    resp = await authed_client.get(SEARCH_URL, params={"q": "caching", "project_key": project.key})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 1
    assert data["items"][0]["project_key"] == project.key


@pytest.mark.asyncio
async def test_search_after_update(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """After updating issue subject, search finds the new text."""
    tracker, _, _ = lookups
    issue = await _create_issue(authed_client, project.key, tracker.id, "Original subject here")

    # Update the subject
    resp = await authed_client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={"subject": "Refactored observability pipeline", "lock_version": issue["lock_version"]},
    )
    assert resp.status_code == 200, resp.text

    # Search for the new text
    resp2 = await authed_client.get(SEARCH_URL, params={"q": "observability", "scope": "issues"})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["total_count"] >= 1

    # Verify trigger updated the search vector: "original" alone should not match
    # since the subject was fully replaced
    resp3 = await authed_client.get(SEARCH_URL, params={"q": "original", "scope": "issues"})
    assert resp3.status_code == 200
    assert resp3.json()["total_count"] == 0


@pytest.mark.asyncio
async def test_search_pagination(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    # Create 5 issues all containing the word "pagination"
    for i in range(5):
        await _create_issue(
            authed_client,
            project.key,
            tracker.id,
            f"Pagination test issue number {i}",
        )

    # Request with limit=2
    resp = await authed_client.get(SEARCH_URL, params={"q": "pagination", "scope": "issues", "limit": 2})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 5
    assert len(data["items"]) == 2
    assert data["offset"] == 0
    assert data["limit"] == 2


@pytest.mark.asyncio
async def test_search_highlights(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Simple task",
        description="The deployment pipeline needs improvements for reliability",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "deployment", "scope": "issues"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    snippets = [item["snippet"] for item in data["items"] if item["snippet"]]
    assert any("<mark>" in s for s in snippets)


@pytest.mark.asyncio
async def test_search_scope_issues_only(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    await _create_issue(authed_client, project.key, tracker.id, "Scalability improvements")
    await _create_wiki_page(
        authed_client,
        project.key,
        "Scalability Guide",
        "Guide to scalability in the system.",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "scalability", "scope": "issues"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_types = {item["result_type"] for item in data["items"]}
    assert result_types == {"issue"}


@pytest.mark.asyncio
async def test_search_scope_wiki_only(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    tracker, _, _ = lookups
    await _create_issue(authed_client, project.key, tracker.id, "Monitoring dashboard setup")
    await _create_wiki_page(
        authed_client,
        project.key,
        "Monitoring Overview",
        "How to set up monitoring dashboards.",
    )

    resp = await authed_client.get(SEARCH_URL, params={"q": "monitoring", "scope": "wiki"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_types = {item["result_type"] for item in data["items"]}
    assert result_types == {"wiki"}


@pytest.mark.asyncio
async def test_search_empty_query_rejected(
    authed_client: AsyncClient,
):
    resp = await authed_client.get(SEARCH_URL, params={"q": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_requires_auth(
    client: AsyncClient,
):
    """Unauthenticated request should return 401."""
    resp = await client.get(SEARCH_URL, params={"q": "anything"})
    assert resp.status_code == 401
