"""Integration tests for the Tags API.

Covers:
- Tag vocabulary CRUD (create, list with usage, rename/recolor, delete)
- Case-insensitive uniqueness per project
- Autocomplete search
- Applying / removing tags on issues and wiki pages (create on the fly)
- Permission enforcement: any member may apply; only managers may curate
- Filtering the issue list by tag
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.tag_service import TagService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="TAG", name="Tag Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="tag_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def member(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="tag_member", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient, admin: User) -> str:
    return await _login(client, admin.login)


@pytest_asyncio.fixture
async def member_token(client: AsyncClient, member: User) -> str:
    return await _login(client, member.login)


@pytest_asyncio.fixture
async def status_open(db_session: AsyncSession):
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status_open):
    t = TrackerFactory.build(name="Bug", default_status_id=status_open.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession):
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def issue(db_session, project, tracker, priority, status_open, admin):
    svc = IssueService()
    created = await svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Tag me"),
        admin,
    )
    await db_session.commit()
    await db_session.refresh(created)
    return created


@pytest_asyncio.fixture
async def wiki_page(db_session, project, admin):
    page, _ = await WikiService().create_page(db_session, project.id, title="Tagged Page", text="hello", author=admin)
    await db_session.commit()
    await db_session.refresh(page)
    return page


# ---------------------------------------------------------------------------
# Vocabulary CRUD
# ---------------------------------------------------------------------------


class TestTagVocabulary:
    async def test_create_and_list(self, client, project, admin_token):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/tags/",
            headers=_auth(admin_token),
            json={"name": "backend", "color": "#4f9d6c"},
        )
        assert resp.status_code == 201, resp.text
        tag = resp.json()
        assert tag["name"] == "backend"
        assert tag["color"] == "#4f9d6c"

        resp = await client.get(f"/api/v1/projects/{project.key}/tags/", headers=_auth(admin_token))
        assert resp.status_code == 200
        tags = resp.json()
        assert len(tags) == 1
        assert tags[0]["issue_count"] == 0
        assert tags[0]["wiki_count"] == 0

    async def test_duplicate_name_case_insensitive_conflicts(self, client, project, admin_token):
        await client.post(
            f"/api/v1/projects/{project.key}/tags/",
            headers=_auth(admin_token),
            json={"name": "Frontend"},
        )
        resp = await client.post(
            f"/api/v1/projects/{project.key}/tags/",
            headers=_auth(admin_token),
            json={"name": "frontend"},
        )
        assert resp.status_code == 409, resp.text

    async def test_rename_and_recolor(self, client, project, admin_token):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/tags/",
            headers=_auth(admin_token),
            json={"name": "old"},
        )
        tag_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/projects/{project.key}/tags/{tag_id}/",
            headers=_auth(admin_token),
            json={"name": "new", "color": "#abcdef"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "new"
        assert resp.json()["color"] == "#abcdef"

    async def test_delete(self, client, project, admin_token):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/tags/",
            headers=_auth(admin_token),
            json={"name": "temp"},
        )
        tag_id = resp.json()["id"]
        resp = await client.delete(f"/api/v1/projects/{project.key}/tags/{tag_id}/", headers=_auth(admin_token))
        assert resp.status_code == 204
        resp = await client.get(f"/api/v1/projects/{project.key}/tags/", headers=_auth(admin_token))
        assert resp.json() == []

    async def test_search_autocomplete(self, client, project, admin_token):
        for name in ("alpha", "alpine", "beta"):
            await client.post(
                f"/api/v1/projects/{project.key}/tags/",
                headers=_auth(admin_token),
                json={"name": name},
            )
        resp = await client.get(
            f"/api/v1/projects/{project.key}/tags/search/",
            headers=_auth(admin_token),
            params={"q": "alp"},
        )
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert names == {"alpha", "alpine"}

    async def test_member_cannot_curate(self, client, project, member_token):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/tags/",
            headers=_auth(member_token),
            json={"name": "nope"},
        )
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Issue tagging
# ---------------------------------------------------------------------------


class TestIssueTagging:
    async def test_member_can_set_tags_create_on_the_fly(self, client, issue, member_token):
        resp = await client.put(
            f"/api/v1/issues/{issue.display_key}/tags/",
            headers=_auth(member_token),
            json={"names": ["urgent", "Backend"]},
        )
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()}
        assert names == {"urgent", "Backend"}

    async def test_add_and_remove_one(self, client, project, issue, member_token, admin_token):
        resp = await client.post(
            f"/api/v1/issues/{issue.display_key}/tags/",
            headers=_auth(member_token),
            json={"name": "review"},
        )
        assert resp.status_code == 201, resp.text
        tag_id = resp.json()["id"]

        resp = await client.get(f"/api/v1/issues/{issue.display_key}/tags/", headers=_auth(member_token))
        assert {t["name"] for t in resp.json()} == {"review"}

        resp = await client.delete(f"/api/v1/issues/{issue.display_key}/tags/{tag_id}/", headers=_auth(member_token))
        assert resp.status_code == 204

        resp = await client.get(f"/api/v1/issues/{issue.display_key}/tags/", headers=_auth(member_token))
        assert resp.json() == []

    async def test_set_is_idempotent_and_replaces(self, client, issue, member_token):
        await client.put(
            f"/api/v1/issues/{issue.display_key}/tags/",
            headers=_auth(member_token),
            json={"names": ["a", "b"]},
        )
        resp = await client.put(
            f"/api/v1/issues/{issue.display_key}/tags/",
            headers=_auth(member_token),
            json={"names": ["b", "c"]},
        )
        assert {t["name"] for t in resp.json()} == {"b", "c"}

    async def test_filter_issue_list_by_tag(self, client, project, issue, member_token):
        resp = await client.post(
            f"/api/v1/issues/{issue.display_key}/tags/",
            headers=_auth(member_token),
            json={"name": "findme"},
        )
        tag_id = resp.json()["id"]

        resp = await client.get(
            f"/api/v1/projects/{project.key}/issues/",
            headers=_auth(member_token),
            params={"tag_id": tag_id, "status": "all"},
        )
        assert resp.status_code == 200
        keys = {i["key"] for i in resp.json()["items"]}
        assert issue.display_key in keys


# ---------------------------------------------------------------------------
# Wiki tagging
# ---------------------------------------------------------------------------


class TestWikiTagging:
    async def test_set_and_list_wiki_tags(self, client, project, wiki_page, member_token):
        resp = await client.put(
            f"/api/v1/projects/{project.key}/wiki/{wiki_page.slug}/tags/",
            headers=_auth(member_token),
            json={"names": ["docs", "howto"]},
        )
        assert resp.status_code == 200, resp.text
        assert {t["name"] for t in resp.json()} == {"docs", "howto"}

        resp = await client.get(
            f"/api/v1/projects/{project.key}/wiki/{wiki_page.slug}/tags/",
            headers=_auth(member_token),
        )
        assert {t["name"] for t in resp.json()} == {"docs", "howto"}

    async def test_shared_namespace_with_issues(self, client, project, issue, wiki_page, admin_token):
        # Tag both an issue and a wiki page with the same tag name; the usage
        # counts on the single project tag should reflect both.
        await client.post(
            f"/api/v1/issues/{issue.display_key}/tags/",
            headers=_auth(admin_token),
            json={"name": "shared"},
        )
        await client.post(
            f"/api/v1/projects/{project.key}/wiki/{wiki_page.slug}/tags/",
            headers=_auth(admin_token),
            json={"name": "shared"},
        )
        resp = await client.get(f"/api/v1/projects/{project.key}/tags/", headers=_auth(admin_token))
        rows = {t["name"]: t for t in resp.json()}
        assert rows["shared"]["issue_count"] == 1
        assert rows["shared"]["wiki_count"] == 1


# ---------------------------------------------------------------------------
# Cross-project tag autocomplete (search page tag filter)
# ---------------------------------------------------------------------------


class TestGlobalTagAutocomplete:
    async def test_dedup_across_projects(self, db_session, client, admin, admin_token):
        """The same name in two projects collapses to a single suggestion."""
        p1 = ProjectFactory.build(key="GA1", name="GA One", is_public=True)
        p2 = ProjectFactory.build(key="GA2", name="GA Two", is_public=True)
        db_session.add_all([p1, p2])
        await db_session.commit()
        await db_session.refresh(p1)
        await db_session.refresh(p2)

        svc = TagService()
        await svc.get_or_create(db_session, p1.id, "Backend", admin)
        await svc.get_or_create(db_session, p2.id, "backend", admin)  # same name, other project/case
        await svc.get_or_create(db_session, p1.id, "Frontend", admin)
        await db_session.commit()

        resp = await client.get("/api/v1/tags/search/", headers=_auth(admin_token), params={"q": "back"})
        assert resp.status_code == 200, resp.text
        names = [t["name"] for t in resp.json()]
        assert len(names) == 1
        assert names[0].lower() == "backend"

    async def test_access_control(self, db_session, client, admin, member, member_token):
        """Tags in private projects the user can't access are excluded; public ones aren't."""
        pub = ProjectFactory.build(key="GAP", name="Pub", is_public=True)
        priv = ProjectFactory.build(key="GPR", name="Priv", is_public=False)
        db_session.add_all([pub, priv])
        await db_session.commit()
        await db_session.refresh(pub)
        await db_session.refresh(priv)

        svc = TagService()
        await svc.get_or_create(db_session, pub.id, "publictag", admin)
        await svc.get_or_create(db_session, priv.id, "privatetag", admin)
        await db_session.commit()

        resp = await client.get("/api/v1/tags/search/", headers=_auth(member_token))
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()}
        assert "publictag" in names
        assert "privatetag" not in names

    async def test_admin_sees_private(self, db_session, client, admin, admin_token):
        """An admin sees tags from private projects too."""
        priv = ProjectFactory.build(key="GPR2", name="Priv2", is_public=False)
        db_session.add(priv)
        await db_session.commit()
        await db_session.refresh(priv)
        await TagService().get_or_create(db_session, priv.id, "adminonlytag", admin)
        await db_session.commit()

        resp = await client.get("/api/v1/tags/search/", headers=_auth(admin_token), params={"q": "adminonly"})
        assert resp.status_code == 200, resp.text
        assert {t["name"] for t in resp.json()} == {"adminonlytag"}

    async def test_query_and_limit(self, db_session, client, admin, admin_token):
        """The q substring matches case-insensitively and limit caps results."""
        p = ProjectFactory.build(key="GQL", name="Q", is_public=True)
        db_session.add(p)
        await db_session.commit()
        await db_session.refresh(p)
        svc = TagService()
        for n in ("apple", "apricot", "banana"):
            await svc.get_or_create(db_session, p.id, n, admin)
        await db_session.commit()

        resp = await client.get("/api/v1/tags/search/", headers=_auth(admin_token), params={"q": "AP"})
        assert {t["name"] for t in resp.json()} == {"apple", "apricot"}

        resp = await client.get("/api/v1/tags/search/", headers=_auth(admin_token), params={"limit": 1})
        assert len(resp.json()) == 1
