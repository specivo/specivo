"""Adding a file needs the right to change its container; velocity and wiki counts hide what they should.

The world is the one ``test_issue_visibility_listings`` builds (see there):
a public project P with a public and a private issue in an active sprint,
read by a manager (every permission), a reporter (``view_issues`` and
``add_issues`` only), an outsider reading P as a non-member (the Non member
role grants ``view_issues`` only), anonymous visitors and a key limited to
another project.

- Uploading takes ``edit_issues`` / ``add_issue_notes`` (or ``add_issues`` on
  one's own issue) on an issue, ``manage_wiki`` on a wiki page, and
  ``edit_notes`` (or ``edit_own_notes`` on one's own) on a comment: 403 for
  a reader without it, 404 where the container is hidden.
- A completed sprint's velocity counts only the issues the reader may see;
  a snapshot without issue ids is shown only to readers who see every issue.
- The project list counts wiki pages only where the reader may read the wiki.
- ``PATCH /issues/{ref}/`` returns the history entry it created.
"""

from __future__ import annotations

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import ATTACHMENT_DETAIL, Journal, JournalDetail
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.api_key_scope import ProjectScope, bound_scope
from specivo.services.attachment_service import AttachmentService
from specivo.services.permission_service import clear_role_cache
from specivo.services.project_service import ProjectService
from specivo.services.sprint_service import SprintService
from tests.factories.user import UserFactory
from tests.integration.test_issue_visibility_listings import (
    World,
    _bearer,
    _cookie,
    _issue,
    build_world,
    go_live,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_attachments = AttachmentService()
_sprints = SprintService()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    clear_role_cache(db_session)
    w = await build_world(db_session, "UAC")
    yield w
    clear_role_cache(db_session)


async def _member(db: AsyncSession, w: World, login: str, permissions: list[str]) -> User:
    """A member of P whose one role grants *permissions*."""
    user = UserFactory.build(login=login, status="active")
    role = Role(name=f"UAC {login}", permissions=permissions, builtin=0, issues_visibility="default")
    db.add_all([user, role])
    await db.flush()
    member = Member(user_id=user.id, project_id=w.p.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)
    return user


async def _post(client: AsyncClient, headers: dict[str, str], container_type: str, container_id: int):
    return await client.post(
        "/api/v1/attachments/",
        headers=headers,
        files={"file": ("note.txt", io.BytesIO(b"note"), "text/plain")},
        data={"container_type": container_type, "container_id": str(container_id)},
    )


async def _wiki_page_id(db: AsyncSession, w: World) -> int:
    return (await db.execute(select(WikiPage.id).where(WikiPage.slug == w.wiki_slug))).scalar_one()


# ---------------------------------------------------------------------------
# Upload access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_a_reader_may_not_add_files_to_an_issue(client: AsyncClient, world: World, reader: str) -> None:
    w = world
    resp = await _post(client, await _bearer(client, getattr(w, reader)), "Issue", w.pub.id)
    assert resp.status_code == 403, resp.text


async def test_a_hidden_issue_is_not_found_for_an_upload(client: AsyncClient, world: World) -> None:
    w = world
    resp = await _post(client, await _bearer(client, w.reporter), "Issue", w.priv.id)
    assert resp.status_code == 404, resp.text


async def test_an_editor_and_a_commenter_may_add_files_to_an_issue(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    commenter = await _member(db_session, w, "uac_commenter", ["view_issues", "add_issue_notes"])
    for user in (w.manager, commenter):
        resp = await _post(client, await _bearer(client, user), "Issue", w.pub.id)
        assert resp.status_code == 201, (user.login, resp.text)


async def test_the_creator_of_an_issue_may_add_files_to_it(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    """``add_issues`` covers the files of one's own new issue, not anybody else's."""
    w = world
    lookups = (
        await db_session.get(Tracker, w.pub.tracker_id),
        await db_session.get(IssueStatus, w.pub.status_id),
        await db_session.get(IssuePriority, w.pub.priority_id),
    )
    own = await _issue(db_session, w.p, lookups, w.reporter, "UAC reporter's own marmot")
    await db_session.commit()
    headers = await _bearer(client, w.reporter)
    assert (await _post(client, headers, "Issue", own.id)).status_code == 201
    assert (await _post(client, headers, "Issue", w.pub.id)).status_code == 403


async def test_an_anonymous_visitor_may_not_add_files(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    await go_live(db_session, w)
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    assert await _attachments.can_view_container(db_session, anonymous, "Issue", w.pub.id) is False
    assert await _attachments.can_attach_to_container(db_session, anonymous, "Issue", w.pub.id) is False

    resp = await _post(client, {}, "Issue", w.pub.id)
    assert resp.status_code in (401, 403), resp.text


async def test_a_key_limited_to_another_project_gets_not_found(client: AsyncClient, world: World) -> None:
    w = world
    resp = await _post(client, {"Authorization": f"Bearer {w.limited_raw}"}, "Issue", w.pub.id)
    assert resp.status_code == 404, resp.text


async def test_adding_files_to_a_wiki_page_takes_manage_wiki(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    page_id = await _wiki_page_id(db_session, w)
    reader = await _member(db_session, w, "uac_wiki_reader", ["view_wiki"])
    editor = await _member(db_session, w, "uac_wiki_editor", ["view_wiki", "manage_wiki"])

    assert (await _post(client, await _bearer(client, reader), "WikiPage", page_id)).status_code == 403
    assert (await _post(client, await _bearer(client, editor), "WikiPage", page_id)).status_code == 201
    # The reporter may not even read P's wiki: the page is not found.
    assert (await _post(client, await _bearer(client, w.reporter), "WikiPage", page_id)).status_code == 404


async def test_adding_files_to_a_comment_takes_the_note_editing_rights(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    own_editor = await _member(db_session, w, "uac_own_notes", ["view_issues", "edit_own_notes"])
    any_editor = await _member(db_session, w, "uac_any_notes", ["view_issues", "edit_notes"])
    mine = Journal(issue_id=w.pub.id, project_id=w.p.id, user_id=own_editor.id, notes="Mine.", sequence=1)
    theirs = Journal(issue_id=w.pub.id, project_id=w.p.id, user_id=w.manager.id, notes="Theirs.", sequence=2)
    db_session.add_all([mine, theirs])
    await db_session.commit()

    own_headers = await _bearer(client, own_editor)
    assert (await _post(client, own_headers, "Journal", mine.id)).status_code == 201
    assert (await _post(client, own_headers, "Journal", theirs.id)).status_code == 403
    assert (await _post(client, await _bearer(client, any_editor), "Journal", theirs.id)).status_code == 201
    assert (await _post(client, await _bearer(client, w.reporter), "Journal", theirs.id)).status_code == 403


# ---------------------------------------------------------------------------
# Sprint velocity
# ---------------------------------------------------------------------------


async def _complete(db: AsyncSession, w: World) -> None:
    await _sprints.complete_sprint(db, w.sprint)
    await db.commit()


async def _velocity(db: AsyncSession, w: World, user: User) -> dict[str, int] | None:
    return (await _sprints.velocity_for(db, [w.sprint], user))[w.sprint.id]


async def test_velocity_counts_only_the_issues_the_reader_may_see(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    await _complete(db_session, w)
    # Nothing was done, so both issues went back to the backlog.
    assert {k: w.sprint.velocity_snapshot[k] for k in ("total_issues", "completed_issues")} == {
        "total_issues": 2,
        "completed_issues": 0,
    }

    assert await _velocity(db_session, w, w.reporter) == {"total_issues": 1, "completed_issues": 0}
    assert await _velocity(db_session, w, w.outsider) == {"total_issues": 1, "completed_issues": 0}
    assert await _velocity(db_session, w, w.manager) == {"total_issues": 2, "completed_issues": 0}
    assert await _velocity(db_session, w, w.admin) == {"total_issues": 2, "completed_issues": 0}
    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        assert await _velocity(db_session, w, w.manager) == {"total_issues": 0, "completed_issues": 0}
    await go_live(db_session, w)
    anonymous = await get_anonymous_user(db_session)
    assert await _velocity(db_session, w, anonymous) == {"total_issues": 1, "completed_issues": 0}

    resp = await client.get(f"/api/v1/projects/{w.p.key}/sprints/", headers=await _bearer(client, w.reporter))
    assert resp.status_code == 200, resp.text
    sprint = next(s for s in resp.json() if s["id"] == w.sprint.id)
    # Counts for the reader, never the ids of the issues counted.
    assert sprint["velocity_snapshot"] == {"total_issues": 1, "completed_issues": 0}


async def test_velocity_pages_show_the_readers_counts(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    await _complete(db_session, w)
    for user, committed in ((w.reporter, 1), (w.manager, 2)):
        resp = await client.get(f"/projects/{w.p.key}/sprints/analytics/", cookies=_cookie(user))
        assert resp.status_code == 200, resp.text
        text = " ".join(resp.text.split())
        assert f'title="Committed: {committed}"' in text, user.login
        assert f'title="Committed: {3 - committed}"' not in text, user.login


async def test_a_snapshot_without_ids_is_shown_only_to_readers_who_see_everything(
    db_session: AsyncSession, world: World
) -> None:
    w = world
    await _complete(db_session, w)
    w.sprint.velocity_snapshot = {"total_issues": 2, "completed_issues": 0}
    await db_session.commit()

    assert await _velocity(db_session, w, w.reporter) is None
    assert await _velocity(db_session, w, w.outsider) is None
    # The manager created every issue of P, so sees them all.
    assert await _velocity(db_session, w, w.manager) == {"total_issues": 2, "completed_issues": 0}
    assert await _velocity(db_session, w, w.admin) == {"total_issues": 2, "completed_issues": 0}
    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        assert await _velocity(db_session, w, w.manager) is None
    assert await _sprints.average_velocity(db_session, w.p.id, w.reporter) == 0


# ---------------------------------------------------------------------------
# Wiki page count
# ---------------------------------------------------------------------------


async def test_project_stats_count_wiki_pages_only_for_readers_of_the_wiki(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    svc = ProjectService()
    for user, pages in ((w.reporter, 0), (w.manager, 1), (w.admin, 1)):
        stats = await svc.load_project_stats(db_session, [w.p.id], reader=user)
        assert stats[w.p.id]["wiki_page_count"] == pages, user.login

    reporter_page = " ".join((await client.get("/projects/", cookies=_cookie(w.reporter))).text.split())
    assert "1 wiki page" not in reporter_page
    manager_page = " ".join((await client.get("/projects/", cookies=_cookie(w.manager))).text.split())
    assert "1 wiki page" in manager_page


# ---------------------------------------------------------------------------
# PATCH returns its history entry
# ---------------------------------------------------------------------------


async def test_patch_returns_the_history_entry_and_an_upload_joins_it(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    headers = await _bearer(client, w.manager)
    url = f"/api/v1/issues/{w.pub.display_key}/"

    lock = (await client.get(url, headers=headers)).json()["lock_version"]
    resp = await client.patch(url, headers=headers, json={"subject": "UAC renamed marmot", "lock_version": lock})
    assert resp.status_code == 200, resp.text
    journal_id = resp.json()["journal_id"]
    journal = await db_session.get(Journal, journal_id)
    assert journal is not None and journal.issue_id == w.pub.id

    upload = await client.post(
        "/api/v1/attachments/",
        headers=headers,
        files={"file": ("with-edit.txt", io.BytesIO(b"x"), "text/plain")},
        data={"container_type": "Issue", "container_id": str(w.pub.id), "journal_id": str(journal_id)},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["journal_id"] == journal_id
    details = (
        await db_session.execute(
            select(JournalDetail.new_value).where(
                JournalDetail.journal_id == journal_id, JournalDetail.property == ATTACHMENT_DETAIL
            )
        )
    ).all()
    assert [d[0] for d in details] == ["with-edit.txt"]

    lock = (await client.get(url, headers=headers)).json()["lock_version"]
    unchanged = await client.patch(url, headers=headers, json={"subject": "UAC renamed marmot", "lock_version": lock})
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["journal_id"] is None
