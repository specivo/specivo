"""Serving opted-in projects to a visitor without an account, over the JSON API.

This is the first thing in Specivo that answers an unauthenticated request
with data, so most of what is pinned here is what anonymous visitors must
*not* get:

- with the instance switch off, nothing changes on any allowlisted route;
- every refusal looks the same, whether the project is missing, private, not
  opted in or archived, so the responses cannot be used to enumerate projects;
- a project's opt-in is honoured per permission: issues-only serves issues and
  refuses the wiki, wiki-only does the reverse;
- private issues and private journals stay invisible everywhere they could
  surface — by key, in listings, in search, in children and in includes;
- bad credentials are refused rather than quietly downgraded to anonymous;
- no login and no email appears in any anonymous response body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import jwt
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

from specivo.core.config import get_settings
from specivo.core.constants import JWT_ALGORITHM
from specivo.core.security import get_reader
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.project import EnabledModule, Project
from specivo.models.search import EmbeddingModel
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.chunking_service import ChunkingService
from specivo.services.embedding_service import EmbeddingService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

# A term that appears only in rows this module creates, so a search for it
# cannot pick up another module's fixtures.
TERM = "quokkaglade"

# The body every anonymous refusal produces. It carries no project key and no
# reason, which is the point: all four refusal causes are indistinguishable.
DENIED = {"errors": [{"code": "unauthorized", "message": "Authentication required", "field": None, "details": None}]}


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
        # row that a visibility check comparing ``assigned_to_id`` against a
        # missing user id would wrongly match.
        assigned_to_id=None,
        subject=subject,
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
    admin = AdminUserFactory.build(login="anonapi_admin", status="active")
    member = UserFactory.build(login="anonapi_member", status="active")
    db_session.add_all([admin, member])
    await db_session.commit()

    status = StatusFactory.build(name="AnonNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="AnonBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="AnonNormal", is_default=True, position=1)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    lookups = (tracker, status, priority)

    both = await _project(db_session, "ANONBOTH")
    issues_only = await _project(db_session, "ANONISS")
    wiki_only = await _project(db_session, "ANONWIKI")
    not_opted_in = await _project(db_session, "ANONPUB")
    private = await _project(db_session, "ANONPRIV", is_public=False)
    archived = await _project(db_session, "ANONARCH", status=9)

    public_issue = await _issue(db_session, both, lookups, member, f"Public {TERM} issue")
    private_issue = await _issue(db_session, both, lookups, member, f"Secret {TERM} issue", is_private=True)
    public_child = await _issue(db_session, both, lookups, member, f"Child {TERM}", parent=public_issue)
    private_child = await _issue(
        db_session, both, lookups, member, f"Hidden child {TERM}", is_private=True, parent=public_issue
    )
    await _issue(db_session, issues_only, lookups, member, f"Issues-only {TERM}")

    model = EmbeddingModel(
        name="anonapi-mock", provider="mock", model_name="mock-1536", dimensions=1536, is_default=True
    )
    db_session.add(model)
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

    for journal in (public_comment, private_comment):
        chunks = ChunkingService().chunk_journal(journal.notes)
        assert chunks, "comment is too short to be indexed"
        await EmbeddingService().embed_source(
            db_session,
            source_type="journal",
            entity_id=journal.id,
            project_id=both.id,
            chunks=chunks,
            model_id=model.id,
        )
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


def _allowlisted(w: World) -> dict[str, str]:
    """The five anonymous-readable routes, aimed at fully opted-in content."""
    return {
        "projects": "/api/v1/projects/",
        "issues": f"/api/v1/projects/{w.both.key}/issues/",
        "issue": f"/api/v1/issues/{w.public_issue.display_key}/",
        "wiki": f"/api/v1/projects/{w.both.key}/wiki/{w.wiki_slug}/",
        "search": f"/api/v1/search/?q={TERM}",
    }


def _signed_in(w: World) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(w.member, get_settings())}"}


# ---------------------------------------------------------------------------
# The switch is off: nothing changed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["projects", "issues", "issue", "wiki", "search"])
async def test_switch_off_refuses_every_allowlisted_route(client: AsyncClient, world: World, route: str) -> None:
    """Even a fully opted-in project is unreachable while the switch is off."""
    resp = await client.get(_allowlisted(world)[route])
    assert resp.status_code == 401
    assert resp.json() == DENIED


async def test_switch_off_leaves_signed_in_callers_untouched(client: AsyncClient, world: World) -> None:
    resp = await client.get(_allowlisted(world)["issues"], headers=_signed_in(world))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# One refusal, whatever the reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["missing", "private", "not_opted_in", "archived"])
async def test_denial_is_uniform_across_reasons(client: AsyncClient, live: World, case: str) -> None:
    """A missing, private, not-opted-in and archived project are indistinguishable."""
    key = {
        "missing": "NOSUCHKEY",
        "private": live.private.key,
        "not_opted_in": live.not_opted_in.key,
        "archived": live.archived.key,
    }[case]

    issues = await client.get(f"/api/v1/projects/{key}/issues/")
    wiki = await client.get(f"/api/v1/projects/{key}/wiki/anon-home/")

    for resp in (issues, wiki):
        assert resp.status_code == 401
        assert resp.json() == DENIED
        # No hint anywhere else in the response either.
        assert key not in resp.text
        assert "www-authenticate" not in {h.lower() for h in resp.headers}


async def test_a_refusal_reads_the_same_whether_the_switch_is_on_or_off(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    """Turning the switch on must not change how a refused project answers."""
    path = f"/api/v1/projects/{world.private.key}/issues/"
    off = await client.get(path)

    await set_anonymous_access_enabled(
        db_session,
        True,
        world.admin,
        confirmed_projects=[world.both.key, world.issues_only.key, world.wiki_only.key, world.archived.key],
    )
    await db_session.commit()
    on = await client.get(path)

    assert off.status_code == on.status_code == 401
    assert off.json() == on.json() == DENIED


async def test_project_listing_shows_only_opted_in_projects(client: AsyncClient, live: World) -> None:
    resp = await client.get("/api/v1/projects/")
    assert resp.status_code == 200
    keys = {item["key"] for item in resp.json()["items"]}
    assert keys == {live.both.key, live.issues_only.key, live.wiki_only.key}
    # The archived project is opted in but not active, and must not be listed.
    assert live.archived.key not in keys
    assert live.private.key not in keys
    assert live.not_opted_in.key not in keys


# ---------------------------------------------------------------------------
# Scope: a project opts in per permission
# ---------------------------------------------------------------------------


async def test_issues_only_project_serves_issues_and_refuses_wiki(client: AsyncClient, live: World) -> None:
    issues = await client.get(f"/api/v1/projects/{live.issues_only.key}/issues/")
    assert issues.status_code == 200
    assert issues.json()["total_count"] == 1

    wiki = await client.get(f"/api/v1/projects/{live.issues_only.key}/wiki/anon-home/")
    assert wiki.status_code == 401
    assert wiki.json() == DENIED


async def test_wiki_only_project_serves_wiki_and_refuses_issues(client: AsyncClient, live: World) -> None:
    wiki = await client.get(f"/api/v1/projects/{live.wiki_only.key}/wiki/anon-home/")
    assert wiki.status_code == 200

    issues = await client.get(f"/api/v1/projects/{live.wiki_only.key}/issues/")
    assert issues.status_code == 401
    assert issues.json() == DENIED


# ---------------------------------------------------------------------------
# Private issues and private journals
# ---------------------------------------------------------------------------


async def test_private_issue_is_invisible_by_key(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/issues/{live.private_issue.display_key}/")
    assert resp.status_code == 401
    assert resp.json() == DENIED


async def test_private_issue_is_absent_from_the_listing(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/projects/{live.both.key}/issues/?status=all")
    assert resp.status_code == 200
    subjects = {item["subject"] for item in resp.json()["items"]}
    assert live.public_issue.subject in subjects
    assert live.private_issue.subject not in subjects
    assert live.private_child.subject not in subjects


async def test_private_issue_is_absent_from_children(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/issues/{live.public_issue.display_key}/?include=children")
    assert resp.status_code == 200
    children = {c["subject"] for c in resp.json()["children"]}
    assert live.public_child.subject in children
    assert live.private_child.subject not in children


async def test_private_issue_is_absent_from_search(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/search/?q={TERM}&scope=issues")
    assert resp.status_code == 200
    assert live.private_issue.subject not in resp.text
    assert live.private_child.subject not in resp.text


async def test_private_journal_is_absent_from_includes(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/issues/{live.public_issue.display_key}/?include=journals")
    assert resp.status_code == 200
    notes = [j["notes"] for j in resp.json()["journals"]]
    assert live.public_comment.notes in notes
    assert live.private_comment.notes not in notes
    assert all(j["is_private"] is False for j in resp.json()["journals"])


async def test_private_journal_is_absent_from_search_snippets(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/search/?q={TERM}&scope=comments")
    assert resp.status_code == 200
    assert "must never be served" not in resp.text


async def test_watchers_and_attachments_are_dropped_from_includes(client: AsyncClient, live: World) -> None:
    """The two includes outside the anonymous scope are ignored, not served."""
    resp = await client.get(
        f"/api/v1/issues/{live.public_issue.display_key}/?include=children,journals,watchers,attachments"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["watchers"] is None
    assert body["attachments"] is None
    assert body["children"] != []
    assert body["journals"] is not None


# ---------------------------------------------------------------------------
# Credentials are never downgraded to anonymous
# ---------------------------------------------------------------------------


async def test_invalid_api_key_is_refused(client: AsyncClient, live: World) -> None:
    resp = await client.get("/api/v1/projects/", headers={"Authorization": "Bearer spv_not_a_real_key_at_all_padding"})
    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] != "unauthorized" or resp.json() != DENIED


async def test_revoked_api_key_is_refused(client: AsyncClient, db_session: AsyncSession, live: World) -> None:
    key, raw = await ApiKeyService().create_key(session=db_session, user_id=live.member.id, name="anon-revoked")
    key.is_active = False
    await db_session.commit()

    resp = await client.get("/api/v1/projects/", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "api_key_inactive"


async def test_malformed_bearer_token_is_refused(client: AsyncClient, live: World) -> None:
    resp = await client.get("/api/v1/projects/", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "auth_token_invalid"


async def test_expired_bearer_token_is_refused(client: AsyncClient, live: World) -> None:
    settings = get_settings()
    expired = jwt.encode(
        {"sub": str(live.member.id), "jti": "anonapi-expired", "exp": utcnow() - timedelta(hours=1)},
        settings.secret_key,
        algorithm=JWT_ALGORITHM,
    )
    resp = await client.get("/api/v1/projects/", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "auth_token_expired"


# ---------------------------------------------------------------------------
# Search limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["mode=semantic", "mode=hybrid", "limit=26", "offset=501"])
async def test_search_restrictions_are_enforced(client: AsyncClient, live: World, query: str) -> None:
    resp = await client.get(f"/api/v1/search/?q={TERM}&{query}")
    assert resp.status_code == 401
    assert resp.json() == DENIED


@pytest.mark.parametrize("query", ["", "mode=keyword", "limit=25", "offset=500"])
async def test_search_within_the_anonymous_bounds_still_works(client: AsyncClient, live: World, query: str) -> None:
    resp = await client.get(f"/api/v1/search/?q={TERM}&{query}")
    assert resp.status_code == 200


async def test_search_project_filter_hides_whether_a_project_exists(client: AsyncClient, live: World) -> None:
    """A real-but-unreadable key and a key naming nothing are refused alike."""
    unreadable = await client.get(f"/api/v1/search/?q={TERM}&project_key={live.private.key}")
    missing = await client.get(f"/api/v1/search/?q={TERM}&project_key=NOSUCHKEY")

    assert unreadable.status_code == missing.status_code == 401
    assert unreadable.json() == missing.json() == DENIED


async def test_signed_in_search_modes_are_unaffected(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/api/v1/search/?q={TERM}&limit=100", headers=_signed_in(live))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["projects", "issues", "issue", "wiki", "search"])
async def test_anonymous_responses_are_uncacheable(client: AsyncClient, live: World, route: str) -> None:
    resp = await client.get(_allowlisted(live)[route])
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    vary = {part.strip().lower() for part in resp.headers["vary"].split(",")}
    assert {"cookie", "authorization"} <= vary
    assert "noindex" in resp.headers["x-robots-tag"]


async def test_signed_in_responses_keep_their_headers(client: AsyncClient, live: World) -> None:
    resp = await client.get(_allowlisted(live)["issues"], headers=_signed_in(live))
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"


async def test_a_cookieless_get_is_unaffected_by_csrf(client: AsyncClient, live: World) -> None:
    """CSRF only guards mutating requests that carry an auth cookie."""
    resp = await client.get(_allowlisted(live)["issues"])
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Leak control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["projects", "issues", "issue", "wiki", "search"])
async def test_no_login_or_email_appears_anywhere(client: AsyncClient, live: World, route: str) -> None:
    url = _allowlisted(live)[route]
    if route == "issue":
        url += "?include=children,journals"
    resp = await client.get(url)
    assert resp.status_code == 200

    body = resp.text
    for user in (live.member, live.admin):
        assert user.login not in body
        assert user.email not in body
    assert "@example.com" not in body


async def test_an_author_is_named_but_only_by_display_name(client: AsyncClient, live: World) -> None:
    """The positive half: people are still identified, just not by login."""
    resp = await client.get(f"/api/v1/issues/{live.public_issue.display_key}/?include=journals")
    assert resp.status_code == 200

    body = resp.json()
    assert body["author"]["name"] == live.member.display_name
    assert "login" not in body["author"]
    assert body["journals"][0]["user"]["name"] == live.member.display_name


async def test_project_listing_withholds_the_project_tree(client: AsyncClient, live: World) -> None:
    resp = await client.get("/api/v1/projects/")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["path"] is None
        assert item["parent_key"] is None
        assert item["parent_id"] is None
        assert item["inherit_members"] is None
        assert item["issue_sequence"] is None
        assert item["computed_metadata"] is None


async def test_signed_in_project_listing_still_carries_the_tree(client: AsyncClient, live: World) -> None:
    resp = await client.get("/api/v1/projects/", headers=_signed_in(live))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "the member should see public projects"
    assert all(item["path"] for item in items)
    assert all(item["issue_sequence"] is not None for item in items)


# ---------------------------------------------------------------------------
# A toggle applies to the very next request
# ---------------------------------------------------------------------------


async def test_opting_a_project_out_applies_immediately(
    client: AsyncClient, db_session: AsyncSession, live: World
) -> None:
    assert (await client.get(f"/api/v1/projects/{live.both.key}/issues/")).status_code == 200

    await set_anonymous_permissions(db_session, live.both, [], live.admin)
    await db_session.commit()

    resp = await client.get(f"/api/v1/projects/{live.both.key}/issues/")
    assert resp.status_code == 401
    assert resp.json() == DENIED


async def test_turning_the_switch_off_applies_immediately(
    client: AsyncClient, db_session: AsyncSession, live: World
) -> None:
    assert (await client.get("/api/v1/projects/")).status_code == 200

    await set_anonymous_access_enabled(db_session, False, live.admin)
    await db_session.commit()

    resp = await client.get("/api/v1/projects/")
    assert resp.status_code == 401
    assert resp.json() == DENIED


# ---------------------------------------------------------------------------
# Anonymous requests are read-only, and write nothing
# ---------------------------------------------------------------------------


def _bare_request(path: str = "/api/v1/projects/") -> StarletteRequest:
    """A GET carrying no credentials of any kind."""
    return StarletteRequest(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
        }
    )


async def test_anonymous_requests_run_in_a_read_only_transaction(db_session: AsyncSession, live: World) -> None:
    """``SET LOCAL`` is scoped to the savepoint, so it never escapes the request."""
    reader = get_reader(_bare_request(), StarletteResponse(), db_session)

    user = await reader.__anext__()
    assert user.is_anonymous
    assert (await db_session.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"

    await reader.aclose()
    assert (await db_session.execute(text("SHOW transaction_read_only"))).scalar_one() == "off"


async def test_a_signed_in_request_is_not_made_read_only(db_session: AsyncSession, live: World) -> None:
    request = _bare_request()
    request.scope["headers"] = [
        (b"authorization", f"Bearer {_make_access_token(live.member, get_settings())}".encode())
    ]

    reader = get_reader(request, StarletteResponse(), db_session)
    user = await reader.__anext__()
    assert not user.is_anonymous
    assert (await db_session.execute(text("SHOW transaction_read_only"))).scalar_one() == "off"
    await reader.aclose()


@pytest.mark.parametrize("route", ["projects", "issues", "issue", "wiki", "search"])
async def test_anonymous_reads_write_no_audit_rows(
    client: AsyncClient, db_session: AsyncSession, live: World, route: str
) -> None:
    """A crawler must not be able to fill the audit log one row per request."""
    before = (await db_session.execute(select(func.count()).select_from(SecurityAuditLog))).scalar_one()

    resp = await client.get(_allowlisted(live)[route])
    assert resp.status_code == 200

    after = (await db_session.execute(select(func.count()).select_from(SecurityAuditLog))).scalar_one()
    assert after == before
