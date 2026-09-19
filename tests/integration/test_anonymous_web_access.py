"""Serving opted-in projects to a visitor without an account, over the web pages.

The API half of this is pinned in ``test_anonymous_api_access.py``. This module
covers the same rules where a person actually meets them — in a browser — and
the properties that only exist on the web:

- with the instance switch off, an allowlisted page answers a credential-less
  visitor with the login redirect it has always answered with;
- every refusal is a pure function of the URL that was asked for: a missing, a
  private, a not-opted-in and an archived project produce byte-identical
  responses, so the pages cannot be used to enumerate projects;
- a project's opt-in is honoured per permission, issues and wiki separately;
- the rendered HTML carries no control an anonymous visitor could not use, no
  user menu, and no login or email address anywhere;
- private issues and private journals never reach the page, by URL or through
  a listing, a child, a relation or a search result;
- nothing that is not a GET is served, and a GET writes nothing;
- credentials that fail are refused rather than downgraded to anonymous;
- signed-in visitors see exactly the pages they saw before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

import jwt
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import JWT_ALGORITHM
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission
from specivo.services.relation_service import RelationService
from specivo.services.wiki_service import WikiService
from specivo.testing.conftest_base import _test_app
from specivo.web.deps import ANONYMOUS_WEB_ROUTES
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

# A term that appears only in rows this module creates.
TERM = "wombatharbour"

# Logins and emails that must never reach an anonymous page. Deliberately
# nothing like the display names, so a leak cannot pass for a display name.
MEMBER_LOGIN = "anonweb_member"
MEMBER_EMAIL = "anonweb_member@example.com"
MEMBER_DISPLAY = "Robin Larsen"
ADMIN_LOGIN = "anonweb_admin"
ADMIN_EMAIL = "anonweb_admin@example.com"
ADMIN_DISPLAY = "Sam Whitfield"


@dataclass(frozen=True)
class World:
    """Projects and content covering every anonymous-access configuration."""

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
    public_comment: Journal
    private_comment: Journal
    wiki_slug: str


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _project(db: AsyncSession, key: str, *, is_public: bool = True, status: int = 1) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public, status=status)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))
    await db.commit()
    return project


async def _issue(
    db: AsyncSession,
    project: Project,
    lookups: tuple,
    author: User,
    subject: str,
    *,
    description: str = "",
    is_private: bool = False,
    parent: Issue | None = None,
) -> Issue:
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
        # Deliberately unassigned: a private issue with a NULL assignee is the
        # row a visibility check comparing ``assigned_to_id`` against a missing
        # user id would wrongly match.
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
    """Everything built and opted in, with the instance switch still off."""
    admin = AdminUserFactory.build(login=ADMIN_LOGIN, email=ADMIN_EMAIL, display_name=ADMIN_DISPLAY, status="active")
    member = UserFactory.build(login=MEMBER_LOGIN, email=MEMBER_EMAIL, display_name=MEMBER_DISPLAY, status="active")
    db_session.add_all([admin, member])
    await db_session.commit()

    status = StatusFactory.build(name="AnonwebNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="AnonwebBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="AnonwebNormal", is_default=True, position=1)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    lookups = (tracker, status, priority)

    both = await _project(db_session, "ANONWBOTH")
    issues_only = await _project(db_session, "ANONWISS")
    wiki_only = await _project(db_session, "ANONWWIK")
    not_opted_in = await _project(db_session, "ANONWPUB")
    private = await _project(db_session, "ANONWPRIV", is_public=False)
    archived = await _project(db_session, "ANONWARCH", status=9)

    public_issue = await _issue(
        db_session,
        both,
        lookups,
        member,
        f"Public {TERM} issue",
        description=f"A description of the {TERM} problem.",
    )
    private_issue = await _issue(db_session, both, lookups, member, f"Secret {TERM} issue", is_private=True)
    public_child = await _issue(db_session, both, lookups, member, f"Child {TERM}", parent=public_issue)
    private_child = await _issue(
        db_session, both, lookups, member, f"Hidden child {TERM}", is_private=True, parent=public_issue
    )
    await _issue(db_session, issues_only, lookups, member, f"Issues-only {TERM}")

    # A relation from the public issue to the private one: its key must not
    # appear on the page either.
    await RelationService().create(db_session, public_issue, private_issue, "relates")
    await db_session.commit()

    journals = JournalService()
    public_comment = await journals.add_comment(
        db_session, public_issue, member, f"A public note about {TERM} for everyone to read."
    )
    private_comment = await journals.add_comment(
        db_session, public_issue, member, f"A private note about {TERM} that must never be served."
    )
    private_comment.is_private = True
    await db_session.commit()

    wiki = WikiService()
    page, _content = await wiki.create_page(db_session, both.id, "Anon Home", f"Wiki text mentioning {TERM}.", admin)
    await wiki.create_page(db_session, wiki_only.id, "Anon Home", f"Wiki-only {TERM}.", admin)
    await db_session.commit()

    for project, permissions in (
        (both, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]),
        (issues_only, [Permission.VIEW_ISSUES]),
        (wiki_only, [Permission.VIEW_WIKI]),
        (archived, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]),
    ):
        await set_anonymous_permissions(db_session, project, permissions, admin)
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
        public_comment=public_comment,
        private_comment=private_comment,
        wiki_slug=page.slug,
    )


@pytest_asyncio.fixture
async def live(db_session: AsyncSession, world: World) -> World:
    """*world* with the instance switch on — anonymous reading is live."""
    opted_in = [world.both.key, world.issues_only.key, world.wiki_only.key, world.archived.key]
    await set_anonymous_access_enabled(db_session, True, world.admin, confirmed_projects=opted_in)
    await db_session.commit()
    return world


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pages(w: World) -> dict[str, str]:
    """Every allowlisted page, aimed at fully opted-in content."""
    return {
        "projects": "/projects/",
        "project": f"/projects/{w.both.key}/",
        "issues": f"/projects/{w.both.key}/issues/",
        "issue": f"/issue/{w.public_issue.display_key}/",
        "wiki_index": f"/projects/{w.both.key}/wiki/",
        "wiki": f"/projects/{w.both.key}/wiki/{w.wiki_slug}/",
        "search": f"/search/?q={TERM}",
    }


# Every page above except the wiki index, which is a redirect to the home page
# even when it is served.
RENDERED_PAGES = ["projects", "project", "issues", "issue", "wiki", "search"]
ALL_PAGES = [*RENDERED_PAGES, "wiki_index"]


def refusal_location(path: str) -> str:
    """The Location every refusal carries for *path*."""
    return f"/login/?next={quote(path, safe='')}"


def assert_refused(resp, path: str) -> None:
    """Assert *resp* is the uniform refusal for *path* and discloses nothing."""
    assert resp.status_code == 302, f"{path} answered {resp.status_code}"
    assert resp.headers["location"] == refusal_location(path)
    assert resp.text == ""
    assert "www-authenticate" not in {h.lower() for h in resp.headers}


def _signed_in(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


# ---------------------------------------------------------------------------
# The switch is off: the pages behave as they always have
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", ALL_PAGES)
async def test_switch_off_sends_every_allowlisted_page_to_the_login_screen(
    client: AsyncClient, world: World, page: str
) -> None:
    """Even a fully opted-in project is unreachable while the switch is off."""
    path = _pages(world)[page]
    resp = await client.get(path, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login/")
    assert resp.text == ""


async def test_switch_off_leaves_signed_in_visitors_untouched(client: AsyncClient, world: World) -> None:
    resp = await client.get(_pages(world)["issues"], cookies=_signed_in(world.member))
    assert resp.status_code == 200


async def test_a_page_outside_the_allowlist_is_never_served(client: AsyncClient, live: World) -> None:
    """The switch being on changes nothing for a page that did not opt in."""
    for path in (
        "/",
        f"/projects/{live.both.key}/roadmap/",
        f"/projects/{live.both.key}/settings/",
        f"/projects/{live.both.key}/sprints/",
        f"/projects/{live.both.key}/time-entries/",
        f"/projects/{live.both.key}/wiki/pages/",
        f"/projects/{live.both.key}/wiki/{live.wiki_slug}/history/",
        f"/issue/{live.public_issue.display_key}/edit/",
        "/my/notifications/",
        "/admin/",
    ):
        resp = await client.get(path, follow_redirects=False)
        # Whatever each page already answered with, unchanged: a redirect to
        # the login screen. The exact code is not pinned because it is not
        # this work's to choose — handlers differ over whether they pass a
        # status to ``RedirectResponse``, whose default is 307 — and pinning
        # one would make this test fail for a page that is still refusing.
        assert 300 <= resp.status_code < 400, f"{path} answered {resp.status_code}"
        assert resp.headers["location"].startswith("/login/"), path


# ---------------------------------------------------------------------------
# One refusal, whatever the reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["missing", "private", "not_opted_in", "archived"])
async def test_the_refusal_does_not_depend_on_why(client: AsyncClient, live: World, case: str) -> None:
    """A missing, private, not-opted-in and archived project answer alike.

    The refusal is a pure function of the URL that was asked for, so comparing
    responses tells a visitor nothing about which projects exist.
    """
    key = {
        "missing": "NOSUCHKEY",
        "private": live.private.key,
        "not_opted_in": live.not_opted_in.key,
        "archived": live.archived.key,
    }[case]

    for path in (
        f"/projects/{key}/",
        f"/projects/{key}/issues/",
        f"/projects/{key}/wiki/anon-home/",
    ):
        assert_refused(await client.get(path, follow_redirects=False), path)


async def test_a_refusal_reads_the_same_whether_the_switch_is_on_or_off(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    path = f"/projects/{world.private.key}/issues/"
    off = await client.get(path, follow_redirects=False)

    await set_anonymous_access_enabled(
        db_session,
        True,
        world.admin,
        confirmed_projects=[world.both.key, world.issues_only.key, world.wiki_only.key, world.archived.key],
    )
    await db_session.commit()
    on = await client.get(path, follow_redirects=False)

    assert off.status_code == on.status_code == 302
    assert off.headers["location"] == on.headers["location"] == refusal_location(path)
    assert off.text == on.text == ""


async def test_a_private_issue_is_refused_exactly_like_one_that_does_not_exist(
    client: AsyncClient, live: World
) -> None:
    private = f"/issue/{live.private_issue.display_key}/"
    missing = f"/issue/{live.both.key}-99999/"

    assert_refused(await client.get(private, follow_redirects=False), private)
    assert_refused(await client.get(missing, follow_redirects=False), missing)


# ---------------------------------------------------------------------------
# Scope: a project opts in per permission
# ---------------------------------------------------------------------------


async def test_an_issues_only_project_serves_issues_and_refuses_the_wiki(client: AsyncClient, live: World) -> None:
    issues = await client.get(f"/projects/{live.issues_only.key}/issues/")
    assert issues.status_code == 200
    assert f"Issues-only {TERM}" in issues.text

    wiki = f"/projects/{live.issues_only.key}/wiki/anon-home/"
    assert_refused(await client.get(wiki, follow_redirects=False), wiki)


async def test_a_wiki_only_project_serves_the_wiki_and_refuses_issues(client: AsyncClient, live: World) -> None:
    wiki = await client.get(f"/projects/{live.wiki_only.key}/wiki/anon-home/")
    assert wiki.status_code == 200
    assert f"Wiki-only {TERM}" in wiki.text

    issues = f"/projects/{live.wiki_only.key}/issues/"
    assert_refused(await client.get(issues, follow_redirects=False), issues)


async def test_the_project_list_shows_only_opted_in_projects(client: AsyncClient, live: World) -> None:
    resp = await client.get("/projects/")
    assert resp.status_code == 200
    assert live.both.key in resp.text
    assert live.issues_only.key in resp.text
    assert live.wiki_only.key in resp.text
    for hidden in (live.private, live.not_opted_in, live.archived):
        assert hidden.key not in resp.text
        assert hidden.name not in resp.text


async def test_the_wiki_index_leads_an_anonymous_visitor_to_the_home_page(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/projects/{live.both.key}/wiki/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/projects/{live.both.key}/wiki/home/"


# ---------------------------------------------------------------------------
# What the page looks like
# ---------------------------------------------------------------------------

# Markers for the interactive machinery of each page. None of it may reach a
# visitor who could not use it: every entry is either an Alpine component that
# writes, or a link into a signed-in-only area.
FORBIDDEN_ON_EVERY_PAGE = (
    "header-user",
    "user-dropdown",
    "/my/profile/",
    "/my/preferences/",
    "/my/api-keys/",
    "/my/notifications/",
    "/logout/",
    "notif-dropdown",
)

FORBIDDEN_ON_ISSUE_PAGES = (
    "commentForm(",
    "descriptionEditor(",
    "issueSidebar(",
    "issueMetadataPanel(",
    "watcherToggle(",
    "timeLogForm(",
    "relationForm(",
    "replyForm(",
    "resolveThread(",
    "journalReactions(",
    "tagField(",
    "hx-delete",
    "/move/",
    "/edit/",
)

FORBIDDEN_ON_WIKI_PAGES = (
    "wikiAttachments(",
    "tagField(",
    "/edit/",
    "/history/",
    "/wiki/new/",
    "/wiki/trash/",
)


@pytest.mark.parametrize("page", RENDERED_PAGES)
async def test_no_page_offers_an_account_menu_or_a_signed_in_only_link(
    client: AsyncClient, live: World, page: str
) -> None:
    resp = await client.get(_pages(live)[page])
    assert resp.status_code == 200
    for marker in FORBIDDEN_ON_EVERY_PAGE:
        assert marker not in resp.text, f"{page} still renders {marker!r}"


@pytest.mark.parametrize("page", RENDERED_PAGES)
async def test_every_page_offers_a_way_to_sign_in(client: AsyncClient, live: World, page: str) -> None:
    resp = await client.get(_pages(live)[page])
    assert resp.status_code == 200
    assert "/login/?next=" in resp.text


async def test_the_issue_page_is_readable_and_carries_no_controls(client: AsyncClient, live: World) -> None:
    resp = await client.get(_pages(live)["issue"])
    assert resp.status_code == 200

    # The point of the page: it is still worth reading.
    assert live.public_issue.subject in resp.text
    assert f"the {TERM} problem" in resp.text
    assert live.public_comment.notes in resp.text
    assert MEMBER_DISPLAY in resp.text

    for marker in FORBIDDEN_ON_ISSUE_PAGES:
        assert marker not in resp.text, f"the issue page still renders {marker!r}"


async def test_the_issue_list_carries_no_create_or_filter_by_person_controls(client: AsyncClient, live: World) -> None:
    resp = await client.get(_pages(live)["issues"])
    assert resp.status_code == 200

    assert live.public_issue.subject in resp.text
    assert "/issues/new/" not in resp.text
    assert "assigned_to_id" not in resp.text


async def test_the_wiki_page_is_readable_and_carries_no_controls(client: AsyncClient, live: World) -> None:
    resp = await client.get(_pages(live)["wiki"])
    assert resp.status_code == 200

    assert f"Wiki text mentioning {TERM}" in resp.text
    for marker in FORBIDDEN_ON_WIKI_PAGES:
        assert marker not in resp.text, f"the wiki page still renders {marker!r}"


async def test_the_project_page_withholds_what_the_api_withholds(client: AsyncClient, live: World) -> None:
    """The project overview names the project, not its people or its tree."""
    resp = await client.get(_pages(live)["project"])
    assert resp.status_code == 200

    assert live.both.name in resp.text
    # People, membership and the settings/roadmap/time areas are all out.
    assert "/settings/" not in resp.text
    assert "/roadmap/" not in resp.text
    assert "/time-entries/" not in resp.text
    assert "person with access" not in resp.text
    assert "people with access" not in resp.text


@pytest.mark.parametrize("page", RENDERED_PAGES)
async def test_no_login_or_email_appears_on_any_page(client: AsyncClient, live: World, page: str) -> None:
    resp = await client.get(_pages(live)[page])
    assert resp.status_code == 200

    body = resp.text
    for login, email in ((MEMBER_LOGIN, MEMBER_EMAIL), (ADMIN_LOGIN, ADMIN_EMAIL)):
        assert login not in body, f"{page} leaks the login {login!r}"
        assert email not in body, f"{page} leaks the email {email!r}"
    assert "@example.com" not in body


# ---------------------------------------------------------------------------
# Private data never reaches the page
# ---------------------------------------------------------------------------


async def test_a_private_issue_is_absent_from_the_listing(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/projects/{live.both.key}/issues/?status=all")
    assert resp.status_code == 200
    assert live.public_issue.subject in resp.text
    assert live.private_issue.subject not in resp.text
    assert live.private_issue.display_key not in resp.text
    assert live.private_child.subject not in resp.text


async def test_a_private_issue_is_absent_from_children_and_relations(client: AsyncClient, live: World) -> None:
    resp = await client.get(_pages(live)["issue"])
    assert resp.status_code == 200
    assert live.private_child.subject not in resp.text
    assert live.private_child.display_key not in resp.text
    # The relation exists, but naming its target would disclose the key.
    assert live.private_issue.display_key not in resp.text


async def test_a_private_journal_never_renders(client: AsyncClient, live: World) -> None:
    resp = await client.get(_pages(live)["issue"])
    assert resp.status_code == 200
    assert live.public_comment.notes in resp.text
    assert "must never be served" not in resp.text


async def test_private_content_is_absent_from_the_search_page(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/search/?q={TERM}")
    assert resp.status_code == 200
    assert live.private_issue.subject not in resp.text
    assert live.private_child.subject not in resp.text
    assert "must never be served" not in resp.text


async def test_the_search_page_refuses_a_project_filter_it_may_not_read(client: AsyncClient, live: World) -> None:
    """A real-but-unreadable key and a key naming nothing are refused alike."""
    unreadable = f"/search/?q={TERM}&project_key={live.private.key}"
    missing = f"/search/?q={TERM}&project_key=NOSUCHKEY"

    assert_refused(await client.get(unreadable, follow_redirects=False), unreadable)
    assert_refused(await client.get(missing, follow_redirects=False), missing)


async def test_the_search_page_stays_inside_the_anonymous_bounds(client: AsyncClient, live: World) -> None:
    """Semantic modes and long pages are brought back to the API's limits."""
    resp = await client.get(f"/search/?q={TERM}&mode=hybrid&limit=100")
    assert resp.status_code == 200
    # The mode toggle is a control the visitor cannot spend, so it is gone.
    assert "mode-toggle-btn" not in resp.text
    assert "Hybrid" not in resp.text


# ---------------------------------------------------------------------------
# Nothing that is not a GET, and no writes
# ---------------------------------------------------------------------------

_PATH_PARAM = re.compile(r"\{[^}]+\}")

# Everything under these prefixes is a surface of its own with its own
# contract: the JSON API (pinned by the API contract test), agent transport,
# the health probe, and the incoming webhook receivers, which authenticate a
# sending system by signature rather than a person.
_NON_WEB_PREFIXES = ("/api/v1", "/mcp", "/health", "/hooks")


def _mutating_web_routes() -> list[tuple[str, str]]:
    """Every non-GET web route, as ``(method, concrete path)`` pairs."""
    out: list[tuple[str, str]] = []
    for route in _test_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path.startswith(_NON_WEB_PREFIXES):
            continue
        for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
            out.append((method, _PATH_PARAM.sub("1", route.path)))
    return out


def test_there_are_mutating_web_routes_to_check() -> None:
    """Guards the enumeration below against silently checking nothing."""
    assert len(_mutating_web_routes()) >= 5


async def test_no_mutating_web_route_serves_an_anonymous_visitor(client: AsyncClient, live: World) -> None:
    """Anonymous access is read-only, and that is a property of every route."""
    served: list[str] = []
    for method, path in _mutating_web_routes():
        resp = await client.request(method, path, follow_redirects=False)
        # A redirect to the login page, or 422 from FastAPI refusing the
        # request body before the handler ever runs. Neither reaches anything
        # that writes. Both 302 and 307 appear because the handlers predate
        # this work and differ over whether they pass a status code to
        # ``RedirectResponse``, whose default is 307.
        if resp.status_code in (302, 307):
            if not resp.headers["location"].startswith("/login/"):
                served.append(f"{method} {path} -> {resp.headers['location']}")
        elif resp.status_code != 422:
            served.append(f"{method} {path} -> {resp.status_code}")

    assert not served, "these web routes did not refuse an anonymous visitor:\n" + "\n".join(served)


@pytest.mark.parametrize("page", RENDERED_PAGES)
async def test_rendering_a_page_writes_no_rows(
    client: AsyncClient, db_session: AsyncSession, live: World, page: str
) -> None:
    """A GET by a reader must never write — no audit row, no auto-created page."""
    from specivo.models.security_audit import SecurityAuditLog
    from specivo.models.wiki import WikiPage

    async def counts() -> tuple[int, int, int]:
        return (
            (await db_session.execute(select(func.count()).select_from(SecurityAuditLog))).scalar_one(),
            (await db_session.execute(select(func.count()).select_from(WikiPage))).scalar_one(),
            (await db_session.execute(select(func.count()).select_from(Journal))).scalar_one(),
        )

    before = await counts()
    assert (await client.get(_pages(live)[page])).status_code == 200
    assert await counts() == before


async def test_the_wiki_index_creates_no_home_page_for_an_anonymous_visitor(
    client: AsyncClient, db_session: AsyncSession, live: World
) -> None:
    """The project with no wiki home must not gain one because a stranger looked."""
    from specivo.models.wiki import WikiPage

    before = (await db_session.execute(select(func.count()).select_from(WikiPage))).scalar_one()
    await client.get(f"/projects/{live.issues_only.key}/wiki/", follow_redirects=False)
    after = (await db_session.execute(select(func.count()).select_from(WikiPage))).scalar_one()
    assert after == before


# ---------------------------------------------------------------------------
# Credentials are never downgraded to anonymous
# ---------------------------------------------------------------------------


async def test_an_expired_session_cookie_is_refused_rather_than_downgraded(client: AsyncClient, live: World) -> None:
    settings = get_settings()
    expired = jwt.encode(
        {"sub": str(live.member.id), "jti": "anonweb-expired", "exp": utcnow() - timedelta(hours=1)},
        settings.secret_key,
        algorithm=JWT_ALGORITHM,
    )
    path = _pages(live)["issue"]
    resp = await client.get(path, cookies={"access_token": expired}, follow_redirects=False)
    assert_refused(resp, path)


async def test_a_tampered_session_cookie_is_refused_rather_than_downgraded(client: AsyncClient, live: World) -> None:
    path = _pages(live)["issue"]
    resp = await client.get(path, cookies={"access_token": "not.a.jwt"}, follow_redirects=False)
    assert_refused(resp, path)


async def test_a_token_naming_the_anonymous_row_is_refused(
    client: AsyncClient, db_session: AsyncSession, live: World
) -> None:
    """Nobody signs in as the anonymous principal, on the web either."""
    anonymous = (await db_session.execute(select(User).where(User.is_anonymous.is_(True)))).scalar_one()
    token = jwt.encode(
        {"sub": str(anonymous.id), "jti": "anonweb-impersonate", "exp": utcnow() + timedelta(hours=1)},
        get_settings().secret_key,
        algorithm=JWT_ALGORITHM,
    )
    path = _pages(live)["issue"]
    resp = await client.get(path, cookies={"access_token": token}, follow_redirects=False)
    assert_refused(resp, path)


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", RENDERED_PAGES)
async def test_an_anonymous_page_is_uncacheable(client: AsyncClient, live: World, page: str) -> None:
    resp = await client.get(_pages(live)[page])
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    vary = {part.strip().lower() for part in resp.headers["vary"].split(",")}
    assert {"cookie", "authorization"} <= vary
    assert "noindex" in resp.headers["x-robots-tag"]


async def test_a_refusal_is_uncacheable_too(client: AsyncClient, live: World) -> None:
    """Otherwise a cache could tell a refused page from a switched-off one."""
    path = f"/projects/{live.private.key}/issues/"
    resp = await client.get(path, follow_redirects=False)
    assert resp.headers["cache-control"] == "no-store"


async def test_a_signed_in_page_keeps_its_headers(client: AsyncClient, live: World) -> None:
    resp = await client.get(_pages(live)["issues"], cookies=_signed_in(live.member))
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"


# ---------------------------------------------------------------------------
# Signed-in visitors see what they saw before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["member", "admin"])
async def test_a_signed_in_visitor_still_gets_the_full_page(client: AsyncClient, live: World, who: str) -> None:
    user = live.member if who == "member" else live.admin
    resp = await client.get(_pages(live)["issue"], cookies=_signed_in(user))
    assert resp.status_code == 200

    # The controls an anonymous visitor never sees are all back.
    assert "commentForm(" in resp.text
    assert "descriptionEditor(" in resp.text
    assert "watcherToggle(" in resp.text
    assert "header-user" in resp.text
    assert "/logout/" in resp.text


async def test_a_signed_in_non_member_reads_a_public_project_as_before(client: AsyncClient, live: World) -> None:
    """Anonymous opt-in must not change what a signed-in non-member sees."""
    resp = await client.get(_pages(live)["project"], cookies=_signed_in(live.member))
    assert resp.status_code == 200
    assert live.both.name in resp.text
    assert "/roadmap/" in resp.text


async def test_an_admin_still_sees_a_project_that_is_not_opted_in(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/projects/{live.not_opted_in.key}/", cookies=_signed_in(live.admin))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# A toggle applies to the very next request
# ---------------------------------------------------------------------------


async def test_opting_a_project_out_applies_immediately(
    client: AsyncClient, db_session: AsyncSession, live: World
) -> None:
    path = f"/projects/{live.both.key}/issues/"
    assert (await client.get(path)).status_code == 200

    await set_anonymous_permissions(db_session, live.both, [], live.admin)
    await db_session.commit()

    assert_refused(await client.get(path, follow_redirects=False), path)


async def test_turning_the_switch_off_applies_immediately(
    client: AsyncClient, db_session: AsyncSession, live: World
) -> None:
    assert (await client.get("/projects/")).status_code == 200

    await set_anonymous_access_enabled(db_session, False, live.admin)
    await db_session.commit()

    assert_refused(await client.get("/projects/", follow_redirects=False), "/projects/")


# ---------------------------------------------------------------------------
# The allowlist is the whole surface
# ---------------------------------------------------------------------------


def test_the_web_allowlist_is_the_set_that_was_reviewed() -> None:
    """Spelled out so widening it shows up as a change to this file too."""
    assert set(ANONYMOUS_WEB_ROUTES) == {
        "/projects/",
        "/projects/{key}/",
        "/projects/{project_key}/issues/",
        "/issue/{issue_ref}/",
        "/projects/{project_key}/wiki/",
        "/projects/{project_key}/wiki/{slug}/",
        "/search/",
    }
