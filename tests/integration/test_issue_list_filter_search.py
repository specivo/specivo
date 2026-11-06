"""Issue list filters: assignee, tag and release, searched as you type.

Covers the issue list page (list and board views), the issues API, saved
filters, and the suggestion endpoints the filter fields search:

- ``GET /api/v1/projects/{key}/assignees/search/``
- ``GET /api/v1/projects/{key}/tags/search/`` (paged with ``offset``)
- ``GET /api/v1/projects/{key}/versions/search/?include_shared=true``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.lookup_cache import invalidate_lookups
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.saved_filter import SavedFilter
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.models.version import Version
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory
from tests.factories.version import VersionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass(frozen=True)
class World:
    reader: User
    alice: User
    bob: User
    outsider: User
    grouped: User
    system: User
    project: Project
    other: Project
    shared_from: Project
    tag_front: Tag
    tag_back: Tag
    other_tag: Tag
    v_open: Version
    v_closed: Version
    v_shared: Version
    v_hidden: Version
    i_alice: Issue
    i_unassigned: Issue
    i_mine: Issue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _project(db: AsyncSession, key: str, *, is_public: bool) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db.flush()
    return project


async def _grant(db: AsyncSession, project: Project, *, user: User | None = None, group: UserGroup | None = None):
    role = Role(
        name=f"Filter role {project.key} {user.login if user else group.name}",
        permissions=["view_issues", "add_issues", "edit_issues"],
        builtin=0,
        issues_visibility="all",
    )
    db.add(role)
    await db.flush()
    member = Member(project_id=project.id, user_id=user.id if user else None, group_id=group.id if group else None)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _issue(db: AsyncSession, project: Project, lookups, author: User, subject: str, **kw) -> Issue:
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
        subject=subject,
        **kw,
    )
    db.add(issue)
    await db.flush()
    return issue


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add_all([tracker, priority])
    await db.flush()
    lookups = (tracker, status, priority)

    reader = UserFactory.build(login="fls_reader", display_name="Reader Somchai", status="active")
    alice = UserFactory.build(login="fls_alice", display_name="Alice Thongchai", status="active")
    bob = UserFactory.build(login="fls_bob", display_name="Bob Niran", status="active")
    outsider = UserFactory.build(login="fls_outsider", display_name="Alice Outsider", status="active")
    grouped = UserFactory.build(login="fls_grouped", display_name="Alice Grouped", status="active")
    system = User(
        login="$flsbot",
        email="flsbot@specivo.invalid",
        display_name="Alice Bot",
        password_hash=None,
        status="active",
        is_admin=False,
        must_change_password=False,
    )
    db.add_all([reader, alice, bob, outsider, grouped, system])
    await db.flush()

    project = await _project(db, "FLS", is_public=False)
    other = await _project(db, "FLO", is_public=False)
    shared_from = await _project(db, "FLQ", is_public=True)

    for user in (reader, alice, bob, system):
        await _grant(db, project, user=user)
    await _grant(db, other, user=outsider)
    group = UserGroup(name="FLS group")
    db.add(group)
    await db.flush()
    db.add(UserGroupMember(group_id=group.id, user_id=grouped.id))
    await db.flush()
    await _grant(db, project, group=group)

    tag_front = Tag(project_id=project.id, name="frontend", color="#3366ff")
    tag_back = Tag(project_id=project.id, name="backend")
    other_tag = Tag(project_id=other.id, name="frontier")
    db.add_all([tag_front, tag_back, other_tag])

    v_open = VersionFactory.build(project_id=project.id, name="1.0", status="open")
    v_closed = VersionFactory.build(project_id=project.id, name="0.9", status="closed")
    v_shared = VersionFactory.build(project_id=shared_from.id, name="Shared 2.0", sharing="system")
    v_hidden = VersionFactory.build(project_id=other.id, name="Hidden 3.0", sharing="system")
    db.add_all([v_open, v_closed, v_shared, v_hidden])
    await db.flush()

    i_alice = await _issue(
        db, project, lookups, reader, "Alice frontend work", assigned_to_id=alice.id, fixed_version_id=v_open.id
    )
    i_unassigned = await _issue(db, project, lookups, reader, "Nobody owns this")
    i_mine = await _issue(
        db, project, lookups, reader, "Reader shared work", assigned_to_id=reader.id, fixed_version_id=v_shared.id
    )
    db.add(TagLink(tag_id=tag_front.id, issue_id=i_alice.id))
    db.add(TagLink(tag_id=tag_back.id, issue_id=i_mine.id))
    await db.commit()
    clear_role_cache(db)
    # The board draws a column per status from the process-wide lookup cache.
    invalidate_lookups()

    return World(
        reader=reader,
        alice=alice,
        bob=bob,
        outsider=outsider,
        grouped=grouped,
        system=system,
        project=project,
        other=other,
        shared_from=shared_from,
        tag_front=tag_front,
        tag_back=tag_back,
        other_tag=other_tag,
        v_open=v_open,
        v_closed=v_closed,
        v_shared=v_shared,
        v_hidden=v_hidden,
        i_alice=i_alice,
        i_unassigned=i_unassigned,
        i_mine=i_mine,
    )


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _page(client: AsyncClient, w: World, query: str = "", user: User | None = None) -> str:
    resp = await client.get(f"/projects/{w.project.key}/issues/?{query}", cookies=_cookies(user or w.reader))
    assert resp.status_code == 200, resp.text
    return resp.text


def _listed(html: str, w: World) -> set[str]:
    return {i.subject for i in (w.i_alice, w.i_unassigned, w.i_mine) if i.subject in html}


def _chip(html: str, name: str) -> str | None:
    """The text of the chip for filter *name*, or None when there is none."""
    m = re.search(rf'<span class="sp-filter-chip" data-filter-chip="{name}">(.*?)</a>\s*</span>', html, re.S)
    return unescape(re.sub(r"<[^>]+>", " ", m.group(1))) if m else None


def _chip_clear_url(html: str, name: str) -> str:
    m = re.search(
        rf'data-filter-chip="{name}">.*?<a href="([^"]+)" class="sp-filter-chip-clear"',
        html,
        re.S,
    )
    assert m, f"no clear link for {name}"
    return unescape(m.group(1))


# ---------------------------------------------------------------------------
# The page: filters narrow the list and the board
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", ["list", "board"])
async def test_assignee_filter_narrows_the_list_and_the_board(client: AsyncClient, world: World, view: str):
    w = world
    html = await _page(client, w, f"view={view}&assigned_to_id={w.alice.id}")
    assert _listed(html, w) == {w.i_alice.subject}
    html = await _page(client, w, f"view={view}&assigned_to_id=none")
    assert _listed(html, w) == {w.i_unassigned.subject}
    html = await _page(client, w, f"view={view}&assigned_to_id=me")
    assert _listed(html, w) == {w.i_mine.subject}


@pytest.mark.parametrize("view", ["list", "board"])
async def test_tag_and_release_filters_narrow_the_list_and_the_board(client: AsyncClient, world: World, view: str):
    w = world
    html = await _page(client, w, f"view={view}&tag_id={w.tag_front.id}")
    assert _listed(html, w) == {w.i_alice.subject}
    html = await _page(client, w, f"view={view}&fixed_version_id={w.v_open.id}")
    assert _listed(html, w) == {w.i_alice.subject}
    html = await _page(client, w, f"view={view}&fixed_version_id={w.v_shared.id}")
    assert _listed(html, w) == {w.i_mine.subject}
    html = await _page(client, w, f"view={view}&fixed_version_id=none")
    assert _listed(html, w) == {w.i_unassigned.subject}


async def test_filters_combine(client: AsyncClient, world: World):
    w = world
    html = await _page(client, w, f"assigned_to_id=me&tag_id={w.tag_back.id}&fixed_version_id={w.v_shared.id}")
    assert _listed(html, w) == {w.i_mine.subject}
    html = await _page(client, w, f"assigned_to_id={w.alice.id}&fixed_version_id=none")
    assert _listed(html, w) == set()


async def test_garbage_filter_values_are_ignored(client: AsyncClient, world: World):
    w = world
    html = await _page(client, w, "assigned_to_id=abc&tag_id=x&fixed_version_id=-3")
    assert _listed(html, w) == {w.i_alice.subject, w.i_unassigned.subject, w.i_mine.subject}
    assert 'data-filter-chip="' not in html


# ---------------------------------------------------------------------------
# The page: chips render server-side and clear by link
# ---------------------------------------------------------------------------


async def test_chosen_values_render_as_chips_with_their_labels(client: AsyncClient, world: World):
    w = world
    html = await _page(
        client, w, f"assigned_to_id={w.alice.id}&tag_id={w.tag_front.id}&fixed_version_id={w.v_shared.id}"
    )
    assert "Alice Thongchai" in _chip(html, "assigned_to_id")
    assert "frontend" in _chip(html, "tag_id")
    assert "Shared 2.0" in _chip(html, "fixed_version_id")
    # The value travels with the form, so Apply and the view toggle keep it.
    assert f'<input type="hidden" name="assigned_to_id" value="{w.alice.id}">' in html
    assert f'<input type="hidden" name="tag_id" value="{w.tag_front.id}">' in html
    assert f'<input type="hidden" name="fixed_version_id" value="{w.v_shared.id}">' in html
    # A field with a chip offers no search box.
    assert 'data-filter-combo="assigned_to_id"' not in html


async def test_special_values_render_as_chips(client: AsyncClient, world: World):
    w = world
    html = await _page(client, w, "assigned_to_id=none&fixed_version_id=none")
    assert "Unassigned" in _chip(html, "assigned_to_id")
    assert "No release" in _chip(html, "fixed_version_id")
    html = await _page(client, w, "assigned_to_id=me")
    assert "Me" in _chip(html, "assigned_to_id")
    assert 'value="me"' in html


async def test_without_values_the_fields_are_search_boxes(client: AsyncClient, world: World):
    w = world
    html = await _page(client, w)
    for name in ("assigned_to_id", "tag_id", "fixed_version_id"):
        assert f'data-filter-combo="{name}"' in html
        assert _chip(html, name) is None
    assert 'role="combobox"' in html
    assert 'role="listbox"' in html
    assert f"/api/v1/projects/{w.project.key}/assignees/search/" in html
    # The page no longer lists the people it could filter by.
    assert w.bob.display_name not in html
    assert w.bob.login not in html


async def test_a_chip_clear_link_drops_only_its_own_filter(client: AsyncClient, world: World):
    w = world
    html = await _page(
        client, w, f"status=all&view=board&assigned_to_id={w.alice.id}&tag_id={w.tag_front.id}&offset=25"
    )
    url = urlsplit(_chip_clear_url(html, "assigned_to_id"))
    assert url.path == f"/projects/{w.project.key}/issues/"
    params = parse_qs(url.query)
    assert "assigned_to_id" not in params
    assert "offset" not in params
    assert params["tag_id"] == [str(w.tag_front.id)]
    assert params["status"] == ["all"]
    assert params["view"] == ["board"]


async def test_ids_the_reader_cannot_see_get_a_generic_label(client: AsyncClient, world: World):
    w = world
    html = await _page(client, w, f"tag_id={w.other_tag.id}&fixed_version_id={w.v_hidden.id}")
    assert "frontier" not in html
    assert "Hidden 3.0" not in html
    assert "Unknown tag" in _chip(html, "tag_id")
    assert "Unknown release" in _chip(html, "fixed_version_id")


async def test_tag_field_is_left_out_of_a_project_without_tags(client: AsyncClient, world: World, db_session):
    w = world
    html = await client.get(f"/projects/{w.shared_from.key}/issues/", cookies=_cookies(w.reader))
    assert html.status_code == 200
    assert 'data-filter-combo="tag_id"' not in html.text
    assert 'data-filter-combo="fixed_version_id"' in html.text


async def test_pagination_keeps_the_filters(client: AsyncClient, world: World, db_session: AsyncSession):
    w = world
    tracker = (await db_session.execute(select(Issue.tracker_id).limit(1))).scalar_one()
    status = w.i_unassigned.status_id
    priority = w.i_unassigned.priority_id
    for n in range(3):
        db_session.add(
            IssueFactory.build(
                project_id=w.project.id,
                project_key=w.project.key,
                sequence_number=100 + n,
                tracker_id=tracker,
                status_id=status,
                priority_id=priority,
                author_id=w.reader.id,
                subject=f"Unassigned extra {n}",
            )
        )
    await db_session.commit()
    html = await _page(client, w, "assigned_to_id=none&limit=2")
    links = re.findall(r'href="([^"]*offset=2[^"]*)"', html)
    assert links, "no link to the second page"
    assert all("assigned_to_id=none" in unescape(link) for link in links)


# ---------------------------------------------------------------------------
# Saved filters
# ---------------------------------------------------------------------------


async def test_saved_filter_tabs_show_when_filters_exist(client: AsyncClient, world: World, db_session: AsyncSession):
    """A non-admin member sees tabs for their saved filters, and none without any."""
    w = world
    assert not w.reader.is_admin
    html = await _page(client, w)
    assert 'class="filter-tabs"' not in html

    db_session.add(SavedFilter(name="My open work", user_id=w.reader.id, project_id=w.project.id, filter_definition={}))
    await db_session.commit()
    html = await _page(client, w)
    assert 'class="filter-tabs"' in html
    assert re.search(r'class="filter-tab[^"]*">\s*My open work', html)


async def test_issue_list_has_no_bulk_checkboxes(client: AsyncClient, world: World):
    """The list view has no selection column: there is no bulk toolbar to act on it."""
    html = await _page(client, world)
    assert 'class="issue-table"' in html
    assert 'type="checkbox"' not in html.split('class="issue-table"', 1)[1].split("</table>", 1)[0]


async def test_saved_filter_tabs_link_to_their_filters(client: AsyncClient, world: World, db_session: AsyncSession):
    w = world
    db_session.add(
        SavedFilter(
            name="Release backlog",
            user_id=w.reader.id,
            project_id=w.project.id,
            filter_definition={"assigned_to_id": "none", "version_id": w.v_open.id, "tag_id": w.tag_front.id},
        )
    )
    await db_session.commit()
    html = await _page(client, w)
    m = re.search(r'<a href="([^"]+)"\s+class="filter-tab[^"]*">\s*Release backlog', html)
    assert m, "saved filter tab missing"
    params = parse_qs(urlsplit(unescape(m.group(1))).query)
    assert params == {
        "assigned_to_id": ["none"],
        "fixed_version_id": [str(w.v_open.id)],
        "tag_id": [str(w.tag_front.id)],
    }
    # Following the tab marks it active.
    html = await _page(client, w, f"assigned_to_id=none&tag_id={w.tag_front.id}&fixed_version_id={w.v_open.id}")
    assert re.search(r'class="filter-tab active">\s*Release backlog', html)


async def test_saved_filter_with_the_new_keys_narrows_the_api_list(
    client: AsyncClient, world: World, db_session: AsyncSession
):
    w = world
    sf = SavedFilter(
        name="Mine shared",
        user_id=w.reader.id,
        project_id=w.project.id,
        filter_definition={"fixed_version_id": w.v_shared.id, "tag_id": w.tag_back.id},
    )
    unassigned = SavedFilter(
        name="Nobody",
        user_id=w.reader.id,
        project_id=w.project.id,
        filter_definition={"assigned_to_id": "none", "fixed_version_id": "none"},
    )
    db_session.add_all([sf, unassigned])
    await db_session.commit()
    for saved, expected in ((sf, w.i_mine), (unassigned, w.i_unassigned)):
        resp = await client.get(
            f"/api/v1/projects/{w.project.key}/issues/?saved_filter_id={saved.id}", headers=_bearer(w.reader)
        )
        assert resp.status_code == 200, resp.text
        assert [i["subject"] for i in resp.json()["items"]] == [expected.subject]


# ---------------------------------------------------------------------------
# Issues API
# ---------------------------------------------------------------------------


async def _api_subjects(client: AsyncClient, w: World, query: str) -> set[str]:
    resp = await client.get(f"/api/v1/projects/{w.project.key}/issues/?{query}", headers=_bearer(w.reader))
    assert resp.status_code == 200, resp.text
    return {i["subject"] for i in resp.json()["items"]}


async def test_api_list_understands_none_and_fixed_version_id(client: AsyncClient, world: World):
    w = world
    assert await _api_subjects(client, w, "assigned_to_id=none") == {w.i_unassigned.subject}
    assert await _api_subjects(client, w, "assigned_to_id=me") == {w.i_mine.subject}
    assert await _api_subjects(client, w, "fixed_version_id=none") == {w.i_unassigned.subject}
    assert await _api_subjects(client, w, f"fixed_version_id={w.v_open.id}") == {w.i_alice.subject}
    assert await _api_subjects(client, w, f"version_id={w.v_shared.id}") == {w.i_mine.subject}
    assert await _api_subjects(client, w, "version_id=none") == {w.i_unassigned.subject}


async def test_api_list_refuses_a_malformed_version(client: AsyncClient, world: World):
    w = world
    resp = await client.get(
        f"/api/v1/projects/{w.project.key}/issues/?fixed_version_id=soon", headers=_bearer(w.reader)
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Suggestions: assignees
# ---------------------------------------------------------------------------


async def _assignees(client: AsyncClient, w: World, query: str = "", user: User | None = None):
    return await client.get(
        f"/api/v1/projects/{w.project.key}/assignees/search/?{query}", headers=_bearer(user or w.reader)
    )


async def test_assignee_suggestions_are_the_assignable_people(client: AsyncClient, world: World):
    w = world
    resp = await _assignees(client, w)
    assert resp.status_code == 200, resp.text
    logins = [u["login"] for u in resp.json()]
    # Direct members only, ordered by display name; never a group-only member,
    # a member of another project, or a system account.
    assert logins == [w.alice.login, w.bob.login, w.reader.login]
    row = resp.json()[0]
    assert set(row) == {"id", "login", "display_name", "avatar_url", "avatar_color"}
    assert "email" not in row


async def test_assignee_suggestions_search_login_and_display_name(client: AsyncClient, world: World):
    w = world
    by_name = [u["login"] for u in (await _assignees(client, w, "q=alice")).json()]
    assert by_name == [w.alice.login]
    by_login = [u["login"] for u in (await _assignees(client, w, "q=FLS_BO")).json()]
    assert by_login == [w.bob.login]
    assert (await _assignees(client, w, "q=%25")).json() == []


async def test_assignee_suggestions_are_paged(client: AsyncClient, world: World):
    w = world
    first = [u["login"] for u in (await _assignees(client, w, "limit=2")).json()]
    second = [u["login"] for u in (await _assignees(client, w, "limit=2&offset=2")).json()]
    assert first == [w.alice.login, w.bob.login]
    assert second == [w.reader.login]


async def test_assignee_suggestions_hide_a_private_project(client: AsyncClient, world: World):
    w = world
    resp = await _assignees(client, w, user=w.outsider)
    assert resp.status_code == 404


async def test_assignee_suggestions_need_an_account(client: AsyncClient, world: World):
    w = world
    resp = await client.get(f"/api/v1/projects/{w.shared_from.key}/assignees/search/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Suggestions: tags
# ---------------------------------------------------------------------------


async def test_tag_suggestions_are_the_projects_tags_paged(client: AsyncClient, world: World):
    w = world
    base = f"/api/v1/projects/{w.project.key}/tags/search/"
    all_tags = [t["name"] for t in (await client.get(base, headers=_bearer(w.reader))).json()]
    assert all_tags == ["backend", "frontend"]
    page2 = [t["name"] for t in (await client.get(f"{base}?limit=1&offset=1", headers=_bearer(w.reader))).json()]
    assert page2 == ["frontend"]
    found = [t["name"] for t in (await client.get(f"{base}?q=fro", headers=_bearer(w.reader))).json()]
    assert found == ["frontend"]  # never another project's "frontier"


async def test_tag_suggestions_hide_a_private_project(client: AsyncClient, world: World):
    w = world
    resp = await client.get(f"/api/v1/projects/{w.project.key}/tags/search/", headers=_bearer(w.outsider))
    assert resp.status_code == 404
    resp = await client.get(f"/api/v1/projects/{w.project.key}/tags/search/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Suggestions: releases
# ---------------------------------------------------------------------------


async def _versions(client: AsyncClient, w: World, query: str = "", user: User | None = None):
    return await client.get(
        f"/api/v1/projects/{w.project.key}/versions/search/?include_shared=true&{query}",
        headers=_bearer(user or w.reader),
    )


async def test_release_suggestions_include_visible_shared_versions_open_first(client: AsyncClient, world: World):
    w = world
    resp = await _versions(client, w)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    names = [r["name"] for r in rows]
    # Open versions (own and shared) first, closed last; the private
    # project's shared version is not visible to the reader.
    assert set(names[:2]) == {"1.0", "Shared 2.0"}
    assert names[2:] == ["0.9"]
    assert "Hidden 3.0" not in names
    keys = {r["name"]: r["project_key"] for r in rows}
    assert keys["Shared 2.0"] == w.shared_from.key
    assert keys["1.0"] == w.project.key


async def test_release_suggestions_are_searchable_and_paged(client: AsyncClient, world: World):
    w = world
    assert [r["name"] for r in (await _versions(client, w, "q=shared")).json()] == ["Shared 2.0"]
    assert [r["name"] for r in (await _versions(client, w, "q=hidden")).json()] == []
    first = (await _versions(client, w, "limit=2")).json()
    rest = (await _versions(client, w, "limit=2&offset=2")).json()
    assert len(first) == 2
    assert [r["name"] for r in rest] == ["0.9"]


async def test_release_suggestions_hide_a_private_project(client: AsyncClient, world: World):
    w = world
    assert (await _versions(client, w, user=w.outsider)).status_code == 404
    resp = await client.get(f"/api/v1/projects/{w.project.key}/versions/search/?include_shared=true")
    assert resp.status_code == 401


async def test_release_search_without_include_shared_is_unchanged(client: AsyncClient, world: World):
    w = world
    resp = await client.get(f"/api/v1/projects/{w.project.key}/versions/search/", headers=_bearer(w.reader))
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == ["1.0", "0.9"]
    assert "project_key" not in rows[0]
