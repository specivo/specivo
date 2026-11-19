"""The web search page's project scope, metadata filter, tag filter and failure handling.

Characterization tests: they pin what the page shows for each combination of
``q``, ``project_key``, ``mf``/``mv``/``ma`` and ``tag``, so the handler can be
restructured without changing what a visitor sees.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.web.pages import search as search_page
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

WORD = "quillwort"


async def _project(db: AsyncSession, key: str) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower())
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _manage(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(name=f"Manager-{project.key}", permissions=["*"], builtin=0)
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _issue(client: AsyncClient, project_key: str, tracker_id: int, subject: str, **extra: object) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json={"project_key": project_key, "tracker_id": tracker_id, "subject": subject, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, client: AsyncClient) -> dict:
    """Two projects managed by one signed-in user, with issues in each."""
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add(tracker)
    db_session.add(PriorityFactory.build(name="Normal", is_default=True, position=1))
    user = UserFactory.build(login="websearch_filters_user", status="active")
    db_session.add(user)
    await db_session.commit()
    first = await _project(db_session, "WSFA")
    second = await _project(db_session, "WSFB")
    for project in (first, second):
        await _manage(db_session, project, user)

    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    client.cookies.set("access_token", token)

    keys = {
        "first": await _issue(client, "WSFA", tracker.id, f"{WORD} gearbox", metadata={"env": "prod"}),
        "first_staging": await _issue(client, "WSFA", tracker.id, "Staging only", metadata={"env": "staging"}),
        "second": await _issue(client, "WSFB", tracker.id, f"{WORD} gearbox", metadata={"env": "prod"}),
    }
    return {"keys": keys}


async def _page(client: AsyncClient, **params: object) -> str:
    resp = await client.get("/search/", params={"mode": "keyword", **params})
    assert resp.status_code == 200, resp.text
    return resp.text


async def test_a_project_key_scopes_the_results(client: AsyncClient, world: dict) -> None:
    page = await _page(client, q=WORD, project_key="WSFA")
    assert world["keys"]["first"] in page
    assert world["keys"]["second"] not in page


async def test_an_unknown_project_key_searches_every_project(client: AsyncClient, world: dict) -> None:
    page = await _page(client, q=WORD, project_key="NOSUCHKEY")
    assert world["keys"]["first"] in page
    assert world["keys"]["second"] in page


async def test_a_metadata_filter_alone_lists_matching_issues(client: AsyncClient, world: dict) -> None:
    page = await _page(client, mf="env", mv="prod", project_key="WSFA", scope="wiki")
    assert world["keys"]["first"] in page
    assert world["keys"]["first_staging"] not in page
    assert world["keys"]["second"] not in page


async def test_an_invalid_metadata_field_is_ignored(client: AsyncClient, world: dict) -> None:
    page = await _page(client, mf="Not A Slug!", mv="prod", project_key="WSFA")
    assert world["keys"]["first"] not in page
    assert "Not A Slug!" not in page


async def test_a_metadata_filter_narrows_a_query(client: AsyncClient, world: dict) -> None:
    page = await _page(client, q="gearbox", mf="env", mv="staging")
    assert world["keys"]["first"] not in page
    assert world["keys"]["second"] not in page


async def test_a_tag_with_a_metadata_filter_lists_only_tagged_issues(client: AsyncClient, world: dict) -> None:
    resp = await client.post(f"/api/v1/issues/{world['keys']['first']}/tags/", json={"name": "pump"})
    assert resp.status_code in (200, 201), resp.text
    page = await _page(client, mf="env", mv="prod", tag="pump")
    assert world["keys"]["first"] in page
    assert world["keys"]["second"] not in page


async def test_a_failing_search_shows_no_results(
    client: AsyncClient, world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("search backend down")

    monkeypatch.setattr(search_page._search_svc, "search", broken)
    page = await _page(client, q=WORD)
    assert world["keys"]["first"] not in page
    assert world["keys"]["second"] not in page
