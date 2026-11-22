"""Deleting a wiki page and reaching the trash from the web UI.

The page offers Delete and the wiki offers a Trash link only to members holding
``delete_wiki_pages`` (the permission the trash and its Restore also require).
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
from specivo.models.wiki import WikiPage
from specivo.services.permission_service import clear_role_cache
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = WikiService()

_DELETER_PERMS = ["view_wiki", "manage_wiki", "delete_wiki_pages"]
_EDITOR_PERMS = ["view_wiki", "manage_wiki"]
_READER_PERMS = ["view_wiki"]


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="WDU", identifier="wiki-delete-ui")
    db_session.add(proj)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=proj.id, name="wiki"))
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


async def _member(db: AsyncSession, project: Project, login: str, permissions: list[str]) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    role = Role(name=f"WDU-{login}", permissions=permissions, builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)
    return user


async def _headers(client: AsyncClient, user: User) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def deleter(client: AsyncClient, db_session: AsyncSession, project: Project) -> dict[str, str]:
    user = await _member(db_session, project, "wdu_deleter", _DELETER_PERMS)
    return await _headers(client, user)


@pytest_asyncio.fixture
async def editor(client: AsyncClient, db_session: AsyncSession, project: Project) -> dict[str, str]:
    user = await _member(db_session, project, "wdu_editor", _EDITOR_PERMS)
    return await _headers(client, user)


@pytest_asyncio.fixture
async def reader(client: AsyncClient, db_session: AsyncSession, project: Project) -> dict[str, str]:
    user = await _member(db_session, project, "wdu_reader", _READER_PERMS)
    return await _headers(client, user)


async def _author(db: AsyncSession) -> User:
    user = UserFactory.build(login="wdu_author", status="active")
    db.add(user)
    await db.commit()
    return user


async def _page(
    db: AsyncSession, project: Project, title: str, author: User, parent: WikiPage | None = None
) -> WikiPage:
    page, _ = await _svc.create_page(
        db, project.id, title, "content", author, parent_slug=parent.slug if parent else None
    )
    await db.commit()
    return page


# ---------------------------------------------------------------------------
# Visibility of the Delete action and the Trash link
# ---------------------------------------------------------------------------


async def test_page_shows_delete_and_trash_with_permission(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    page = await _page(db_session, project, "Runbook", await _author(db_session))

    resp = await client.get(f"/projects/{project.key}/wiki/{page.slug}/", headers=deleter)

    assert resp.status_code == 200
    assert 'data-testid="wiki-delete-button"' in resp.text
    assert f'action="/projects/{project.key}/wiki/{page.slug}/delete/"' in resp.text
    assert 'name="csrf_token"' in resp.text
    assert 'data-testid="wiki-trash-link"' in resp.text
    # A page without subpages offers no cascade choice.
    assert 'name="cascade_children"' not in resp.text


async def _assert_no_delete_or_trash(client: AsyncClient, db: AsyncSession, project: Project, headers: dict[str, str]):
    page = await _page(db, project, "Runbook", await _author(db))

    resp = await client.get(f"/projects/{project.key}/wiki/{page.slug}/", headers=headers)

    assert resp.status_code == 200
    assert "wiki-delete-button" not in resp.text
    assert "/delete/" not in resp.text
    assert "wiki-trash-link" not in resp.text
    assert "/wiki/trash/" not in resp.text


async def test_page_hides_delete_and_trash_from_editor(
    client: AsyncClient, db_session: AsyncSession, project: Project, editor: dict[str, str]
):
    await _assert_no_delete_or_trash(client, db_session, project, editor)


async def test_page_hides_delete_and_trash_from_reader(
    client: AsyncClient, db_session: AsyncSession, project: Project, reader: dict[str, str]
):
    await _assert_no_delete_or_trash(client, db_session, project, reader)


async def test_home_page_offers_no_delete(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    await _svc.ensure_home_page(db_session, project.id, author)
    await _page(db_session, project, "Other", author)  # so the page panel (with the Trash link) renders

    resp = await client.get(f"/projects/{project.key}/wiki/home/", headers=deleter)

    assert resp.status_code == 200
    assert "wiki-delete-button" not in resp.text
    assert 'data-testid="wiki-trash-link"' in resp.text


async def test_delete_dialog_offers_subpages_with_count_and_parent(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    top = await _page(db_session, project, "Handbook", author)
    middle = await _page(db_session, project, "Onboarding", author, parent=top)
    child = await _page(db_session, project, "Accounts", author, parent=middle)
    await _page(db_session, project, "Laptop setup", author, parent=child)

    resp = await client.get(f"/projects/{project.key}/wiki/{middle.slug}/", headers=deleter)

    assert resp.status_code == 200
    assert 'name="cascade_children"' in resp.text
    assert "Also move its 2 subpages to the trash" in resp.text
    assert "move up one level, under Handbook" in resp.text


async def test_delete_dialog_top_level_page_subpages_move_to_top(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    top = await _page(db_session, project, "Handbook", author)
    await _page(db_session, project, "Onboarding", author, parent=top)

    resp = await client.get(f"/projects/{project.key}/wiki/{top.slug}/", headers=deleter)

    assert "Also move its subpage to the trash" in resp.text
    assert "move up to the top level" in resp.text


async def test_page_list_shows_trash_link_with_permission(
    client: AsyncClient, project: Project, deleter: dict[str, str]
):
    resp = await client.get(f"/projects/{project.key}/wiki/pages/", headers=deleter)

    assert resp.status_code == 200
    assert f'href="/projects/{project.key}/wiki/trash/"' in resp.text


async def test_page_list_hides_trash_link_and_new_page_for_reader(
    client: AsyncClient, project: Project, reader: dict[str, str]
):
    resp = await client.get(f"/projects/{project.key}/wiki/pages/", headers=reader)

    assert resp.status_code == 200
    assert "/wiki/trash/" not in resp.text
    assert "/wiki/new/" not in resp.text


# ---------------------------------------------------------------------------
# The delete flow
# ---------------------------------------------------------------------------


async def test_delete_moves_page_to_trash_and_shows_notice(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    page = await _page(db_session, project, "Old runbook", await _author(db_session))

    resp = await client.post(
        f"/projects/{project.key}/wiki/{page.slug}/delete/", headers=deleter, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].endswith(f"/projects/{project.key}/wiki/pages/?notice=deleted")

    listing = await client.get(resp.headers["location"], headers=deleter)
    assert 'data-testid="wiki-notice"' in listing.text
    assert "Old runbook" not in listing.text

    trash = await client.get(f"/projects/{project.key}/wiki/trash/", headers=deleter)
    assert trash.status_code == 200
    assert "Old runbook" in trash.text


async def test_unknown_notice_is_ignored(client: AsyncClient, project: Project, deleter: dict[str, str]):
    resp = await client.get(f"/projects/{project.key}/wiki/pages/?notice=bogus", headers=deleter)
    assert "wiki-notice" not in resp.text


async def test_delete_without_cascade_moves_subpages_up(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    top = await _page(db_session, project, "Handbook", author)
    middle = await _page(db_session, project, "Onboarding", author, parent=top)
    child = await _page(db_session, project, "Accounts", author, parent=middle)

    top_id = top.id
    resp = await client.post(
        f"/projects/{project.key}/wiki/{middle.slug}/delete/", headers=deleter, follow_redirects=False
    )
    assert resp.status_code == 302

    child_id = child.id
    db_session.expire_all()
    refreshed = await db_session.scalar(select(WikiPage).where(WikiPage.id == child_id))
    assert refreshed is not None
    assert refreshed.deleted_at is None
    assert refreshed.parent_id == top_id


async def test_delete_with_cascade_trashes_subpages(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    top = await _page(db_session, project, "Handbook", author)
    child = await _page(db_session, project, "Accounts", author, parent=top)

    resp = await client.post(
        f"/projects/{project.key}/wiki/{top.slug}/delete/",
        data={"cascade_children": "true"},
        headers=deleter,
        follow_redirects=False,
    )
    assert resp.status_code == 302

    child_id = child.id
    db_session.expire_all()
    refreshed = await db_session.scalar(select(WikiPage).where(WikiPage.id == child_id))
    assert refreshed is not None
    assert refreshed.deleted_at is not None


async def test_delete_refused_without_permission(
    client: AsyncClient, db_session: AsyncSession, project: Project, editor: dict[str, str]
):
    page = await _page(db_session, project, "Keep me", await _author(db_session))

    resp = await client.post(f"/projects/{project.key}/wiki/{page.slug}/delete/", headers=editor)

    assert resp.status_code == 403
    page_id = page.id
    db_session.expire_all()
    refreshed = await db_session.scalar(select(WikiPage).where(WikiPage.id == page_id))
    assert refreshed is not None
    assert refreshed.deleted_at is None


async def test_trash_refused_without_permission(client: AsyncClient, project: Project, editor: dict[str, str]):
    resp = await client.get(f"/projects/{project.key}/wiki/trash/", headers=editor)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Restore results
# ---------------------------------------------------------------------------


async def test_restore_shows_notice(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    page = await _page(db_session, project, "Comeback", author)
    await _svc.delete_page(db_session, page.id, deleted_by=author)
    await db_session.commit()

    resp = await client.post(
        f"/projects/{project.key}/wiki/trash/{page.id}/restore/", headers=deleter, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/wiki/trash/?notice=restored")

    trash = await client.get(resp.headers["location"], headers=deleter)
    assert 'data-testid="wiki-trash-notice"' in trash.text


async def test_restore_conflict_shows_error_instead_of_json(
    client: AsyncClient, db_session: AsyncSession, project: Project, deleter: dict[str, str]
):
    author = await _author(db_session)
    page = await _page(db_session, project, "Duplicate", author)
    await _svc.delete_page(db_session, page.id, deleted_by=author)
    await db_session.commit()
    await _page(db_session, project, "Duplicate", author)  # takes the slug back

    resp = await client.post(
        f"/projects/{project.key}/wiki/trash/{page.id}/restore/", headers=deleter, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/wiki/trash/?error=slug_taken")

    trash = await client.get(resp.headers["location"], headers=deleter)
    assert 'data-testid="wiki-trash-error"' in trash.text
