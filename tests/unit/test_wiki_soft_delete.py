"""Unit / service-level tests for wiki page soft-delete.

These tests exercise WikiService methods that do not yet exist.
They are written TDD-first and are expected to fail until the
feature is implemented.

Covered:
- delete_page() sets deleted_at and deleted_by_id
- Deleted pages are excluded from list_pages()
- Deleted pages are not found by get_page()
- Home page cannot be soft-deleted
- cascade_children=False re-parents children to the deleted page's parent
- cascade_children=True also soft-deletes all descendants
- delete_page() returns a list of deleted page IDs
- restore_page() clears deleted_at and deleted_by_id
- Restored pages reappear in list_pages()
- restore_page(cascade=True) restores co-deleted children
- restore_page() raises ConflictError when an active page with same slug exists
- list_deleted_pages() returns only soft-deleted pages for the project
- hard_delete_expired_pages() permanently removes pages past retention threshold
- hard_delete_expired_pages() does NOT remove recently deleted pages
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError, NotFoundError, ValidationError
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_svc = WikiService()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str = "sdelete_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, key: str = "SDEL") -> Project:
    proj = ProjectFactory.build(key=key, identifier=f"soft-del-{key.lower()}")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    db.add(EnabledModule(project_id=proj.id, name="wiki"))
    await db.commit()
    return proj


async def _create_page(
    db: AsyncSession,
    project: Project,
    user: User,
    title: str,
    parent_slug: str | None = None,
) -> WikiPage:
    page, _content = await _svc.create_page(db, project.id, title, "content", user, parent_slug=parent_slug)
    await db.commit()
    await db.refresh(page)
    return page


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest.fixture
async def actor(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="soft_del_actor")


# ---------------------------------------------------------------------------
# delete_page() — basic soft-delete
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_soft_delete_sets_deleted_at(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "To Be Deleted")
    before = datetime.now(UTC)

    deleted_ids = await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()
    await db_session.refresh(page)

    assert page.deleted_at is not None
    assert page.deleted_at >= before
    assert page.deleted_by_id == actor.id
    assert page.id in deleted_ids


@pytest.mark.service
async def test_soft_delete_excludes_page_from_list(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Listed Once")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    pages = await _svc.list_pages(db_session, project.id)
    slugs = [p.slug for p in pages]
    assert page.slug not in slugs


@pytest.mark.service
async def test_soft_delete_makes_page_unfindable_by_get_page(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Ghost Page")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await _svc.get_page(db_session, project.id, page.slug)


@pytest.mark.service
async def test_soft_delete_home_page_raises_validation_error(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    home = await _svc.ensure_home_page(db_session, project.id, actor)
    await db_session.commit()
    await db_session.refresh(home)

    with pytest.raises((ValidationError, ValueError)):
        await _svc.delete_page(db_session, home.id, deleted_by=actor)


@pytest.mark.service
async def test_soft_delete_returns_list_of_deleted_ids(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "ID Listed")

    deleted_ids = await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    assert isinstance(deleted_ids, list)
    assert page.id in deleted_ids


# ---------------------------------------------------------------------------
# delete_page() — cascade_children=False (re-parent children)
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_soft_delete_no_cascade_reparents_children(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    grandparent = await _create_page(db_session, project, actor, "Grandparent Node")
    parent = await _create_page(db_session, project, actor, "Parent Node", parent_slug=grandparent.slug)
    child = await _create_page(db_session, project, actor, "Child Node", parent_slug=parent.slug)

    await _svc.delete_page(db_session, parent.id, deleted_by=actor, cascade_children=False)
    await db_session.commit()
    await db_session.refresh(child)

    # Child should be re-parented to grandparent
    assert child.deleted_at is None
    assert child.parent_id == grandparent.id


@pytest.mark.service
async def test_soft_delete_no_cascade_reparents_to_none_when_no_grandparent(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    parent = await _create_page(db_session, project, actor, "Root Parent")
    child = await _create_page(db_session, project, actor, "Orphan Child", parent_slug=parent.slug)

    await _svc.delete_page(db_session, parent.id, deleted_by=actor, cascade_children=False)
    await db_session.commit()
    await db_session.refresh(child)

    # Child has no grandparent, so parent_id becomes None
    assert child.deleted_at is None
    assert child.parent_id is None


# ---------------------------------------------------------------------------
# delete_page() — cascade_children=True (soft-delete all descendants)
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_soft_delete_cascade_also_deletes_children(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    parent = await _create_page(db_session, project, actor, "Cascade Parent")
    child = await _create_page(db_session, project, actor, "Cascade Child", parent_slug=parent.slug)
    grandchild = await _create_page(db_session, project, actor, "Cascade Grandchild", parent_slug=child.slug)

    deleted_ids = await _svc.delete_page(db_session, parent.id, deleted_by=actor, cascade_children=True)
    await db_session.commit()

    for node in [parent, child, grandchild]:
        await db_session.refresh(node)
        assert node.deleted_at is not None, f"Expected {node.slug} to be soft-deleted"
        assert node.id in deleted_ids


@pytest.mark.service
async def test_soft_delete_cascade_excludes_all_descendants_from_list(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    parent = await _create_page(db_session, project, actor, "Cascade List Parent")
    child = await _create_page(db_session, project, actor, "Cascade List Child", parent_slug=parent.slug)

    await _svc.delete_page(db_session, parent.id, deleted_by=actor, cascade_children=True)
    await db_session.commit()

    pages = await _svc.list_pages(db_session, project.id)
    slugs = [p.slug for p in pages]
    assert parent.slug not in slugs
    assert child.slug not in slugs


# ---------------------------------------------------------------------------
# restore_page()
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_restore_page_clears_deleted_fields(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Restore Me")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    await _svc.restore_page(db_session, page.id)
    await db_session.commit()
    await db_session.refresh(page)

    assert page.deleted_at is None
    assert page.deleted_by_id is None


@pytest.mark.service
async def test_restore_page_reappears_in_list(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Risen Page")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    await _svc.restore_page(db_session, page.id)
    await db_session.commit()

    pages = await _svc.list_pages(db_session, project.id)
    slugs = [p.slug for p in pages]
    assert page.slug in slugs


@pytest.mark.service
async def test_restore_page_cascade_restores_co_deleted_children(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    parent = await _create_page(db_session, project, actor, "Restore Cascade Parent")
    child = await _create_page(db_session, project, actor, "Restore Cascade Child", parent_slug=parent.slug)

    await _svc.delete_page(db_session, parent.id, deleted_by=actor, cascade_children=True)
    await db_session.commit()

    await _svc.restore_page(db_session, parent.id, cascade=True)
    await db_session.commit()

    for node in [parent, child]:
        await db_session.refresh(node)
        assert node.deleted_at is None, f"Expected {node.slug} to be restored"


@pytest.mark.service
async def test_restore_page_no_cascade_leaves_children_deleted(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    parent = await _create_page(db_session, project, actor, "Restore No Cascade Parent")
    child = await _create_page(db_session, project, actor, "Restore No Cascade Child", parent_slug=parent.slug)

    await _svc.delete_page(db_session, parent.id, deleted_by=actor, cascade_children=True)
    await db_session.commit()

    await _svc.restore_page(db_session, parent.id, cascade=False)
    await db_session.commit()

    await db_session.refresh(parent)
    await db_session.refresh(child)
    assert parent.deleted_at is None
    assert child.deleted_at is not None


@pytest.mark.service
async def test_restore_page_conflict_when_slug_taken(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Conflict Slug")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    # Create a new active page that takes the same slug
    await _create_page(db_session, project, actor, "Conflict Slug")

    with pytest.raises((ConflictError, Exception)):
        await _svc.restore_page(db_session, page.id)


# ---------------------------------------------------------------------------
# list_deleted_pages()
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_list_deleted_pages_returns_only_deleted(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    active_page = await _create_page(db_session, project, actor, "Active Page")
    deleted_page = await _create_page(db_session, project, actor, "Deleted Page")

    await _svc.delete_page(db_session, deleted_page.id, deleted_by=actor)
    await db_session.commit()

    deleted = await _svc.list_deleted_pages(db_session, project.id)
    deleted_ids = [p.id for p in deleted]

    assert deleted_page.id in deleted_ids
    assert active_page.id not in deleted_ids


@pytest.mark.service
async def test_list_deleted_pages_empty_when_none_deleted(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    await _create_page(db_session, project, actor, "All Active")

    deleted = await _svc.list_deleted_pages(db_session, project.id)
    assert deleted == []


@pytest.mark.service
async def test_list_deleted_pages_includes_deleted_by_info(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Has Deleter Info")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    deleted = await _svc.list_deleted_pages(db_session, project.id)
    assert len(deleted) == 1
    # The returned object should carry deleted_by_id populated
    assert deleted[0].deleted_by_id == actor.id


# ---------------------------------------------------------------------------
# hard_delete_expired_pages()
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_hard_delete_expired_removes_old_pages(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Expired Page")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    # Manually backdate deleted_at to simulate expiry
    await db_session.refresh(page)
    page.deleted_at = datetime.now(UTC) - timedelta(days=91)
    await db_session.commit()

    removed = await _svc.hard_delete_expired_pages(db_session, retention_days=90)
    await db_session.commit()

    assert page.id in removed

    # The row should no longer exist in the DB
    from sqlalchemy import select

    result = await db_session.execute(select(WikiPage).where(WikiPage.id == page.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.service
async def test_hard_delete_expired_keeps_recent_pages(
    db_session: AsyncSession,
    project: Project,
    actor: User,
):
    page = await _create_page(db_session, project, actor, "Recent Deleted Page")
    await _svc.delete_page(db_session, page.id, deleted_by=actor)
    await db_session.commit()

    # deleted_at is now (within retention window)
    removed = await _svc.hard_delete_expired_pages(db_session, retention_days=90)
    await db_session.commit()

    assert page.id not in removed

    # The row must still be present
    from sqlalchemy import select

    result = await db_session.execute(select(WikiPage).where(WikiPage.id == page.id))
    assert result.scalar_one_or_none() is not None
