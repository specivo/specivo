"""Search engine indexing of the pages anonymous visitors can read.

Indexing is off unless the instance setting ``search_engine_indexing`` is on,
and even then only pages served to the anonymous principal from a public,
active project opted in to anonymous access (with that switch on) lose
``noindex``. Everything else keeps ``X-Robots-Tag: noindex, nofollow,
noarchive``: private projects, projects not opted in, private issues,
signed-in visitors, sign-in, admin and the API. ``robots.txt`` and the
sitemap list exactly the same content and follow every change on the next
request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.middleware import ROBOTS_INDEX, ROBOTS_NOINDEX, ROBOTS_NOINDEX_FOLLOW
from specivo.models.issue import Issue
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services import search_engine_service
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import Permission
from specivo.services.search_engine_service import SEARCH_ENGINE_INDEXING_SETTING_KEY
from specivo.services.settings_service import SettingsService
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

TERM = "quokkalantern"
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@dataclass(frozen=True)
class World:
    admin: User
    member: User
    both: Project
    issues_only: Project
    wiki_only: Project
    not_opted_in: Project
    private: Project
    archived: Project
    public_issue: Issue
    private_issue: Issue
    public_child: Issue
    private_child: Issue
    private_project_issue: Issue
    not_opted_issue: Issue
    wiki_slug: str
    private_wiki_slug: str


async def _project(db: AsyncSession, key: str, *, is_public: bool = True) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public, status=1)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))
    await db.commit()
    return project


async def _issue(db, project, lookups, author, subject, *, description="", is_private=False, parent=None) -> Issue:
    tracker, status, priority = lookups
    seq = (
        await db.execute(
            select(func.coalesce(func.max(Issue.sequence_number), 0)).where(Issue.project_id == project.id)
        )
    ).scalar_one()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq + 1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        assigned_to_id=None,
        subject=subject,
        description=description,
        is_private=is_private,
        parent_id=parent.id if parent is not None else None,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    """Every configuration, opted in, with the anonymous switch on and indexing still off."""
    admin = AdminUserFactory.build(login="seoidx_admin", status="active")
    member = UserFactory.build(login="seoidx_member", status="active")
    db_session.add_all([admin, member])
    await db_session.commit()

    status = StatusFactory.build(name="SeoidxNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="SeoidxBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="SeoidxNormal", is_default=True, position=1)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    lookups = (tracker, status, priority)

    both = await _project(db_session, "SEOBOTH")
    issues_only = await _project(db_session, "SEOISS")
    wiki_only = await _project(db_session, "SEOWIK")
    not_opted_in = await _project(db_session, "SEOPUB")
    private = await _project(db_session, "SEOPRIV", is_public=False)
    archived = await _project(db_session, "SEOARCH")

    public_issue = await _issue(
        db_session,
        both,
        lookups,
        member,
        f"Public {TERM} issue",
        description=f"## Summary\n\nThe **{TERM}** flickers when [the lamp](https://example.com) is cold.",
    )
    private_issue = await _issue(db_session, both, lookups, member, f"Secret {TERM}", is_private=True)
    public_child = await _issue(db_session, both, lookups, member, f"Child {TERM}", parent=public_issue)
    private_child = await _issue(
        db_session, both, lookups, member, f"Hidden child {TERM}", is_private=True, parent=public_issue
    )
    await _issue(db_session, issues_only, lookups, member, f"Issues-only {TERM}")
    private_project_issue = await _issue(db_session, private, lookups, member, f"Private project {TERM}")
    not_opted_issue = await _issue(db_session, not_opted_in, lookups, member, f"Not opted {TERM}")
    await _issue(db_session, archived, lookups, member, f"Archived {TERM}")

    wiki = WikiService()
    page, _ = await wiki.create_page(db_session, both.id, "Seo Home", f"Wiki text about the {TERM}.", admin)
    await wiki.create_page(db_session, wiki_only.id, "Seo Home", f"Wiki-only {TERM}.", admin)
    await wiki.create_page(db_session, issues_only.id, "Hidden Wiki", f"Issues-only wiki {TERM}.", admin)
    private_page, _ = await wiki.create_page(db_session, private.id, "Private Wiki", f"Private {TERM}.", admin)
    await db_session.commit()

    for project, permissions in (
        (both, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]),
        (issues_only, [Permission.VIEW_ISSUES]),
        (wiki_only, [Permission.VIEW_WIKI]),
        (archived, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]),
    ):
        await set_anonymous_permissions(db_session, project, permissions, admin)
    await db_session.commit()
    archived.status = 9
    await db_session.commit()

    await set_anonymous_access_enabled(
        db_session, True, admin, confirmed_projects=[both.key, issues_only.key, wiki_only.key, archived.key]
    )
    await db_session.commit()

    return World(
        admin=admin,
        member=member,
        both=both,
        issues_only=issues_only,
        wiki_only=wiki_only,
        not_opted_in=not_opted_in,
        private=private,
        archived=archived,
        public_issue=public_issue,
        private_issue=private_issue,
        public_child=public_child,
        private_child=private_child,
        private_project_issue=private_project_issue,
        not_opted_issue=not_opted_issue,
        wiki_slug=page.slug,
        private_wiki_slug=private_page.slug,
    )


async def _set_indexing(db: AsyncSession, value: str | None) -> None:
    await SettingsService().set_many(db, {SEARCH_ENGINE_INDEXING_SETTING_KEY: value})
    await db.commit()


@pytest_asyncio.fixture
async def indexed(db_session: AsyncSession, world: World) -> World:
    """*world* with search engine indexing turned on."""
    await _set_indexing(db_session, "true")
    return world


def _indexable_pages(w: World) -> dict[str, str]:
    return {
        "projects": "/projects/",
        "project": f"/projects/{w.both.key}/",
        "issues": f"/projects/{w.both.key}/issues/",
        "issue": f"/issue/{w.public_issue.display_key}/",
        "child": f"/issue/{w.public_child.display_key}/",
        "wiki": f"/projects/{w.both.key}/wiki/{w.wiki_slug}/",
        "wiki_only": f"/projects/{w.wiki_only.key}/wiki/{w.wiki_slug}/",
    }


def _signed_in(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _base() -> str:
    return get_settings().app_url.rstrip("/")


def _locs(xml: str) -> list[str]:
    return [el.text or "" for el in ElementTree.fromstring(xml).iter(f"{{{_NS['sm']}}}loc")]


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


async def test_indexing_is_off_by_default(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    assert not await search_engine_service.is_indexing_enabled(db_session)
    for path in _indexable_pages(world).values():
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["x-robots-tag"] == ROBOTS_NOINDEX, path
        assert 'rel="canonical"' not in resp.text
    robots = await client.get("/robots.txt")
    assert robots.text == "User-agent: *\nDisallow: /\n"
    assert (await client.get("/sitemap.xml")).status_code == 404


async def test_the_environment_variable_turns_indexing_on_and_the_admin_setting_wins(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "search_engine_indexing", True)
    assert await search_engine_service.is_indexing_enabled(db_session)
    await _set_indexing(db_session, "false")
    assert not await search_engine_service.is_indexing_enabled(db_session)


async def test_a_stealth_prefix_keeps_indexing_off(
    db_session: AsyncSession, indexed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """robots.txt and the sitemap live at the root and would reveal the prefix."""
    monkeypatch.setattr(get_settings(), "stealth_prefix", "/hidden")
    assert not await search_engine_service.is_indexing_enabled(db_session)


# ---------------------------------------------------------------------------
# The header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", ["projects", "project", "issues", "issue", "child", "wiki", "wiki_only"])
async def test_a_public_page_served_anonymously_is_indexable(client: AsyncClient, indexed: World, page: str) -> None:
    path = _indexable_pages(indexed)[page]
    resp = await client.get(path)
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == ROBOTS_INDEX
    assert "noindex" not in resp.headers["x-robots-tag"]
    assert "nofollow" not in resp.headers["x-robots-tag"]
    assert f'<link rel="canonical" href="{_base()}{path}">' in resp.text
    # Shared caches are still kept out.
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("page", ["projects", "project", "issues", "issue", "wiki"])
async def test_the_same_page_for_a_signed_in_user_keeps_noindex(client: AsyncClient, indexed: World, page: str) -> None:
    resp = await client.get(_indexable_pages(indexed)[page], cookies=_signed_in(indexed.admin))
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == ROBOTS_NOINDEX
    assert 'rel="canonical"' not in resp.text


async def test_content_outside_the_opt_in_keeps_noindex(client: AsyncClient, indexed: World) -> None:
    w = indexed
    for path in (
        f"/issue/{w.private_issue.display_key}/",
        f"/issue/{w.private_child.display_key}/",
        f"/issue/{w.private_project_issue.display_key}/",
        f"/issue/{w.not_opted_issue.display_key}/",
        f"/projects/{w.private.key}/",
        f"/projects/{w.private.key}/wiki/{w.private_wiki_slug}/",
        f"/projects/{w.not_opted_in.key}/",
        f"/projects/{w.archived.key}/",
        f"/projects/{w.issues_only.key}/wiki/hidden-wiki/",
        f"/projects/{w.wiki_only.key}/issues/",
        f"/projects/{w.both.key}/roadmap/",
        f"/projects/{w.both.key}/wiki/",
        "/",
        "/admin/",
    ):
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code != 200, path
        assert resp.headers["x-robots-tag"] == ROBOTS_NOINDEX, path
    # The sign-in page is served, and still never indexed.
    login = await client.get("/login/")
    assert login.status_code == 200
    assert login.headers["x-robots-tag"] == ROBOTS_NOINDEX


async def test_the_api_keeps_noindex_even_when_served_anonymously(client: AsyncClient, indexed: World) -> None:
    for path in ("/api/v1/projects/", f"/api/v1/projects/{indexed.both.key}/issues/"):
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["x-robots-tag"] == ROBOTS_NOINDEX


async def test_search_and_query_pages_are_followed_but_not_indexed(client: AsyncClient, indexed: World) -> None:
    for path in (
        f"/search/?q={TERM}",
        "/search/",
        f"/projects/{indexed.both.key}/issues/?sort=subject",
        f"/issue/{indexed.public_issue.display_key}/?activity_page=1",
    ):
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["x-robots-tag"] == ROBOTS_NOINDEX_FOLLOW, path
        assert 'rel="canonical"' not in resp.text


async def test_public_issue_and_wiki_pages_carry_a_description(client: AsyncClient, indexed: World) -> None:
    issue = await client.get(_indexable_pages(indexed)["issue"])
    match = re.search(r'<meta name="description" content="([^"]*)">', issue.text)
    assert match is not None
    assert match.group(1) == f"Summary The {TERM} flickers when the lamp is cold."
    wiki = await client.get(_indexable_pages(indexed)["wiki"])
    assert f'<meta name="description" content="Wiki text about the {TERM}.">' in wiki.text


async def test_switching_anonymous_access_off_restores_noindex_on_the_next_request(
    client: AsyncClient, db_session: AsyncSession, indexed: World
) -> None:
    path = _indexable_pages(indexed)["issue"]
    assert (await client.get(path)).headers["x-robots-tag"] == ROBOTS_INDEX
    await set_anonymous_access_enabled(db_session, False, indexed.admin)
    await db_session.commit()
    resp = await client.get(path, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["x-robots-tag"] == ROBOTS_NOINDEX


async def test_turning_indexing_off_applies_on_the_next_request(
    client: AsyncClient, db_session: AsyncSession, indexed: World
) -> None:
    path = _indexable_pages(indexed)["wiki"]
    assert (await client.get(path)).headers["x-robots-tag"] == ROBOTS_INDEX
    await _set_indexing(db_session, "false")
    assert (await client.get(path)).headers["x-robots-tag"] == ROBOTS_NOINDEX


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


async def test_robots_txt_allows_only_the_readable_projects(client: AsyncClient, indexed: World) -> None:
    w = indexed
    body = (await client.get("/robots.txt")).text
    lines = body.splitlines()
    assert "Allow: /projects/$" in lines
    assert f"Allow: /projects/{w.both.key}/" in lines
    assert f"Allow: /issue/{w.both.key}-" in lines
    assert f"Allow: /projects/{w.issues_only.key}/" in lines
    assert f"Allow: /issue/{w.issues_only.key}-" in lines
    assert f"Allow: /projects/{w.wiki_only.key}/" in lines
    # A wiki-only project offers no issues.
    assert f"Allow: /issue/{w.wiki_only.key}-" not in lines
    for hidden in (w.private, w.not_opted_in, w.archived):
        assert hidden.key not in body
    assert "Disallow: /" in lines
    assert f"Sitemap: {_base()}/sitemap.xml" in lines
    for app_path in ("/login/", "/api/", "/search/", "/admin/"):
        assert f"Allow: {app_path}" not in lines


async def test_robots_txt_disallows_everything_without_anonymous_access(
    client: AsyncClient, db_session: AsyncSession, indexed: World
) -> None:
    await set_anonymous_access_enabled(db_session, False, indexed.admin)
    await db_session.commit()
    assert (await client.get("/robots.txt")).text == "User-agent: *\nDisallow: /\n"


async def test_robots_txt_disallows_everything_with_no_project_opted_in(
    client: AsyncClient, db_session: AsyncSession, indexed: World
) -> None:
    for project in (indexed.both, indexed.issues_only, indexed.wiki_only, indexed.archived):
        await set_anonymous_permissions(db_session, project, [], indexed.admin)
    await db_session.commit()
    assert (await client.get("/robots.txt")).text == "User-agent: *\nDisallow: /\n"


async def test_the_admin_robots_txt_wins(client: AsyncClient, db_session: AsyncSession, indexed: World) -> None:
    custom = "User-agent: *\nDisallow: /projects/SEOBOTH/\n"
    await SettingsService().set_many(db_session, {"robots_txt": custom})
    await db_session.commit()
    assert (await client.get("/robots.txt")).text == custom


async def test_the_robots_txt_environment_variable_wins(
    client: AsyncClient, indexed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = "User-agent: *\nDisallow: /private-by-env/\n"
    settings = get_settings()
    monkeypatch.setattr(settings, "robots_txt", custom)
    monkeypatch.setattr(settings, "__pydantic_fields_set__", settings.model_fields_set | {"robots_txt"})
    assert (await client.get("/robots.txt")).text == custom


# ---------------------------------------------------------------------------
# The sitemap
# ---------------------------------------------------------------------------


async def test_the_sitemap_lists_only_readable_content(client: AsyncClient, indexed: World) -> None:
    w = indexed
    base = _base()
    index = await client.get("/sitemap.xml")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("application/xml")
    sitemaps = _locs(index.text)
    assert sitemaps == [
        f"{base}/sitemaps/projects.xml",
        f"{base}/sitemaps/{w.both.key}/issues-0.xml",
        f"{base}/sitemaps/{w.both.key}/wiki-0.xml",
        f"{base}/sitemaps/{w.issues_only.key}/issues-0.xml",
        f"{base}/sitemaps/{w.wiki_only.key}/wiki-0.xml",
    ]

    urls: list[str] = []
    for sitemap in sitemaps:
        resp = await client.get(sitemap.removeprefix(base))
        assert resp.status_code == 200, sitemap
        assert "<lastmod>" in resp.text
        urls += _locs(resp.text)

    assert f"{base}/projects/" in urls
    assert f"{base}/projects/{w.both.key}/" in urls
    assert f"{base}/projects/{w.both.key}/issues/" in urls
    assert f"{base}/projects/{w.wiki_only.key}/" in urls
    assert f"{base}/projects/{w.wiki_only.key}/issues/" not in urls
    assert f"{base}/issue/{w.public_issue.display_key}/" in urls
    assert f"{base}/issue/{w.public_child.display_key}/" in urls
    assert f"{base}/projects/{w.both.key}/wiki/{w.wiki_slug}/" in urls
    joined = "\n".join(urls)
    for hidden in (w.private_issue, w.private_child, w.private_project_issue, w.not_opted_issue):
        assert f"/issue/{hidden.display_key}/" not in joined
    for hidden in (w.private, w.not_opted_in, w.archived):
        assert f"/{hidden.key}/" not in joined
    assert "hidden-wiki" not in joined


async def test_every_listed_url_is_indexable(client: AsyncClient, indexed: World) -> None:
    """The sitemap never names a page the page itself would refuse or mark noindex."""
    base = _base()
    for sitemap in _locs((await client.get("/sitemap.xml")).text):
        for url in _locs((await client.get(sitemap.removeprefix(base))).text):
            resp = await client.get(url.removeprefix(base))
            assert resp.status_code == 200, url
            assert resp.headers["x-robots-tag"] == ROBOTS_INDEX, url


async def test_sitemaps_outside_the_opt_in_are_not_found(client: AsyncClient, indexed: World) -> None:
    w = indexed
    for path in (
        f"/sitemaps/{w.private.key}/issues-0.xml",
        f"/sitemaps/{w.private.key}/wiki-0.xml",
        f"/sitemaps/{w.not_opted_in.key}/issues-0.xml",
        f"/sitemaps/{w.archived.key}/issues-0.xml",
        f"/sitemaps/{w.issues_only.key}/wiki-0.xml",
        f"/sitemaps/{w.wiki_only.key}/issues-0.xml",
        f"/sitemaps/{w.both.key}/issues-7.xml",
        f"/sitemaps/{w.both.key}/wiki-7.xml",
        "/sitemaps/NOSUCHKEY/issues-0.xml",
    ):
        assert (await client.get(path)).status_code == 404, path


async def test_the_sitemap_is_not_found_while_indexing_does_not_apply(
    client: AsyncClient, db_session: AsyncSession, indexed: World
) -> None:
    assert (await client.get("/sitemap.xml")).status_code == 200
    await set_anonymous_access_enabled(db_session, False, indexed.admin)
    await db_session.commit()
    for path in ("/sitemap.xml", "/sitemaps/projects.xml", f"/sitemaps/{indexed.both.key}/issues-0.xml"):
        assert (await client.get(path)).status_code == 404, path


async def test_the_sitemap_follows_an_opt_out_on_the_next_request(
    client: AsyncClient, db_session: AsyncSession, indexed: World
) -> None:
    assert f"/{indexed.both.key}/" in (await client.get("/sitemap.xml")).text
    await set_anonymous_permissions(db_session, indexed.both, [], indexed.admin)
    await db_session.commit()
    assert f"/{indexed.both.key}/" not in (await client.get("/sitemap.xml")).text
    assert (await client.get(f"/sitemaps/{indexed.both.key}/issues-0.xml")).status_code == 404


async def test_big_projects_are_split_into_chunks(
    client: AsyncClient, indexed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each chunk holds a fixed range of issue numbers; the private ones are skipped, not counted."""
    monkeypatch.setattr(search_engine_service, "SITEMAP_CHUNK_SIZE", 1)
    w = indexed
    base = _base()
    sitemaps = _locs((await client.get("/sitemap.xml")).text)
    both_issue_chunks = [s for s in sitemaps if f"/{w.both.key}/issues-" in s]
    # Issues 1 (public) and 3 (public child) are readable; 2 and 4 are private.
    assert both_issue_chunks == [
        f"{base}/sitemaps/{w.both.key}/issues-{w.public_issue.sequence_number - 1}.xml",
        f"{base}/sitemaps/{w.both.key}/issues-{w.public_child.sequence_number - 1}.xml",
    ]
    first = await client.get(both_issue_chunks[0].removeprefix(base))
    assert _locs(first.text) == [f"{base}/issue/{w.public_issue.display_key}/"]
    private_chunk = f"/sitemaps/{w.both.key}/issues-{w.private_issue.sequence_number - 1}.xml"
    assert (await client.get(private_chunk)).status_code == 404


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


async def test_admins_turn_indexing_on_and_off_from_the_settings_page(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    page = await admin_client.get("/admin/settings/")
    assert page.status_code == 200
    assert 'name="search_engine_indexing"' in page.text
    assert 'id="search_engine_indexing" value="true" checked' not in page.text

    resp = await admin_client.post(
        "/admin/settings/search-engines/", data={"search_engine_indexing": ["false", "true"]}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert await search_engine_service.is_indexing_enabled(db_session)
    assert 'id="search_engine_indexing" value="true" checked' in (await admin_client.get("/admin/settings/")).text

    await admin_client.post("/admin/settings/search-engines/", data={"search_engine_indexing": "false"})
    assert not await search_engine_service.is_indexing_enabled(db_session)


async def test_a_non_admin_cannot_change_the_setting(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await auth_client.post(
        "/admin/settings/search-engines/", data={"search_engine_indexing": "true"}, follow_redirects=False
    )
    assert resp.status_code in (302, 303, 403)
    assert not await search_engine_service.is_indexing_enabled(db_session)
