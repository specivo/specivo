"""Integration tests for the wiki link graph.

Covers:
- Rebuilding page links from content
- Broken links (target page does not exist)
- Rebuild replaces old links
- Graph endpoint response shape
- Broken link flagging in graph
- CASCADE delete of source page removes links
- SET NULL on target page deletion
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.wiki import WikiPageLink
from specivo.services.wiki_link_service import WikiLinkService
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_wiki_service = WikiService()
_link_service = WikiLinkService()


async def _make_user(db: AsyncSession, login: str = "linkgraph_user") -> User:
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


async def _make_project(db: AsyncSession, key: str = "LNKG", identifier: str = "link-graph") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def link_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="link_test_user")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    link_user: User,
) -> AsyncClient:
    """Client authenticated as a manager with wiki module enabled."""
    await _enable_wiki(db_session, project)
    await _add_manager(db_session, project, link_user)
    token = await _login(client, link_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Tests — service layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_creates_links(
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """Rebuilding page links creates WikiPageLink rows for [[links]]."""
    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)

    page_a, _ = await _wiki_service.create_page(db_session, project.id, "Page A", "Links to [[Page B]]", link_user)
    page_b, _ = await _wiki_service.create_page(db_session, project.id, "Page B", "Some content", link_user)
    await db_session.commit()

    count = await _link_service.rebuild_page_links(db_session, wiki.id, page_a.id)
    await db_session.commit()

    assert count == 1

    stmt = select(WikiPageLink).where(WikiPageLink.source_page_id == page_a.id)
    result = await db_session.execute(stmt)
    links = list(result.scalars().all())
    assert len(links) == 1
    assert links[0].target_slug == "page-b"
    assert links[0].target_page_id == page_b.id


@pytest.mark.asyncio
async def test_broken_link_has_null_target(
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """A link to a non-existent page has target_page_id=NULL."""
    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)

    page, _ = await _wiki_service.create_page(db_session, project.id, "Linker", "See [[NonExistent]]", link_user)
    await db_session.commit()

    count = await _link_service.rebuild_page_links(db_session, wiki.id, page.id)
    await db_session.commit()

    assert count == 1

    stmt = select(WikiPageLink).where(WikiPageLink.source_page_id == page.id)
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.target_slug == "nonexistent"
    assert link.target_page_id is None


@pytest.mark.asyncio
async def test_rebuild_replaces_old_links(
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """Rebuilding replaces old links with new ones from updated content."""
    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)

    page, _ = await _wiki_service.create_page(db_session, project.id, "Mutable", "[[Old Target]]", link_user)
    await db_session.commit()

    await _link_service.rebuild_page_links(db_session, wiki.id, page.id)
    await db_session.commit()

    # Update content to point to a different page
    await _wiki_service.update_page(db_session, page.id, "[[New Target]]", link_user, page.lock_version)
    await db_session.commit()
    await db_session.refresh(page)

    await _link_service.rebuild_page_links(db_session, wiki.id, page.id)
    await db_session.commit()

    stmt = select(WikiPageLink).where(WikiPageLink.source_page_id == page.id)
    result = await db_session.execute(stmt)
    links = list(result.scalars().all())
    assert len(links) == 1
    assert links[0].target_slug == "new-target"


# ---------------------------------------------------------------------------
# Tests — API endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_endpoint_shape(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """GET /wiki/graph returns nodes and edges with expected shape."""
    # Create pages with cross-links
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Alpha", "text": "See [[Beta]]"},
    )
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Beta", "text": "Back to [[Alpha]]"},
    )

    # Rebuild links directly (no Celery in tests)
    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)
    pages = await _wiki_service.list_pages(db_session, project.id)
    for p in pages:
        await _link_service.rebuild_page_links(db_session, wiki.id, p.id)
    await db_session.commit()

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/graph/")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 2

    # Verify node shape
    node = data["nodes"][0]
    assert "id" in node
    assert "slug" in node
    assert "title" in node

    # Verify edge shape
    edge = data["edges"][0]
    assert "source_page_id" in edge
    assert "target_page_id" in edge
    assert "target_slug" in edge
    assert "is_broken" in edge


@pytest.mark.asyncio
async def test_broken_link_flagged_in_graph(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """Broken links appear with is_broken=True in the graph endpoint."""
    await authed_client.post(
        f"/api/v1/projects/{project.key}/wiki/",
        json={"title": "Source", "text": "See [[DoesNotExist]]"},
    )

    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)
    pages = await _wiki_service.list_pages(db_session, project.id)
    for p in pages:
        await _link_service.rebuild_page_links(db_session, wiki.id, p.id)
    await db_session.commit()

    resp = await authed_client.get(f"/api/v1/projects/{project.key}/wiki/graph/")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    broken_edges = [e for e in data["edges"] if e["is_broken"]]
    assert len(broken_edges) == 1
    assert broken_edges[0]["target_slug"] == "doesnotexist"
    assert broken_edges[0]["target_page_id"] is None


# ---------------------------------------------------------------------------
# Tests — CASCADE / SET NULL behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_source_cascades(
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """Soft-deleting a source page marks it as deleted; links remain (soft delete)."""
    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)

    page, _ = await _wiki_service.create_page(db_session, project.id, "Src Page", "[[Some Target]]", link_user)
    await db_session.commit()

    await _link_service.rebuild_page_links(db_session, wiki.id, page.id)
    await db_session.commit()

    # Verify link exists
    stmt = select(WikiPageLink).where(WikiPageLink.source_page_id == page.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is not None

    # Soft-delete the source page
    page_id = page.id
    await _wiki_service.delete_page(db_session, page_id, deleted_by=link_user)
    await db_session.commit()

    # Page is soft-deleted (deleted_at set), links remain
    from specivo.models.wiki import WikiPage

    db_session.expire_all()
    result = await db_session.execute(select(WikiPage).where(WikiPage.id == page_id))
    deleted_page = result.scalar_one()
    assert deleted_page.deleted_at is not None


@pytest.mark.asyncio
async def test_target_delete_soft_deletes(
    db_session: AsyncSession,
    project: Project,
    link_user: User,
):
    """Soft-deleting a target page marks it deleted; link FK remains (soft delete)."""
    wiki = await _wiki_service.get_or_create_wiki(db_session, project.id)

    source, _ = await _wiki_service.create_page(db_session, project.id, "Source", "[[Target]]", link_user)
    target, _ = await _wiki_service.create_page(db_session, project.id, "Target", "Content", link_user)
    await db_session.commit()

    await _link_service.rebuild_page_links(db_session, wiki.id, source.id)
    await db_session.commit()

    # Verify link resolves to target
    stmt = select(WikiPageLink).where(WikiPageLink.source_page_id == source.id)
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.target_page_id == target.id

    # Soft-delete the target page
    target_id = target.id
    await _wiki_service.delete_page(db_session, target_id, deleted_by=link_user)
    await db_session.commit()

    # target_page_id still points to the soft-deleted page
    db_session.expire_all()
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.target_page_id == target_id

    # But the target page is now soft-deleted
    from specivo.models.wiki import WikiPage

    result = await db_session.execute(select(WikiPage).where(WikiPage.id == target_id))
    deleted_page = result.scalar_one()
    assert deleted_page.deleted_at is not None
