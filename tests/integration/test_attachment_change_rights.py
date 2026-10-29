"""Deleting a file, or editing its description, takes the right to change its container.

The upload rules apply: the file's author may change it while they may still
add files to its container (``edit_issues`` or ``add_issue_notes`` on an
issue, ``manage_wiki`` on a wiki page, ``edit_own_notes`` on their own
comment). Anyone else's file takes ``edit_issues`` / ``manage_wiki`` /
``edit_notes``. A file the user may not open is 404, one they may open but not
change is 403, and a key limited to another project gets 404. Removing a file
from an issue stays recorded in its history.

The world is the one ``test_issue_visibility_listings`` builds.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.journal import ATTACHMENT_DETAIL, Journal, JournalDetail
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.attachment_service import AttachmentService
from specivo.services.permission_service import clear_role_cache
from tests.factories.user import UserFactory
from tests.integration.test_issue_visibility_listings import World, _bearer, build_world, go_live

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    clear_role_cache(db_session)
    w = await build_world(db_session, "ACR")
    yield w
    clear_role_cache(db_session)


async def _member(db: AsyncSession, w: World, login: str, permissions: list[str]) -> tuple[User, Role]:
    user = UserFactory.build(login=login, status="active")
    role = Role(name=f"ACR {login}", permissions=permissions, builtin=0, issues_visibility="default")
    db.add_all([user, role])
    await db.flush()
    member = Member(user_id=user.id, project_id=w.p.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)
    return user, role


async def _file(db: AsyncSession, container_type: str, container_id: int, author: User, name: str) -> Attachment:
    attachment = Attachment(
        container_type=container_type,
        container_id=container_id,
        filename=name,
        disk_filename=f"acr-{name}",
        content_type="text/plain",
        filesize=1,
        author_id=author.id,
    )
    db.add(attachment)
    await db.commit()
    return attachment


async def _delete(client: AsyncClient, headers: dict[str, str], attachment: Attachment) -> int:
    return (await client.delete(f"/api/v1/attachments/{attachment.id}/", headers=headers)).status_code


async def _describe(client: AsyncClient, headers: dict[str, str], attachment: Attachment) -> int:
    resp = await client.patch(f"/api/v1/attachments/{attachment.id}/", headers=headers, json={"description": "x"})
    return resp.status_code


async def test_an_author_may_change_their_file_only_while_they_may_add_files(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    author, role = await _member(db_session, w, "acr_author", ["view_issues", "add_issue_notes"])
    kept = await _file(db_session, "Issue", w.pub.id, author, "kept.txt")
    gone = await _file(db_session, "Issue", w.pub.id, author, "gone.txt")
    headers = await _bearer(client, author)
    assert await _describe(client, headers, kept) == 200
    assert await _delete(client, headers, gone) == 204

    # The deletion is still recorded in the issue's history.
    rows = (
        await db_session.execute(
            select(JournalDetail.old_value)
            .join(Journal, Journal.id == JournalDetail.journal_id)
            .where(Journal.issue_id == w.pub.id, JournalDetail.property == ATTACHMENT_DETAIL)
        )
    ).all()
    assert [r[0] for r in rows] == ["gone.txt"]

    role.permissions = ["view_issues"]
    await db_session.commit()
    clear_role_cache(db_session)
    assert await _describe(client, headers, kept) == 403
    assert await _delete(client, headers, kept) == 403


async def test_someone_elses_file_takes_edit_issues(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    commenter, _role = await _member(db_session, w, "acr_commenter", ["view_issues", "add_issue_notes"])
    theirs = await _file(db_session, "Issue", w.pub.id, w.manager, "theirs.txt")
    assert await _delete(client, await _bearer(client, commenter), theirs) == 403
    assert await _delete(client, await _bearer(client, w.reporter), theirs) == 403
    assert await _delete(client, await _bearer(client, w.outsider), theirs) == 403

    editor, _role = await _member(db_session, w, "acr_editor", ["view_issues", "edit_issues"])
    assert await _delete(client, await _bearer(client, editor), theirs) == 204


async def test_an_admin_may_delete_any_file(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    w = world
    theirs = await _file(db_session, "Issue", w.priv.id, w.manager, "admin-target.txt")
    assert await _delete(client, await _bearer(client, w.admin), theirs) == 204


async def test_a_file_the_user_may_not_open_is_not_found(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    hidden = await _file(db_session, "Issue", w.priv.id, w.manager, "hidden.txt")
    assert await _delete(client, await _bearer(client, w.reporter), hidden) == 404

    elsewhere = await _file(db_session, "Issue", w.pub.id, w.manager, "elsewhere.txt")
    limited = {"Authorization": f"Bearer {w.limited_raw}"}
    assert await _delete(client, limited, elsewhere) == 404
    assert await _describe(client, limited, elsewhere) == 404


async def test_an_anonymous_visitor_may_not_change_files(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    target = await _file(db_session, "Issue", w.pub.id, w.manager, "public.txt")
    await go_live(db_session, w)
    anonymous = await get_anonymous_user(db_session)
    assert await AttachmentService().can_change_attachment(db_session, anonymous, target) is False
    assert (await client.delete(f"/api/v1/attachments/{target.id}/")).status_code in (401, 403, 404)
    assert await db_session.get(Attachment, target.id) is not None


async def test_wiki_files_take_manage_wiki(client: AsyncClient, db_session: AsyncSession, world: World) -> None:
    w = world
    page_id = (await db_session.execute(select(WikiPage.id).where(WikiPage.slug == w.wiki_slug))).scalar_one()
    reader, _r = await _member(db_session, w, "acr_wiki_reader", ["view_wiki"])
    editor, _e = await _member(db_session, w, "acr_wiki_editor", ["view_wiki", "manage_wiki"])
    target = await _file(db_session, "WikiPage", page_id, w.manager, "diagram.txt")
    own = await _file(db_session, "WikiPage", page_id, reader, "readers-own.txt")

    assert await _delete(client, await _bearer(client, reader), target) == 403
    # Without manage_wiki the reader may not have added the file, nor remove it.
    assert await _delete(client, await _bearer(client, reader), own) == 403
    assert await _delete(client, await _bearer(client, editor), target) == 204


async def test_comment_files_take_the_note_editing_rights(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    own_editor, _o = await _member(db_session, w, "acr_own_notes", ["view_issues", "edit_own_notes"])
    any_editor, _a = await _member(db_session, w, "acr_any_notes", ["view_issues", "edit_notes"])
    comment = Journal(issue_id=w.pub.id, project_id=w.p.id, user_id=w.manager.id, notes="See file.", sequence=1)
    db_session.add(comment)
    await db_session.commit()
    target = await _file(db_session, "Journal", comment.id, w.manager, "comment-file.txt")

    assert await _delete(client, await _bearer(client, own_editor), target) == 403
    assert await _delete(client, await _bearer(client, any_editor), target) == 204
