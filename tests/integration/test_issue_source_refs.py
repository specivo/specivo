"""The number an imported issue had: shown, searched and resolved.

Readers are non-admin throughout, since admins see everything. ``hidden`` is a
private issue in a project where the reader's role has ``issues_visibility="own"``,
and ``elsewhere`` is an issue in a private project the reader is not a member of;
both must behave exactly like a number that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.issue_source_ref import IssueSourceRef
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.search import SearchResult
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.api_key_scope import ProjectScope, bound_scope
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import Permission
from specivo.services.search_service import SearchService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MODES = ("keyword", "semantic", "hybrid")
SIDEBAR = Path("specivo/templates/themes/default/pages/issues/_sidebar.html")


@dataclass
class World:
    admin: User
    reader: User
    tracker: Tracker
    status: IssueStatus
    priority: IssuePriority
    open: Project  # the reader is a member, sees everything
    own: Project  # the reader is a member, sees only their own issues
    closed: Project  # private, and the reader is not a member
    public: Project  # opted in to anonymous reading
    wiki_only: Project  # public and opted in to the wiki alone
    visible: Issue
    hidden: Issue
    elsewhere: Issue
    public_issue: Issue
    public_private_issue: Issue
    wiki_only_issue: Issue


async def _project(db: AsyncSession, key: str, *, is_public: bool = False) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=is_public)
    db.add(project)
    await db.flush()
    db.add_all([EnabledModule(project_id=project.id, name=name) for name in ("issue_tracking", "wiki")])
    await db.flush()
    return project


async def _grant(db: AsyncSession, user: User, project: Project, visibility: str) -> None:
    role = Role(
        name=f"Srcref-{project.key}",
        permissions=["view_issues", "add_issues", "edit_issues", "move_issues"],
        builtin=0,
        issues_visibility=visibility,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _new_issue(db: AsyncSession, w: World, project: Project, subject: str, *, is_private: bool = False) -> Issue:
    return await _issue(db, (w.tracker, w.status, w.priority), w.admin, project, subject, is_private=is_private)


async def _issue(
    db: AsyncSession,
    lookups: tuple,
    author: User,
    project: Project,
    subject: str,
    *,
    is_private: bool = False,
) -> Issue:
    tracker, status, priority = lookups
    seq = await db.scalar(
        select(func.coalesce(func.max(Issue.sequence_number), 0)).where(Issue.project_id == project.id)
    )
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
        description="",
        is_private=is_private,
    )
    db.add(issue)
    await db.flush()
    return issue


def _ref(issue: Issue, ref: str, label: str | None = None, *, system: str = "rt", primary: bool = True):
    return IssueSourceRef(
        issue_id=issue.id,
        source_system=system,
        ref=ref,
        label=label or f"{system.upper()} #{ref}",
        is_primary=primary,
    )


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    admin = AdminUserFactory.build(login="srcref_admin", status="active")
    reader = UserFactory.build(login="srcref_reader", status="active")
    db.add_all([admin, reader])
    await db.flush()

    status = StatusFactory.build(name="Srcref New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Srcref Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Srcref Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    await db.flush()
    lookups = (tracker, status, priority)

    open_project = await _project(db, "SROPEN")
    own = await _project(db, "SROWN")
    closed = await _project(db, "SRCLOSED")
    public = await _project(db, "SRPUB", is_public=True)
    wiki_only = await _project(db, "SRWIKI", is_public=True)
    await _grant(db, reader, open_project, "all")
    await _grant(db, reader, own, "own")

    visible = await _issue(db, lookups, admin, open_project, "Printer jams on tray two")
    hidden = await _issue(db, lookups, admin, own, "A private matter", is_private=True)
    elsewhere = await _issue(db, lookups, admin, closed, "In a closed project")
    public_issue = await _issue(db, lookups, admin, public, "Open to everyone")
    public_private = await _issue(db, lookups, admin, public, "Private in a public project", is_private=True)
    wiki_only_issue = await _issue(db, lookups, admin, wiki_only, "In a wiki-only project")
    db.add_all(
        [
            _ref(visible, "123"),
            _ref(visible, "PROJ-7", "PROJ-7", system="jira", primary=False),
            _ref(hidden, "500"),
            _ref(hidden, "HID-5", "HID-5", system="jira"),
            _ref(elsewhere, "501"),
            _ref(elsewhere, "CLS-5", "CLS-5", system="jira"),
            _ref(public_issue, "700"),
            _ref(public_private, "701"),
            _ref(wiki_only_issue, "702"),
        ]
    )

    await set_anonymous_permissions(db, public, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_permissions(db, wiki_only, [Permission.VIEW_WIKI], admin)
    await db.commit()
    return World(
        admin=admin,
        reader=reader,
        tracker=tracker,
        status=status,
        priority=priority,
        open=open_project,
        own=own,
        closed=closed,
        public=public,
        wiki_only=wiki_only,
        visible=visible,
        hidden=hidden,
        elsewhere=elsewhere,
        public_issue=public_issue,
        public_private_issue=public_private,
        wiki_only_issue=wiki_only_issue,
    )


@pytest_asyncio.fixture
async def live(db_session: AsyncSession, world: World) -> World:
    """*world* with anonymous reading switched on."""
    await set_anonymous_access_enabled(
        db_session, True, world.admin, confirmed_projects=[world.public.key, world.wiki_only.key]
    )
    await db_session.commit()
    return world


def _as(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _search(db: AsyncSession, mode: str, query: str, user: User) -> list[SearchResult]:
    svc = SearchService()
    if mode == "keyword":
        results, _, _ = await svc.search(db, query, user=user)
    elif mode == "semantic":
        results, _ = await svc.semantic_search(db, query, user=user)
    else:
        results, _, _ = await svc.hybrid_search(db, query, user=user)
    return results


def _titles(results: list[SearchResult]) -> list[str]:
    return [r.title for r in results if r.result_type == "issue"]


def _refusal(path: str) -> str:
    return f"/login/?next={quote(path, safe='')}"


# ---------------------------------------------------------------------------
# Showing the number
# ---------------------------------------------------------------------------


async def test_imported_issue_shows_source_label_on_page(client: AsyncClient, world: World) -> None:
    resp = await client.get(f"/issue/{world.visible.display_key}/", cookies=_as(world.reader))
    assert resp.status_code == 200
    assert "Imported from: RT #123" in resp.text
    assert 'data-testid="issue-imported-from"' in resp.text


async def test_the_label_shows_under_another_language(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    """The prefix has no Thai catalogue entry yet and falls back; the label is shown as stored."""
    world.reader.language = "th"
    await db_session.commit()
    resp = await client.get(f"/issue/{world.visible.display_key}/", cookies=_as(world.reader))
    assert resp.status_code == 200
    assert 'lang="th"' in resp.text
    assert "RT #123" in resp.text


def test_the_page_row_goes_through_gettext() -> None:
    assert "{% trans label=imported_from.label %}Imported from: %(label)s{% endtrans %}" in SIDEBAR.read_text()


def test_a_long_label_can_wrap_at_phone_width() -> None:
    """An unbroken key must not widen the sidebar: the row may break anywhere."""
    row = next(line for line in SIDEBAR.read_text().splitlines() if "issue-imported-from" in line)
    assert "overflow-wrap: anywhere" in row


async def test_the_label_is_escaped_on_the_page(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    db_session.add(_ref(world.public_issue, "x", "<script>alert(1)</script>"))
    await db_session.commit()
    resp = await client.get(f"/issue/{world.public_issue.display_key}/", cookies=_as(world.admin))
    assert "<script>alert(1)</script>" not in resp.text


async def test_issue_without_source_ref_shows_nothing(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    plain = await _new_issue(db_session, world, world.open, "Filed here")
    await db_session.commit()
    resp = await client.get(f"/issue/{plain.display_key}/", cookies=_as(world.reader))
    assert resp.status_code == 200
    assert "issue-imported-from" not in resp.text
    api = await client.get(f"/api/v1/issues/{plain.display_key}/", cookies=_as(world.reader))
    assert api.json()["imported_from"] is None


async def test_anonymous_visitor_sees_the_number_of_a_public_issue(client: AsyncClient, live: World) -> None:
    resp = await client.get(f"/issue/{live.public_issue.display_key}/")
    assert resp.status_code == 200
    assert "Imported from: RT #700" in resp.text


async def test_detail_api_returns_imported_from(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    db_session.add_all([_ref(world.visible, "120", primary=False), _ref(world.visible, "121", primary=False)])
    await db_session.commit()
    resp = await client.get(f"/api/v1/issues/{world.visible.display_key}/", cookies=_as(world.reader))
    assert resp.status_code == 200
    data = resp.json()["imported_from"]
    assert data == {"system": "rt", "ref": "123", "label": "RT #123", "aliases": ["PROJ-7", "120", "121"]}


async def test_list_endpoint_has_no_lazy_load_error(client: AsyncClient, world: World) -> None:
    resp = await client.get(f"/api/v1/projects/{world.open.key}/issues/", cookies=_as(world.reader))
    assert resp.status_code == 200
    assert resp.json()["items"]
    assert "imported_from" not in resp.json()["items"][0]


async def test_deleting_an_issue_removes_its_refs(db_session: AsyncSession, world: World) -> None:
    issue_id = world.visible.id
    await db_session.execute(text("DELETE FROM issues WHERE id = :id"), {"id": issue_id})
    count = await db_session.scalar(
        select(func.count()).select_from(IssueSourceRef).where(IssueSourceRef.issue_id == issue_id)
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_search_finds_source_key(db_session: AsyncSession, world: World, mode: str) -> None:
    for query in ("PROJ-7", "proj-7"):
        assert _titles(await _search(db_session, mode, query, world.reader))[0] == world.visible.display_key


@pytest.mark.parametrize("mode", MODES)
async def test_search_finds_label_with_hash(db_session: AsyncSession, world: World, mode: str) -> None:
    for query in ("RT #123", "rt #123"):
        assert _titles(await _search(db_session, mode, query, world.reader))[0] == world.visible.display_key


@pytest.mark.parametrize("mode", MODES)
async def test_search_finds_bare_hash_number(db_session: AsyncSession, world: World, mode: str) -> None:
    assert _titles(await _search(db_session, mode, "#123", world.reader))[0] == world.visible.display_key


@pytest.mark.parametrize("mode", MODES)
async def test_search_finds_a_word_of_a_multi_word_label(db_session: AsyncSession, world: World, mode: str) -> None:
    """The word typed before the number is the label's last word, as often as its first."""
    db_session.add(_ref(world.visible, "900", "Request Tracker #900", system="rt"))
    await db_session.commit()
    for query in ("Request Tracker #900", "Request #900"):
        assert world.visible.display_key in _titles(await _search(db_session, mode, query, world.reader))


@pytest.mark.parametrize("mode", MODES)
async def test_search_does_not_pin_a_bare_number(db_session: AsyncSession, world: World, mode: str) -> None:
    assert world.visible.display_key not in _titles(await _search(db_session, mode, "123", world.reader))


@pytest.mark.parametrize("mode", MODES)
async def test_an_ordinary_word_before_the_number_still_finds_it(
    db_session: AsyncSession, world: World, mode: str
) -> None:
    for query in ("bug #123", "see ticket #123"):
        assert _titles(await _search(db_session, mode, query, world.reader))[0] == world.visible.display_key


@pytest.mark.parametrize("mode", MODES)
async def test_a_word_naming_a_system_prefers_that_system(db_session: AsyncSession, world: World, mode: str) -> None:
    tracker = await db_session.get(Tracker, world.visible.tracker_id)
    status = await db_session.get(IssueStatus, world.visible.status_id)
    priority = await db_session.get(IssuePriority, world.visible.priority_id)
    project = await db_session.get(Project, world.visible.project_id)
    other = await _issue(db_session, (tracker, status, priority), world.admin, project, "Scanner fails at boot")
    db_session.add(_ref(other, "123", system="jira"))
    await db_session.commit()

    jira = _titles(await _search(db_session, mode, "jira #123", world.reader))
    assert jira[0] == other.display_key
    assert world.visible.display_key not in jira
    rt = _titles(await _search(db_session, mode, "RT #123", world.reader))
    assert rt[0] == world.visible.display_key
    assert other.display_key not in rt


@pytest.mark.parametrize("mode", MODES)
async def test_a_hidden_issue_does_not_change_how_a_word_is_read(
    db_session: AsyncSession, world: World, mode: str
) -> None:
    """If a hidden match could suppress the visible one, the results would reveal it."""
    before = _titles(await _search(db_session, mode, "secret #123", world.reader))
    db_session.add(_ref(world.hidden, "123", "Secret #123", system="secret"))
    await db_session.commit()

    after = _titles(await _search(db_session, mode, "secret #123", world.reader))

    assert after == before
    assert after[0] == world.visible.display_key


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("query", ["repo#123", "&#123;", "#123abc"])
async def test_a_hash_glued_to_other_text_does_not_pin(
    db_session: AsyncSession, world: World, mode: str, query: str
) -> None:
    assert world.visible.display_key not in _titles(await _search(db_session, mode, query, world.reader))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("query", "attr"), [("#500", "hidden"), ("HID-5", "hidden"), ("#501", "elsewhere"), ("CLS-5", "elsewhere")]
)
async def test_search_does_not_return_private_issue_by_source_ref(
    db_session: AsyncSession, world: World, mode: str, query: str, attr: str
) -> None:
    assert getattr(world, attr).display_key not in _titles(await _search(db_session, mode, query, world.reader))


@pytest.mark.parametrize("mode", MODES)
async def test_the_creator_of_an_own_visibility_issue_finds_it(
    db_session: AsyncSession, world: World, mode: str
) -> None:
    """The private case above fails for visibility, not because the ref is unreachable."""
    assert _titles(await _search(db_session, mode, "#500", world.admin))[0] == world.hidden.display_key


@pytest.mark.parametrize("mode", MODES)
async def test_search_respects_key_limit_for_source_ref(db_session: AsyncSession, world: World, mode: str) -> None:
    with bound_scope(ProjectScope(frozenset({world.own.id}))):
        assert world.visible.display_key not in _titles(await _search(db_session, mode, "#123", world.reader))
        assert world.visible.display_key not in _titles(await _search(db_session, mode, "PROJ-7", world.admin))
    with bound_scope(ProjectScope(frozenset({world.open.id}))):
        assert _titles(await _search(db_session, mode, "#123", world.reader))[0] == world.visible.display_key


async def test_search_api_respects_a_limited_key(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    _, raw = await ApiKeyService().create_key(
        session=db_session, user_id=world.reader.id, name="srcref-limited", scopes={"projects": [world.own.key]}
    )
    await db_session.commit()
    resp = await client.get("/api/v1/search/?q=%23123&mode=keyword", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    assert world.visible.display_key not in [item["title"] for item in resp.json()["items"]]


@pytest.mark.parametrize("mode", MODES)
async def test_native_issue_is_pinned_before_a_foreign_key(db_session: AsyncSession, world: World, mode: str) -> None:
    """A project whose key equals the foreign prefix: both are pinned, native first."""
    native_project = await _project(db_session, "PROJ")
    await _grant(db_session, world.reader, native_project, "all")
    native = IssueFactory.build(
        project_id=native_project.id,
        project_key="PROJ",
        sequence_number=7,
        tracker_id=world.tracker.id,
        status_id=world.status.id,
        priority_id=world.priority.id,
        author_id=world.admin.id,
        subject="The native one",
    )
    db_session.add(native)
    await db_session.commit()
    assert _titles(await _search(db_session, mode, "PROJ-7", world.reader))[:2] == [
        "PROJ-7",
        world.visible.display_key,
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_a_number_too_large_for_an_issue_still_matches_a_source_key(
    db_session: AsyncSession, world: World, mode: str
) -> None:
    db_session.add(_ref(world.visible, "BIG-99999999999", "BIG-99999999999", system="jira"))
    await db_session.commit()
    assert world.visible.display_key in _titles(await _search(db_session, mode, "BIG-99999999999", world.reader))


@pytest.mark.parametrize("mode", MODES)
async def test_issue_key_search_still_pins_first(db_session: AsyncSession, world: World, mode: str) -> None:
    assert _titles(await _search(db_session, mode, world.visible.display_key, world.reader))[0] == (
        world.visible.display_key
    )


# ---------------------------------------------------------------------------
# /imported/{system}/{ref}/
# ---------------------------------------------------------------------------


async def test_redirect_one_match(client: AsyncClient, world: World) -> None:
    resp = await client.get("/imported/rt/123/", cookies=_as(world.reader))
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/issue/{world.visible.display_key}/"


async def test_redirect_ignores_case(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    db_session.add(_ref(world.visible, "ÉTÉ-1", "ÉTÉ-1", system="other"))
    await db_session.commit()
    for path in ("/imported/RT/123/", "/imported/jira/proj-7/", "/imported/other/%C3%A9t%C3%A9-1/"):
        resp = await client.get(path, cookies=_as(world.reader))
        assert resp.status_code == 302, path
        assert resp.headers["location"] == f"/issue/{world.visible.display_key}/"


async def test_redirect_without_a_trailing_slash_gets_there(client: AsyncClient, world: World) -> None:
    client.cookies.update(_as(world.reader))
    resp = await client.get("/imported/rt/123", follow_redirects=True)
    assert str(resp.url).endswith(f"/issue/{world.visible.display_key}/")


async def test_redirect_several_matches_goes_to_search(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    """Two installations of a tracker can both have a ticket 123."""
    twin = await _new_issue(db_session, world, world.open, "The other 123")
    db_session.add(_ref(twin, "123"))
    await db_session.commit()
    resp = await client.get("/imported/rt/123/", cookies=_as(world.reader))
    assert resp.status_code == 302
    assert resp.headers["location"] == "/search/?q=rt+%23123"


async def test_several_matches_of_which_one_is_visible_goes_to_it(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    """The hidden twin neither changes the answer nor shows through it."""
    db_session.add(_ref(world.elsewhere, "123"))
    await db_session.commit()
    resp = await client.get("/imported/rt/123/", cookies=_as(world.reader))
    assert resp.headers["location"] == f"/issue/{world.visible.display_key}/"


async def test_redirect_unknown_is_404_for_signed_in(client: AsyncClient, world: World) -> None:
    resp = await client.get("/imported/rt/999999/", cookies=_as(world.reader))
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/imported/rt/500/", "/imported/rt/501/", "/imported/jira/HID-5/"])
async def test_redirect_private_issue_looks_like_unknown_for_signed_in_user(
    client: AsyncClient, world: World, path: str
) -> None:
    unknown = await client.get("/imported/rt/999999/", cookies=_as(world.reader))
    hidden = await client.get(path, cookies=_as(world.reader))
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.text == unknown.text


@pytest.mark.parametrize(
    "path",
    [
        "/imported/rt/%00/",
        "/imported/%00/1/",
        f"/imported/rt/{'9' * 300}/",
        f"/imported/{'r' * 40}/1/",
    ],
)
async def test_a_value_no_column_can_hold_is_an_ordinary_404(client: AsyncClient, world: World, path: str) -> None:
    resp = await client.get(path, cookies=_as(world.reader))
    assert resp.status_code == 404


async def test_redirect_anonymous_gets_same_refusal_for_private_and_unknown(client: AsyncClient, live: World) -> None:
    for path in (
        "/imported/rt/701/",  # a private issue in a project they can read
        "/imported/rt/501/",  # a private project
        "/imported/rt/702/",  # a public project opted in to the wiki alone
        "/imported/rt/999999/",  # nothing at all
    ):
        resp = await client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["location"] == _refusal(path)
        assert resp.text == ""


async def test_redirect_anonymous_refuses_a_value_no_column_can_hold(client: AsyncClient, live: World) -> None:
    resp = await client.get("/imported/rt/%00/")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login/")


async def test_redirect_anonymous_refused_when_the_switch_is_off(client: AsyncClient, world: World) -> None:
    resp = await client.get("/imported/rt/700/")
    assert resp.status_code == 302
    assert resp.headers["location"] == _refusal("/imported/rt/700/")


async def test_redirect_anonymous_on_public_issue_redirects(client: AsyncClient, live: World) -> None:
    resp = await client.get("/imported/rt/700/")
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/issue/{live.public_issue.display_key}/"


async def test_redirect_respects_a_limited_key(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    _, inside = await ApiKeyService().create_key(
        session=db_session, user_id=world.reader.id, name="srcref-in", scopes={"projects": [world.open.key]}
    )
    _, outside = await ApiKeyService().create_key(
        session=db_session, user_id=world.reader.id, name="srcref-out", scopes={"projects": [world.own.key]}
    )
    await db_session.commit()
    resp = await client.get("/imported/rt/123/", headers={"Authorization": f"Bearer {inside}"})
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/issue/{world.visible.display_key}/"
    resp = await client.get("/imported/rt/123/", headers={"Authorization": f"Bearer {outside}"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/imported/rt/%2F%2Fevil.example/", "/imported/rt//evil.example/", "/imported/rt/https:%2F%2Fevil.example/"],
)
async def test_redirect_is_always_a_local_path(
    client: AsyncClient, db_session: AsyncSession, world: World, path: str
) -> None:
    for ref in ("//evil.example", "https://evil.example", "/evil.example"):
        db_session.add(_ref(world.visible, ref, ref))
    await db_session.commit()
    resp = await client.get(path, cookies=_as(world.reader))
    # Each path names a seeded ref, so each must resolve, and only to this site's issue page.
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == f"/issue/{world.visible.display_key}/"


async def test_ref_with_slash_resolves(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    db_session.add(_ref(world.visible, "team/42", "team/42", system="other"))
    await db_session.commit()
    resp = await client.get("/imported/other/team/42/", cookies=_as(world.reader))
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/issue/{world.visible.display_key}/"


async def test_several_matches_with_a_key_search_by_the_key(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    twin = await _new_issue(db_session, world, world.open, "The other PROJ-7")
    db_session.add(_ref(twin, "PROJ-7", "PROJ-7", system="jira"))
    await db_session.commit()
    resp = await client.get("/imported/jira/PROJ-7/", cookies=_as(world.reader))
    assert resp.headers["location"] == "/search/?q=PROJ-7"


async def _move(client: AsyncClient, db: AsyncSession, world: World, issue_ref: str) -> str:
    """Move to a project of its own: numbering there starts from the project's counter, untouched by the factories."""
    target = await _project(db, "SRMOVE")
    await db.commit()
    resp = await client.post(
        f"/api/v1/issues/{issue_ref}/move/",
        json={"target_project_key": target.key},
        headers=_bearer(world.admin),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


async def test_moved_issue_redirects_to_its_new_key(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    new_key = await _move(client, db_session, world, world.visible.display_key)
    assert new_key.startswith("SRMOVE-")
    resp = await client.get("/imported/rt/123/", cookies=_as(world.admin))
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/issue/{new_key}/"


async def test_retired_ref_redirect_still_works(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    old_key = world.visible.display_key
    new_key = await _move(client, db_session, world, old_key)
    resp = await client.get(f"/issue/{old_key}/", cookies=_as(world.admin))
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/issue/{new_key}/"


async def test_new_issue_does_not_get_a_source_ref(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    empty = await _project(db_session, "SRNEW")
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/projects/{empty.key}/issues/",
        json={"project_key": empty.key, "subject": "Filed here", "tracker_id": world.tracker.id},
        headers=_bearer(world.admin),
    )
    assert resp.status_code == 201, resp.text
    count = await db_session.scalar(
        select(func.count()).select_from(IssueSourceRef).where(IssueSourceRef.issue_id == resp.json()["id"])
    )
    assert count == 0
