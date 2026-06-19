"""Integration tests for per-project FTS analyzer language on the QUERY side.

The index side already resolves the FTS config per project via
``specivo_fts_config(project_id)``. These tests verify the query side does the
same: a project whose ``projects.fts_language='russian'`` is searched with the
Russian stemmer (so ``диссертации`` and ``диссертация`` both match the indexed
``Защита диссертации``), while a project with no override (english default)
does NOT match Russian stemming.
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
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers (mirrored from tests/integration/test_search_fts.py)
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str = "fts_lang_user") -> User:
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
    fts_language: str | None = None,
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=True)
    if fts_language is not None:
        # Set the per-project FTS language BEFORE any content is created so the
        # index trigger (specivo_fts_config(project_id)) uses this language.
        proj.fts_language = fts_language
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def search_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="fts_lang_test_user")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
) -> AsyncClient:
    """Client authenticated as the search user (projects added per-test)."""
    token = await _login(client, search_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_russian_project_matches_both_stem_directions(
    db_session: AsyncSession,
    authed_client: AsyncClient,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """A project with fts_language='russian' resolves the russian stemmer on
    the query side, so both ``диссертации`` and ``диссертация`` match the
    indexed ``Защита диссертации``.
    """
    tracker, _, _ = lookups

    # Per-project russian language set BEFORE content creation -> index uses russian.
    ru_project = await _make_project(
        db_session, key="RUFT", identifier="ru-fts-project", fts_language="russian"
    )
    await _add_manager(db_session, ru_project, search_user)

    await _create_issue(authed_client, ru_project.key, tracker.id, "Защита диссертации")

    # Genitive form (as indexed)
    resp = await authed_client.get(
        SEARCH_URL, params={"q": "диссертации", "scope": "issues"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    subtitles = [i["subtitle"] for i in data["items"] if i["result_type"] == "issue"]
    assert any("диссертации" in s for s in subtitles)

    # Nominative form -> russian stemming should still match (both directions).
    resp2 = await authed_client.get(
        SEARCH_URL, params={"q": "диссертация", "scope": "issues"}
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["total_count"] >= 1
    subtitles2 = [i["subtitle"] for i in data2["items"] if i["result_type"] == "issue"]
    assert any("диссертации" in s for s in subtitles2)


@pytest.mark.asyncio
async def test_english_default_project_does_not_stem_russian(
    db_session: AsyncSession,
    authed_client: AsyncClient,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """A project with no override uses the english default: the russian
    nominative query does NOT match the indexed genitive form, proving the
    language is genuinely resolved per-project (not globally russian).
    """
    tracker, _, _ = lookups

    en_project = await _make_project(
        db_session, key="ENFT", identifier="en-fts-project"
    )
    await _add_manager(db_session, en_project, search_user)

    await _create_issue(authed_client, en_project.key, tracker.id, "Защита диссертации")

    # english stemmer treats these as distinct tokens -> nominative does not match genitive.
    resp = await authed_client.get(
        SEARCH_URL, params={"q": "диссертация", "scope": "issues"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    en_subtitles = [
        i["subtitle"]
        for i in data["items"]
        if i["result_type"] == "issue" and i["project_key"] == en_project.key
    ]
    assert not any("диссертации" in s for s in en_subtitles)
