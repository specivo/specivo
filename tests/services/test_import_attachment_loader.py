"""Service tests for importing attached files.

This is the one loader that touches a filesystem outside the database, so the
tests cover both what happens when the bytes are there and what happens when
they are not.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from specivo.importers.core.ir import (
    ContainerKind,
    EntityType,
    IRAttachment,
    IRIssue,
    IRLookups,
    IRPriority,
    IRProject,
    IRStatus,
    IRTracker,
    IRUser,
    IRWikiPage,
    IRWikiVersion,
)
from specivo.importers.load.attachment_loader import (
    NOTE_MISSING_FILES,
    NOTE_UNUSUAL_TYPES,
    load_issue_attachments,
    load_wiki_attachments,
)
from specivo.importers.load.issue_loader import load_issues
from specivo.importers.load.lookup_loader import load_lookups
from specivo.importers.load.project_loader import load_projects
from specivo.importers.load.user_loader import load_users
from specivo.importers.load.wiki_loader import load_wiki_pages
from specivo.models.attachment import Attachment
from specivo.models.user import User
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

SAMPLE = b"log line one\nlog line two\n"


class AttachAdapter(FakeAdapter):
    """Adapter with its own source system.

    The import service account's login comes from it, and test modules run in
    parallel: two inserting the same login in uncommitted transactions block on
    the unique index.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("source_system", "redmineattach")
        super().__init__(**kwargs)


@pytest.fixture
def files_dir(tmp_path):
    """A source storage directory holding one file in each on-disk layout."""
    dated = tmp_path / "2026" / "03"
    dated.mkdir(parents=True)
    (dated / "aaa111.txt").write_bytes(SAMPLE)
    (tmp_path / "bbb222.txt").write_bytes(SAMPLE)
    return tmp_path


@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    """Point Specivo's own attachment storage at a scratch directory."""
    import specivo.services.attachment_service as service

    target = tmp_path / "specivo-attachments"
    target.mkdir()
    monkeypatch.setattr(service, "_upload_dir", target)
    return target


def _attachment(ref: str = "800", **overrides) -> IRAttachment:
    data = {
        "source_ref": ref,
        "container_kind": ContainerKind.ISSUE,
        "container_ref": "100",
        "filename": "server.log",
        "storage_key": "2026/03/aaa111.txt",
        "content_type": "text/plain",
        "filesize": 999,
        "description": "The log",
        "author_ref": "7",
        "created_at": datetime(2020, 6, 1, 12, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return IRAttachment(**data)


def _adapter(files_dir, **kwargs) -> AttachAdapter:
    base = {
        "lookups": IRLookups(
            statuses=[IRStatus(source_ref="1", name="New", category="backlog")],
            trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1")],
            priorities=[IRPriority(source_ref="4", name="Normal", is_default=True)],
        ),
        "users": [IRUser(source_ref="7", login="attach_alex", display_name="Alex", email="attach_alex@example.org")],
        "projects": [IRProject(source_ref="1", identifier="attachtest", name="Attach Test")],
        "issues": [
            IRIssue(
                source_ref="100",
                project_ref="1",
                tracker_ref="1",
                status_ref="1",
                priority_ref="4",
                subject="Crash on start",
                author_ref="7",
            )
        ],
        "files_dir": files_dir,
    }
    base.update(kwargs)
    return AttachAdapter(**base)


@pytest_asyncio.fixture
async def loaded(make_context):
    async def _load(adapter: AttachAdapter, **options):
        ctx = make_context(adapter, **options)
        await load_lookups(ctx)
        await load_users(ctx)
        await load_projects(ctx)
        await load_issues(ctx)
        return ctx

    return _load


class TestIssueAttachments:
    async def test_file_is_copied_and_recorded(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment()]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        attachment = await db_session.get(Attachment, attachment_id)
        assert attachment.filename == "server.log"
        assert attachment.description == "The log"
        assert (attachment_dir / attachment.disk_filename).read_bytes() == SAMPLE

    async def test_hash_and_size_come_from_the_bytes(self, db_session, loaded, files_dir, attachment_dir):
        """The source's own digest and size are not trusted."""
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(filesize=999)]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        attachment = await db_session.get(Attachment, attachment_id)
        assert attachment.filesize == len(SAMPLE)
        assert attachment.content_hash == hashlib.sha256(SAMPLE).hexdigest()

    async def test_flat_layout_is_found(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(storage_key="bbb222.txt")]))
        await load_issue_attachments(ctx)

        assert await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800") is not None

    async def test_attached_to_the_right_issue(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment()]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        attachment = await db_session.get(Attachment, attachment_id)
        assert attachment.container_type == "Issue"
        assert attachment.container_id == await ctx.id_map.get(db_session, EntityType.ISSUE, "100")

    async def test_upload_date_is_restored(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment()]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        attachment = await db_session.get(Attachment, attachment_id)
        assert attachment.created_at == datetime(2020, 6, 1, 12, 0, tzinfo=UTC)

    async def test_unknown_uploader_falls_back_to_the_import_account(
        self, db_session, loaded, files_dir, attachment_dir
    ):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(author_ref="999")]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        attachment = await db_session.get(Attachment, attachment_id)
        assert (await db_session.get(User, attachment.author_id)).is_service_account is True

    async def test_two_files_of_the_same_name_are_kept_apart(self, db_session, loaded, files_dir, attachment_dir):
        """Different content under one name must not overwrite anything."""
        (files_dir / "ccc333.txt").write_bytes(b"different content")
        attachments = [_attachment("800"), _attachment("801", storage_key="ccc333.txt")]
        ctx = await loaded(_adapter(files_dir, attachments=attachments))
        await load_issue_attachments(ctx)

        rows = (await db_session.execute(select(Attachment))).scalars().all()
        assert len(rows) == 2
        assert len({row.disk_filename for row in rows}) == 2
        assert len({row.filename for row in rows}) == 2


class TestMissingFiles:
    async def test_missing_file_is_skipped_and_listed(self, db_session, loaded, files_dir, attachment_dir):
        """A row offering a download that fails is worse than a reported absence."""
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(storage_key="2026/03/gone.txt")]))
        await load_issue_attachments(ctx)

        count = (await db_session.execute(select(func.count()).select_from(Attachment))).scalar_one()
        assert count == 0
        assert ctx.summary.notes[NOTE_MISSING_FILES]
        assert any("not found on disk" in w.message for w in ctx.summary.warnings)

    async def test_the_attempted_path_is_reported(self, db_session, loaded, files_dir, attachment_dir):
        """That is how an operator spots the wrong volume being mounted."""
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(storage_key="2026/03/gone.txt")]))
        await load_issue_attachments(ctx)
        assert "gone.txt" in ctx.summary.notes[NOTE_MISSING_FILES][0]

    async def test_traversal_is_refused(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(storage_key="../../etc/passwd")]))
        await load_issue_attachments(ctx)

        count = (await db_session.execute(select(func.count()).select_from(Attachment))).scalar_one()
        assert count == 0
        assert any("could not be resolved" in w.message for w in ctx.summary.warnings)

    async def test_attachment_on_a_missing_container_is_skipped(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(container_ref="999")]))
        await load_issue_attachments(ctx)
        assert any("was not imported" in w.message for w in ctx.summary.warnings)


class TestContentTypes:
    async def test_a_type_no_longer_accepted_is_still_imported(self, db_session, loaded, files_dir, attachment_dir):
        """The allowlist governs uploads now; history predates it."""
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(content_type="application/x-msdownload")]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        assert attachment_id is not None
        assert ctx.summary.notes[NOTE_UNUSUAL_TYPES]

    async def test_an_accepted_type_is_not_listed(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment()]))
        await load_issue_attachments(ctx)
        assert NOTE_UNUSUAL_TYPES not in ctx.summary.notes

    async def test_missing_type_becomes_a_generic_one(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment(content_type=None)]))
        await load_issue_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        assert (await db_session.get(Attachment, attachment_id)).content_type == "application/octet-stream"


class TestWikiAttachments:
    async def test_file_is_attached_to_the_page(self, db_session, loaded, files_dir, attachment_dir):
        pages = [
            IRWikiPage(
                source_ref="10",
                project_ref="1",
                title="Home",
                versions=[IRWikiVersion(source_ref="100", version=1, text="Body", author_ref="7")],
            )
        ]
        attachments = [_attachment(container_kind=ContainerKind.WIKI_PAGE, container_ref="10")]
        ctx = await loaded(_adapter(files_dir, wiki_pages=pages, attachments=attachments))
        await load_wiki_pages(ctx)
        await load_wiki_attachments(ctx)

        attachment_id = await ctx.id_map.get(db_session, EntityType.ATTACHMENT, "800")
        attachment = await db_session.get(Attachment, attachment_id)
        assert attachment.container_type == "WikiPage"
        assert attachment.container_id == await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")

    async def test_issue_pass_leaves_wiki_files_alone(self, db_session, loaded, files_dir, attachment_dir):
        """Each container is imported alongside the entities it hangs off."""
        attachments = [_attachment(container_kind=ContainerKind.WIKI_PAGE, container_ref="10")]
        ctx = await loaded(_adapter(files_dir, attachments=attachments))
        await load_issue_attachments(ctx)

        count = (await db_session.execute(select(func.count()).select_from(Attachment))).scalar_one()
        assert count == 0


class TestIdempotency:
    async def test_second_run_copies_nothing(self, db_session, loaded, files_dir, attachment_dir):
        ctx = await loaded(_adapter(files_dir, attachments=[_attachment()]))
        await load_issue_attachments(ctx)
        await load_issue_attachments(ctx)

        count = (await db_session.execute(select(func.count()).select_from(Attachment))).scalar_one()
        assert count == 1
        assert ctx.summary.skipped[EntityType.ATTACHMENT] == 1
        assert len(list(attachment_dir.iterdir())) == 1
