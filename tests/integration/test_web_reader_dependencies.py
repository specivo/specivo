"""How project and issue web pages refuse a reader: signed out, unknown, unreachable, not permitted.

Characterization tests for the pages that resolve their project or issue
through the shared reader dependencies in ``specivo.web.deps``. They pin the
status codes the pages answered before those dependencies existed:

- a visitor without a session is sent to log in (``login_redirect``: back to
  the page after a GET, to the bare login page after a POST);
- a key or reference that names nothing is a 404;
- a private project the reader cannot reach is a 404 as well, never a 403,
  and so is its retired key, so keys cannot be enumerated (ADR-0004);
- a reachable project where the reader lacks the page's permission is a 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, ProjectKeyAlias
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    outsider: User
    viewer: User


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="RD New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="RD Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="RD Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()

    public = ProjectFactory.build(key="RDOPEN", identifier="rd-open", is_public=True)
    secret = ProjectFactory.build(key="RDSEC", identifier="rd-secret", is_public=False)
    db.add_all([public, secret])
    await db.flush()
    for project in (public, secret):
        db.add_all([EnabledModule(project_id=project.id, name=m) for m in ("issue_tracking", "time_tracking", "wiki")])

    outsider = UserFactory.build(login="rd_outsider", status="active")
    viewer = UserFactory.build(login="rd_viewer", status="active")
    db.add_all([outsider, viewer])
    await db.flush()
    # The private project's retired key.
    db.add(ProjectKeyAlias(project_id=secret.id, old_key="RDOLD", renamed_by_id=viewer.id))

    # The viewer may read issues in the public project, and nothing more.
    role = Role(name="RD Viewer", permissions=["view_issues"], builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=viewer.id, project_id=public.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))

    for project in (public, secret):
        db.add(
            IssueFactory.build(
                project_id=project.id,
                project_key=project.key,
                sequence_number=1,
                tracker_id=tracker.id,
                status_id=status.id,
                priority_id=priority.id,
                author_id=viewer.id,
                subject=f"{project.key} issue",
            )
        )
    await db.commit()
    clear_role_cache(db)
    yield World(outsider, viewer)
    clear_role_cache(db)


def _cookies(user: User | None) -> dict[str, str]:
    return {} if user is None else {"access_token": _make_access_token(user, get_settings())}


async def _get(client: AsyncClient, user: User | None, url: str) -> Response:
    return await client.get(url, cookies=_cookies(user), headers={"accept": "text/html"}, follow_redirects=False)


async def _csrf(client: AsyncClient, user: User | None) -> str:
    """The CSRF token the middleware hands this reader (bound to their session)."""
    resp = await client.get("/login/", cookies=_cookies(user), follow_redirects=False)
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError("no csrf_token cookie")


async def _post(client: AsyncClient, user: User | None, url: str, data: dict[str, str] | None = None) -> Response:
    csrf = await _csrf(client, user)
    return await client.post(
        url,
        data={**(data or {}), "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf, "accept": "text/html"},
        follow_redirects=False,
    )


# Project pages: (path with {key}, the viewer's status in the public project).
PROJECT_PAGES = [
    ("/projects/{key}/sprints/", 200),
    ("/projects/{key}/backlog/", 200),
    ("/projects/{key}/sprints/analytics/", 200),
    ("/projects/{key}/time-entries/", 403),
    ("/projects/{key}/time-entries/new/", 403),
    ("/projects/{key}/recurring-patterns/", 200),
    ("/projects/{key}/roadmap/", 200),
    ("/projects/{key}/settings/", 403),
    ("/projects/{key}/issues/new/", 403),
]


@pytest.mark.parametrize(("page", "viewer_status"), PROJECT_PAGES)
async def test_project_page_refusals(client: AsyncClient, world: World, page: str, viewer_status: int) -> None:
    open_url = page.format(key="RDOPEN")

    signed_out = await _get(client, None, open_url)
    assert signed_out.status_code == 302
    assert signed_out.headers["location"] == f"/login/?next={quote(open_url, safe='')}"

    assert (await _get(client, world.outsider, page.format(key="RDNONE"))).status_code == 404
    assert (await _get(client, world.outsider, page.format(key="RDSEC"))).status_code == 404
    assert (await _get(client, world.outsider, page.format(key="RDOLD"))).status_code == 404
    assert (await _get(client, world.outsider, page.format(key="rdsec"))).status_code == 404

    assert (await _get(client, world.viewer, open_url)).status_code == viewer_status


@pytest.mark.parametrize(
    ("page", "viewer_status"),
    [("/issue/{ref}/edit/", 403), ("/issue/{ref}/versions/1/", 404)],
)
async def test_issue_page_refusals(client: AsyncClient, world: World, page: str, viewer_status: int) -> None:
    open_url = page.format(ref="RDOPEN-1")

    signed_out = await _get(client, None, open_url)
    assert signed_out.status_code == 302
    assert signed_out.headers["location"] == f"/login/?next={quote(open_url, safe='')}"

    assert (await _get(client, world.outsider, page.format(ref="RDOPEN-999"))).status_code == 404
    assert (await _get(client, world.outsider, page.format(ref="RDNONE-1"))).status_code == 404
    assert (await _get(client, world.outsider, page.format(ref="RDSEC-1"))).status_code == 404
    assert (await _get(client, world.outsider, page.format(ref="RDOLD-1"))).status_code == 404

    assert (await _get(client, world.viewer, open_url)).status_code == viewer_status


@pytest.mark.parametrize(
    ("page", "data"),
    [("/issue/{ref}/versions/1/restore/", {}), ("/issue/{ref}/move/", {"target_project_key": "RDOPEN"})],
)
async def test_issue_action_refusals(client: AsyncClient, world: World, page: str, data: dict[str, str]) -> None:
    signed_out = await _post(client, None, page.format(ref="RDOPEN-1"), data)
    assert signed_out.status_code == 302
    assert signed_out.headers["location"] == "/login/"

    assert (await _post(client, world.outsider, page.format(ref="RDOPEN-999"), data)).status_code == 404
    assert (await _post(client, world.outsider, page.format(ref="RDSEC-1"), data)).status_code == 404
    assert (await _post(client, world.outsider, page.format(ref="RDOLD-1"), data)).status_code == 404

    # The viewer reaches the issue but may not edit it.
    assert (await _post(client, world.viewer, page.format(ref="RDOPEN-1"), data)).status_code == 403


# Wiki pages. The viewer reaches the public project but holds no wiki permission: 403.
WIKI_PAGES = [
    "/projects/{key}/wiki/new/",
    "/projects/{key}/wiki/trash/",
    "/projects/{key}/wiki/start/edit/",
    "/projects/{key}/wiki/start/diff/",
    "/projects/{key}/wiki/start/history/",
]
WIKI_ACTIONS = ["/projects/{key}/wiki/start/delete/", "/projects/{key}/wiki/trash/1/restore/"]


@pytest.mark.parametrize("page", WIKI_PAGES)
async def test_wiki_page_refusals(client: AsyncClient, world: World, page: str) -> None:
    open_url = page.format(key="RDOPEN")

    signed_out = await _get(client, None, open_url)
    assert signed_out.status_code == 302
    assert signed_out.headers["location"] == f"/login/?next={quote(open_url, safe='')}"

    for key in ("RDNONE", "RDSEC", "RDOLD", "rdsec"):
        assert (await _get(client, world.outsider, page.format(key=key))).status_code == 404

    assert (await _get(client, world.viewer, open_url)).status_code == 403


@pytest.mark.parametrize("page", WIKI_ACTIONS)
async def test_wiki_action_refusals(client: AsyncClient, world: World, page: str) -> None:
    signed_out = await _post(client, None, page.format(key="RDOPEN"))
    assert signed_out.status_code == 302
    assert signed_out.headers["location"] == "/login/"

    for key in ("RDNONE", "RDSEC", "RDOLD"):
        assert (await _post(client, world.outsider, page.format(key=key))).status_code == 404

    assert (await _post(client, world.viewer, page.format(key="RDOPEN"))).status_code == 403


@pytest.mark.parametrize("page", [p for p, _ in PROJECT_PAGES] + WIKI_PAGES)
async def test_refusal_does_not_name_the_private_project(client: AsyncClient, world: World, page: str) -> None:
    """A retired or differently cased key of a private project never brings its current key into the page."""
    for typed in ("RDOLD", "rdsec"):
        resp = await _get(client, world.outsider, page.format(key=typed))
        assert resp.status_code == 404
        assert "RDSEC" not in resp.text


@pytest.mark.parametrize("page", ["/issue/{ref}/edit/", "/issue/{ref}/versions/1/"])
async def test_issue_refusal_does_not_name_the_private_project(client: AsyncClient, world: World, page: str) -> None:
    resp = await _get(client, world.outsider, page.format(ref="RDOLD-1"))
    assert resp.status_code == 404
    assert "RDSEC" not in resp.text
