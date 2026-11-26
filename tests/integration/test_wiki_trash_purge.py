"""Purging the wiki trash: ``WikiService.hard_delete_expired_pages``.

Pages in the trash longer than the retention period are deleted for good,
together with everything that hangs off them: their versions, comments,
watchers, tags and stored links go by the database's cascades, and their
attachments (which have no foreign key to the page) are deleted here, with the
files they point to returned so the caller can remove them after the commit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.entity_link import EntityLink
from specivo.models.journal import Journal
from specivo.models.project import EnabledModule, Project
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.models.watcher import Watcher
from specivo.models.wiki import WikiContent, WikiPage
from specivo.services import attachment_service
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = WikiService()


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(attachment_service, "_upload_dir", tmp_path)
    return tmp_path


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="WTPURGE", identifier="wiki-trash-purge")
    db_session.add(proj)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=proj.id, name="wiki"))
    await db_session.commit()
    return proj


@pytest.fixture
async def actor(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="wiki_trash_purge_actor", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


async def _page(db: AsyncSession, project: Project, user: User, title: str, parent: str | None = None) -> WikiPage:
    page, _ = await _svc.create_page(db, project.id, title, "first", user, parent_slug=parent)
    await db.commit()
    return page


async def _trash(db: AsyncSession, page: WikiPage, user: User, days_ago: int, *, cascade: bool = False) -> None:
    """Move *page* to the trash and backdate the deletion by *days_ago* days."""
    ids = await _svc.delete_page(db, page.id, deleted_by=user, cascade_children=cascade)
    await db.execute(
        update(WikiPage).where(WikiPage.id.in_(ids)).values(deleted_at=datetime.now(UTC) - timedelta(days=days_ago))
    )
    await db.commit()


async def _attachment(db: AsyncSession, upload_dir: Path, user: User, container_type: str, container_id: int) -> Path:
    disk_filename = f"purge-{container_type.lower()}-{container_id}.txt"
    path = upload_dir / disk_filename
    path.write_text("payload")
    db.add(
        Attachment(
            container_type=container_type,
            container_id=container_id,
            filename=f"{container_type}-{container_id}.txt",
            disk_filename=disk_filename,
            filesize=7,
            author_id=user.id,
        )
    )
    await db.commit()
    return path


async def _count(db: AsyncSession, model: type, *where: object) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_purges_an_expired_page_with_all_its_dependent_rows(
    db_session: AsyncSession, project: Project, actor: User, upload_dir: Path
) -> None:
    page = await _page(db_session, project, actor, "Expired Page")
    await _svc.update_page(db_session, page.id, "second", actor, lock_version=page.lock_version)
    await db_session.commit()
    live = await _page(db_session, project, actor, "Live Page")

    page_file = await _attachment(db_session, upload_dir, actor, "WikiPage", page.id)
    comment = Journal(wiki_page_id=page.id, project_id=project.id, user_id=actor.id, notes="a comment", sequence=1)
    db_session.add(comment)
    await db_session.flush()
    comment_file = await _attachment(db_session, upload_dir, actor, "Journal", comment.id)
    db_session.add(Watcher(wiki_page_id=page.id, user_id=actor.id))
    tag = Tag(project_id=project.id, name="purge-tag")
    db_session.add(tag)
    await db_session.flush()
    db_session.add(TagLink(tag_id=tag.id, wiki_page_id=page.id))
    # One link out of the purged page, one link from a live page into it.
    db_session.add(
        EntityLink(
            source_type="wiki_page",
            source_wiki_page_id=page.id,
            source_project_id=project.id,
            kind="wiki_link",
            target_type="wiki_page",
            target_wiki_page_id=live.id,
            target_project_id=project.id,
            target_ref="live-page",
        )
    )
    db_session.add(
        EntityLink(
            source_type="wiki_page",
            source_wiki_page_id=live.id,
            source_project_id=project.id,
            kind="wiki_link",
            target_type="wiki_page",
            target_wiki_page_id=page.id,
            target_project_id=project.id,
            target_ref="expired-page",
        )
    )
    await db_session.commit()
    assert await _count(db_session, WikiContent, WikiContent.page_id == page.id) == 2

    await _trash(db_session, page, actor, days_ago=91)

    result = await _svc.hard_delete_expired_pages(db_session, retention_days=90)
    await db_session.commit()

    assert result.page_ids == [page.id]
    assert sorted(result.files) == sorted([page_file, comment_file])
    assert await _count(db_session, WikiPage, WikiPage.id == page.id) == 0
    assert await _count(db_session, WikiContent, WikiContent.page_id == page.id) == 0
    assert await _count(db_session, Journal, Journal.wiki_page_id == page.id) == 0
    assert await _count(db_session, Watcher, Watcher.wiki_page_id == page.id) == 0
    assert await _count(db_session, TagLink, TagLink.wiki_page_id == page.id) == 0
    assert await _count(db_session, EntityLink, EntityLink.source_wiki_page_id == page.id) == 0
    assert await _count(db_session, Attachment, Attachment.disk_filename.like("purge-%")) == 0
    # The live page's link to the purged page stays, unresolved, and so does the live page.
    inbound = (await db_session.execute(select(EntityLink).where(EntityLink.target_ref == "expired-page"))).scalar_one()
    assert inbound.target_wiki_page_id is None
    assert await _count(db_session, WikiPage, WikiPage.id == live.id) == 1
    # The files stay on disk until the caller has committed.
    assert page_file.exists()
    assert comment_file.exists()


async def test_respects_the_retention_period(
    db_session: AsyncSession, project: Project, actor: User, upload_dir: Path
) -> None:
    page = await _page(db_session, project, actor, "Month Old")
    await _trash(db_session, page, actor, days_ago=30)

    kept = await _svc.hard_delete_expired_pages(db_session, retention_days=90)
    assert kept.page_ids == []
    assert await _count(db_session, WikiPage, WikiPage.id == page.id) == 1

    purged = await _svc.hard_delete_expired_pages(db_session, retention_days=7)
    assert purged.page_ids == [page.id]
    assert await _count(db_session, WikiPage, WikiPage.id == page.id) == 0


async def test_never_touches_live_pages_or_their_attachments(
    db_session: AsyncSession, project: Project, actor: User, upload_dir: Path
) -> None:
    live = await _page(db_session, project, actor, "Old But Live")
    await db_session.execute(
        update(WikiPage).where(WikiPage.id == live.id).values(created_at=datetime.now(UTC) - timedelta(days=400))
    )
    live_file = await _attachment(db_session, upload_dir, actor, "WikiPage", live.id)

    result = await _svc.hard_delete_expired_pages(db_session, retention_days=0)

    assert live.id not in result.page_ids
    assert live_file not in result.files
    assert await _count(db_session, Attachment, Attachment.container_id == live.id) == 1


async def test_a_subpage_still_in_the_trash_outlives_its_purged_parent(
    db_session: AsyncSession, project: Project, actor: User, upload_dir: Path
) -> None:
    parent = await _page(db_session, project, actor, "Old Parent")
    child = await _page(db_session, project, actor, "Young Child", parent=parent.slug)
    await _trash(db_session, parent, actor, days_ago=120, cascade=True)
    # The child comes back on its own (still pointing at its trashed parent), then is deleted again.
    await _svc.restore_page(db_session, child.id, project_id=project.id, cascade=False)
    await db_session.commit()
    await _trash(db_session, child, actor, days_ago=5)
    assert (await db_session.scalar(select(WikiPage.parent_id).where(WikiPage.id == child.id))) == parent.id

    result = await _svc.hard_delete_expired_pages(db_session, retention_days=90)
    await db_session.commit()

    assert result.page_ids == [parent.id]
    stmt = select(WikiPage.parent_id, WikiPage.deleted_at).where(WikiPage.id == child.id)
    row = (await db_session.execute(stmt)).one()
    assert row.parent_id is None
    assert row.deleted_at is not None
