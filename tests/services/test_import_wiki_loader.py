"""Service tests for wiki pages, their history, watchers and redirects.

The point of this loader is that a page arrives with its past intact: every
revision, in order, with the author who wrote it and the date they did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from specivo.importers.core.ir import (
    ContainerKind,
    EntityType,
    IRActivity,
    IRLookups,
    IRPriority,
    IRProject,
    IRStatus,
    IRTimeEntry,
    IRTracker,
    IRUser,
    IRWatcher,
    IRWikiPage,
    IRWikiVersion,
)
from specivo.importers.load.lookup_loader import load_lookups
from specivo.importers.load.project_loader import load_projects
from specivo.importers.load.time_entry_loader import load_time_entries
from specivo.importers.load.user_loader import load_users
from specivo.importers.load.wiki_loader import load_wiki_pages, load_wiki_redirects, load_wiki_watchers
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.watcher import Watcher
from specivo.models.wiki import WikiContent, WikiPage, WikiRedirect
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


class WikiAdapter(FakeAdapter):
    """Adapter with its own source system.

    The import service account's login is derived from it, and test modules run
    in parallel: two of them inserting the same login in uncommitted
    transactions block on the unique index.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("source_system", "redminewiki")
        super().__init__(**kwargs)


def _version(ref: str, version: int, text: str, **overrides) -> IRWikiVersion:
    data = {
        "source_ref": ref,
        "version": version,
        "text": text,
        "author_ref": "7",
        "created_at": datetime(2020, 1, version, 9, 0, tzinfo=UTC),
    }
    data.update(overrides)
    return IRWikiVersion(**data)


def _page(ref: str = "10", title: str = "Home", **overrides) -> IRWikiPage:
    data = {
        "source_ref": ref,
        "project_ref": "1",
        "title": title,
        "versions": [_version("100", 1, "h1. Home\n\nFirst draft.")],
    }
    data.update(overrides)
    return IRWikiPage(**data)


def _adapter(**kwargs) -> WikiAdapter:
    base = {
        "lookups": IRLookups(
            statuses=[IRStatus(source_ref="1", name="New", category="backlog")],
            trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1")],
            priorities=[IRPriority(source_ref="4", name="Normal", is_default=True)],
            activities=[IRActivity(source_ref="9", name="Wiki Development", is_default=True)],
        ),
        "users": [IRUser(source_ref="7", login="wiki_alex", display_name="Alex", email="wiki_alex@example.org")],
        "projects": [IRProject(source_ref="1", identifier="wikitest", name="Wiki Test")],
    }
    base.update(kwargs)
    return WikiAdapter(**base)


@pytest_asyncio.fixture
async def loaded(make_context):
    async def _load(adapter: WikiAdapter, **options):
        ctx = make_context(adapter, **options)
        await load_lookups(ctx)
        await load_users(ctx)
        await load_projects(ctx)
        return ctx

    return _load


class TestPages:
    async def test_page_is_created(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        page = await db_session.get(WikiPage, page_id)
        assert page.title == "Home"
        assert page.slug == "home"

    async def test_markup_is_converted(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        content = (
            await db_session.execute(
                select(WikiContent).where(WikiContent.page_id == page_id, WikiContent.version == 1)
            )
        ).scalar_one()
        assert content.text.startswith("# Home")

    async def test_protected_flag_carries_over(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page(protected=True)]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        assert (await db_session.get(WikiPage, page_id)).protected is True

    async def test_child_page_hangs_off_its_parent(self, db_session, loaded):
        pages = [_page("10", "Home"), _page("11", "Guide", parent_ref="10")]
        ctx = await loaded(_adapter(wiki_pages=pages))
        await load_wiki_pages(ctx)

        parent_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        child_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "11")
        assert (await db_session.get(WikiPage, child_id)).parent_id == parent_id

    async def test_page_without_a_title_is_skipped(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page(title="")]))
        await load_wiki_pages(ctx)
        assert any("no title" in w.message for w in ctx.summary.warnings)


class TestHistory:
    def _page_with_history(self) -> IRWikiPage:
        return _page(
            versions=[
                _version("100", 1, "First draft."),
                _version("101", 2, "Second draft.", author_ref="7", comments="Expanded"),
                _version("102", 3, "Third draft."),
            ]
        )

    async def test_every_revision_is_kept(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[self._page_with_history()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        count = (
            await db_session.execute(
                select(func.count()).select_from(WikiContent).where(WikiContent.page_id == page_id)
            )
        ).scalar_one()
        assert count == 3

    async def test_revisions_are_numbered_consecutively(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[self._page_with_history()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        versions = (
            (
                await db_session.execute(
                    select(WikiContent.version).where(WikiContent.page_id == page_id).order_by(WikiContent.version)
                )
            )
            .scalars()
            .all()
        )
        assert versions == [1, 2, 3]

    async def test_gaps_in_the_source_are_closed(self, db_session, loaded):
        """Redmine can leave a gap when a revision is deleted; Specivo reads by
        position, so a gap would look like a missing revision."""
        page = _page(versions=[_version("100", 1, "One"), _version("102", 7, "Two")])
        ctx = await loaded(_adapter(wiki_pages=[page]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        versions = (
            (
                await db_session.execute(
                    select(WikiContent.version).where(WikiContent.page_id == page_id).order_by(WikiContent.version)
                )
            )
            .scalars()
            .all()
        )
        assert versions == [1, 2]

    async def test_each_revision_keeps_its_own_date(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[self._page_with_history()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        first = (
            await db_session.execute(
                select(WikiContent).where(WikiContent.page_id == page_id, WikiContent.version == 1)
            )
        ).scalar_one()
        assert first.created_at == datetime(2020, 1, 1, 9, 0, tzinfo=UTC)

    async def test_revision_comment_is_kept(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[self._page_with_history()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        second = (
            await db_session.execute(
                select(WikiContent).where(WikiContent.page_id == page_id, WikiContent.version == 2)
            )
        ).scalar_one()
        assert second.comments == "Expanded"

    async def test_unknown_author_falls_back_to_the_import_account(self, db_session, loaded):
        page = _page(versions=[_version("100", 1, "One", author_ref="999")])
        ctx = await loaded(_adapter(wiki_pages=[page]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        content = (
            (await db_session.execute(select(WikiContent).where(WikiContent.page_id == page_id))).scalars().first()
        )
        assert (await db_session.get(User, content.author_id)).is_service_account is True

    async def test_history_is_readable_through_the_service(self, db_session, loaded):
        from specivo.services.wiki_service import WikiService

        ctx = await loaded(_adapter(wiki_pages=[self._page_with_history()]))
        await load_wiki_pages(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        history = await WikiService().get_page_history(db_session, page_id)
        assert len(history) == 3


class TestWatchersAndRedirects:
    async def test_wiki_watcher_is_subscribed(self, db_session, loaded):
        watchers = [IRWatcher(container_kind=ContainerKind.WIKI_PAGE, container_ref="10", user_ref="7")]
        ctx = await loaded(_adapter(wiki_pages=[_page()], wiki_watchers=watchers))
        await load_wiki_pages(ctx)
        await load_wiki_watchers(ctx)

        page_id = await ctx.id_map.get(db_session, EntityType.WIKI_PAGE, "10")
        found = (await db_session.execute(select(Watcher).where(Watcher.wiki_page_id == page_id))).scalar_one_or_none()
        assert found is not None

    async def test_redirect_is_recreated_from_the_rename(self, db_session, loaded):
        """A link written against a page's old name keeps working."""
        ctx = await loaded(_adapter(wiki_pages=[_page()], wiki_redirects=[("Old Name", "Home")]))
        await load_wiki_pages(ctx)
        await load_wiki_redirects(ctx)

        redirect = (
            await db_session.execute(select(WikiRedirect).where(WikiRedirect.title_from == "old-name"))
        ).scalar_one()
        assert redirect.redirected_to == "home"

    async def test_redirect_to_itself_is_ignored(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page()], wiki_redirects=[("Home", "Home")]))
        await load_wiki_pages(ctx)
        await load_wiki_redirects(ctx)

        count = (await db_session.execute(select(func.count()).select_from(WikiRedirect))).scalar_one()
        assert count == 0

    async def test_redirects_are_not_duplicated_on_a_second_run(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page()], wiki_redirects=[("Old Name", "Home")]))
        await load_wiki_pages(ctx)
        await load_wiki_redirects(ctx)
        await load_wiki_redirects(ctx)

        count = (await db_session.execute(select(func.count()).select_from(WikiRedirect))).scalar_one()
        assert count == 1


class TestTimeEntries:
    def _entry(self, ref: str = "300", **overrides) -> IRTimeEntry:
        data = {
            "source_ref": ref,
            "project_ref": "1",
            "hours": Decimal("2.5"),
            "spent_on": datetime(2020, 5, 1).date(),
            "user_ref": "7",
            "activity_ref": "9",
            "comments": "Investigating",
            "created_at": datetime(2020, 5, 1, 17, 0, tzinfo=UTC),
        }
        data.update(overrides)
        return IRTimeEntry(**data)

    async def test_entry_is_created(self, db_session, loaded):
        ctx = await loaded(_adapter(time_entries=[self._entry()]))
        await load_time_entries(ctx)

        entry_id = await ctx.id_map.get(db_session, EntityType.TIME_ENTRY, "300")
        entry = await db_session.get(TimeEntry, entry_id)
        assert entry.hours == Decimal("2.50")
        assert entry.comments == "Investigating"
        assert entry.user_id == await ctx.id_map.get(db_session, EntityType.USER, "7")

    async def test_float_noise_is_rounded_without_a_warning(self, db_session, loaded):
        """Redmine returns 7.5 as 7.499999999999999; that is not a real change."""
        ctx = await loaded(_adapter(time_entries=[self._entry(hours=Decimal("7.499999999999999"))]))
        await load_time_entries(ctx)

        entry_id = await ctx.id_map.get(db_session, EntityType.TIME_ENTRY, "300")
        assert (await db_session.get(TimeEntry, entry_id)).hours == Decimal("7.50")
        assert ctx.summary.warnings == []

    async def test_a_real_rounding_is_reported(self, db_session, loaded):
        """On a billable instance this is somebody's invoice."""
        ctx = await loaded(_adapter(time_entries=[self._entry(hours=Decimal("2.055"))]))
        await load_time_entries(ctx)
        assert any("rounded" in w.message for w in ctx.summary.warnings)

    async def test_entry_rounding_to_zero_is_skipped(self, db_session, loaded):
        """Inventing a duration would put time on a record nobody logged."""
        ctx = await loaded(_adapter(time_entries=[self._entry(hours=Decimal("0.001"))]))
        await load_time_entries(ctx)

        assert await ctx.id_map.get(db_session, EntityType.TIME_ENTRY, "300") is None
        assert any("zero hours" in w.message for w in ctx.summary.warnings)

    async def test_timestamps_are_restored(self, db_session, loaded):
        ctx = await loaded(_adapter(time_entries=[self._entry()]))
        await load_time_entries(ctx)

        entry_id = await ctx.id_map.get(db_session, EntityType.TIME_ENTRY, "300")
        assert (await db_session.get(TimeEntry, entry_id)).created_at == datetime(2020, 5, 1, 17, 0, tzinfo=UTC)

    async def test_missing_activity_is_reported(self, db_session, loaded):
        ctx = await loaded(_adapter(time_entries=[self._entry(activity_ref="999")]))
        await load_time_entries(ctx)
        assert any("activity was not imported" in w.message for w in ctx.summary.warnings)

    async def test_second_run_creates_no_duplicate(self, db_session, loaded):
        ctx = await loaded(_adapter(time_entries=[self._entry()]))
        await load_time_entries(ctx)
        await load_time_entries(ctx)

        count = (await db_session.execute(select(func.count()).select_from(TimeEntry))).scalar_one()
        assert count == 1


class TestIdempotency:
    async def test_second_wiki_run_creates_nothing(self, db_session, loaded):
        ctx = await loaded(_adapter(wiki_pages=[_page()]))
        await load_wiki_pages(ctx)
        await load_wiki_pages(ctx)

        count = (
            await db_session.execute(select(func.count()).select_from(WikiPage).where(WikiPage.title == "Home"))
        ).scalar_one()
        assert count == 1
        assert ctx.summary.skipped[EntityType.WIKI_PAGE] == 1
