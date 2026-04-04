"""Integration tests for search metadata filtering .

Verifies that search supports filtering by tracker, status, priority,
assigned_to, author, date ranges, and JSONB metadata fields. Uses the
same helper patterns as test_search_fts.py.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _make_project(db: AsyncSession, key: str = "FLT", identifier: str = "filter-project") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(db: AsyncSession) -> dict:
    """Seed two trackers, two statuses, and two priorities for filter testing."""
    status_new = StatusFactory.build(name="New", position=1, is_closed=False)
    status_done = StatusFactory.build(name="Done", position=2, is_closed=True)
    db.add(status_new)
    db.add(status_done)
    await db.flush()

    tracker_bug = TrackerFactory.build(name="Bug", default_status_id=status_new.id)
    tracker_feat = TrackerFactory.build(name="Feature", default_status_id=status_new.id)
    db.add(tracker_bug)
    db.add(tracker_feat)

    priority_low = PriorityFactory.build(name="Low", is_default=False, position=1)
    priority_high = PriorityFactory.build(name="High", is_default=True, position=2)
    db.add(priority_low)
    db.add(priority_high)

    await db.commit()
    for obj in (status_new, status_done, tracker_bug, tracker_feat, priority_low, priority_high):
        await db.refresh(obj)

    return {
        "status_new": status_new,
        "status_done": status_done,
        "tracker_bug": tracker_bug,
        "tracker_feat": tracker_feat,
        "priority_low": priority_low,
        "priority_high": priority_high,
    }


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
    **extra,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if description is not None:
        payload["description"] = description
    payload.update(extra)
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


async def _search(client: AsyncClient, q: str, **params) -> dict:
    resp = await client.get(SEARCH_URL, params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> dict:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def filter_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="filter_test_user")


@pytest_asyncio.fixture
async def second_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="filter_second_user")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    filter_user: User,
    lookups: dict,
) -> AsyncClient:
    """Client authenticated as a manager with wiki module enabled."""
    await _enable_wiki(db_session, project)
    await _add_manager(db_session, project, filter_user)
    token = await _login(client, filter_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests — Individual filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_by_tracker_id(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with tracker_id filter returns only issues of that tracker."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest alpha bug")
    await _create_issue(authed_client, project.key, lookups["tracker_feat"].id, "Filtertest alpha feature")

    data = await _search(authed_client, "filtertest alpha", scope="issues", tracker_id=lookups["tracker_bug"].id)
    assert data["total_count"] == 1
    assert "bug" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_filter_by_status_id(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with status_id filter returns only issues with that status."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest beta open")

    data = await _search(authed_client, "filtertest beta", scope="issues", status_id=lookups["status_new"].id)
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_filter_by_priority_id(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with priority_id filter returns only issues with that priority."""
    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest gamma highpri",
        priority_id=lookups["priority_high"].id,
    )
    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest gamma lowpri",
        priority_id=lookups["priority_low"].id,
    )

    data = await _search(authed_client, "filtertest gamma", scope="issues", priority_id=lookups["priority_high"].id)
    assert data["total_count"] == 1
    assert "highpri" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_filter_by_assigned_to_id(
    db_session: AsyncSession,
    authed_client: AsyncClient,
    project: Project,
    filter_user: User,
    second_user: User,
    lookups: dict,
):
    """Search with assigned_to_id filter returns only issues assigned to that user."""
    await _add_manager(db_session, project, second_user)

    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest delta assigned",
        assigned_to_id=filter_user.id,
    )
    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest delta unassigned",
    )

    data = await _search(authed_client, "filtertest delta", scope="issues", assigned_to_id=filter_user.id)
    assert data["total_count"] == 1
    assert "assigned" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_filter_by_author_id(
    db_session: AsyncSession,
    authed_client: AsyncClient,
    client: AsyncClient,
    project: Project,
    filter_user: User,
    second_user: User,
    lookups: dict,
):
    """Search with author_id filter returns only issues authored by that user."""
    await _add_manager(db_session, project, second_user)

    # First user creates an issue
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest epsilon byauthor")

    # Second user creates an issue
    second_token = await _login(client, second_user.login)
    client.headers["Authorization"] = f"Bearer {second_token}"
    await _create_issue(client, project.key, lookups["tracker_bug"].id, "Filtertest epsilon byother")

    # Filter by first user as author
    data = await _search(authed_client, "filtertest epsilon", scope="issues", author_id=filter_user.id)
    assert data["total_count"] == 1
    assert "byauthor" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_filter_by_created_after(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with created_after filter excludes issues created before that date."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest zeta recent")

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    data = await _search(authed_client, "filtertest zeta recent", scope="issues", created_after=yesterday)
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_filter_by_created_before(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with created_before filter excludes issues created after that date."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest eta old")

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    data = await _search(authed_client, "filtertest eta old", scope="issues", created_before=tomorrow)
    assert data["total_count"] >= 1


@pytest.mark.asyncio
async def test_filter_by_date_range(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with both created_after and created_before narrows to a date range."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest theta ranged")

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    data = await _search(
        authed_client,
        "filtertest theta ranged",
        scope="issues",
        created_after=yesterday,
        created_before=tomorrow,
    )
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_filter_by_metadata_jsonb(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Search with metadata filter matches JSONB containment."""
    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest iota metaissue",
        metadata={"env": "prod"},
    )
    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest iota other",
        metadata={"env": "staging"},
    )

    data = await _search(
        authed_client,
        "filtertest iota",
        scope="issues",
        metadata='{"env":"prod"}',
    )
    assert data["total_count"] == 1
    assert "metaissue" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_metadata_containment(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """JSONB @> containment: {"env":"prod"} matches {"env":"prod","tier":"1"}."""
    await _create_issue(
        authed_client,
        project.key,
        lookups["tracker_bug"].id,
        "Filtertest kappa multifield",
        metadata={"env": "prod", "tier": "1"},
    )

    data = await _search(
        authed_client,
        "filtertest kappa",
        scope="issues",
        metadata='{"env":"prod"}',
    )
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_combined_filters(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Multiple filters (tracker + status + date) are combined with AND."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest lambda combo match")
    await _create_issue(authed_client, project.key, lookups["tracker_feat"].id, "Filtertest lambda combo nomatch")

    data = await _search(
        authed_client,
        "filtertest lambda combo",
        scope="issues",
        tracker_id=lookups["tracker_bug"].id,
        status_id=lookups["status_new"].id,
        created_after=yesterday,
        created_before=tomorrow,
    )
    assert data["total_count"] == 1
    assert "match" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_filters_in_hybrid_mode(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Filters apply equally in hybrid search mode."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest mu hybrid bug")
    await _create_issue(authed_client, project.key, lookups["tracker_feat"].id, "Filtertest mu hybrid feature")

    data = await _search(
        authed_client,
        "filtertest mu hybrid",
        mode="hybrid",
        scope="issues",
        tracker_id=lookups["tracker_bug"].id,
    )
    assert data["total_count"] == 1
    assert "bug" in data["items"][0]["subtitle"].lower()


@pytest.mark.asyncio
async def test_filters_do_not_affect_wiki_results(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Issue-specific filters (tracker_id, status_id) do not filter out wiki results."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest nu shared topic")
    await _create_wiki_page(authed_client, project.key, "Nu Topic Guide", "Filtertest nu shared topic wiki text")

    # scope=all with tracker_id filter — wiki results should still appear
    data = await _search(
        authed_client,
        "filtertest nu shared topic",
        scope="all",
        tracker_id=lookups["tracker_bug"].id,
    )
    result_types = {item["result_type"] for item in data["items"]}
    assert "wiki" in result_types


@pytest.mark.asyncio
async def test_filter_with_no_matches_returns_empty(
    authed_client: AsyncClient,
    project: Project,
    lookups: dict,
):
    """Filters that match no issues return empty results with total_count=0."""
    await _create_issue(authed_client, project.key, lookups["tracker_bug"].id, "Filtertest xi nomatch")

    data = await _search(
        authed_client,
        "filtertest xi nomatch",
        scope="issues",
        tracker_id=lookups["tracker_feat"].id,  # wrong tracker
    )
    assert data["total_count"] == 0
    assert data["items"] == []
