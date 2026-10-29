"""Loading a bundle written with the migration SDK, end to end.

The bundle comes from ``tests/fixtures/migration_sdk.py``, which writes it with
``tools/migration-sdk`` the way an importer outside core would. These tests check
that what the SDK writes is not only valid but loads as intended.

Marked ``serial`` for the same reason as the other bundle imports: a load
writes instance-wide rows (roles, statuses, activities, accounts).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.ir import EntityType
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import Project
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.wiki import WikiContent, WikiPage
from tests.fixtures.migration_sdk import LOG, write_sdk_bundle

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.serial,
]


async def _issue(session: AsyncSession, subject: str) -> Issue:
    return (await session.execute(select(Issue).where(Issue.subject == subject))).scalar_one()


async def _user(session: AsyncSession, login: str) -> User:
    return (await session.execute(select(User).where(User.login == login))).scalar_one()


@pytest.fixture(params=["bundle", "bundle.zip"])
def sdk_bundle(request, tmp_path):
    return write_sdk_bundle(tmp_path / request.param)


class TestAnSdkBundle:
    @pytest.fixture
    async def imported(self, db_session, build_bundle_import, sdk_bundle):
        return await build_bundle_import(sdk_bundle).run()

    async def test_every_record_is_created_without_warnings(self, imported):
        assert [w.message for w in imported.warnings] == []
        created = imported.created
        assert created[EntityType.USER] == 2
        assert created[EntityType.GROUP] == 1
        assert created[EntityType.PROJECT] == 2
        assert created[EntityType.ISSUE] == 3
        assert created[EntityType.JOURNAL] == 1
        assert created[EntityType.RELATION] == 1
        assert created[EntityType.WIKI_PAGE] == 2
        assert created[EntityType.ATTACHMENT] == 3
        assert created[EntityType.TIME_ENTRY] == 1
        assert created[EntityType.MEMBERSHIP] == 1

    async def test_issue_fields_and_placeholders(self, db_session, imported):
        issue = await _issue(db_session, "SDK login fails")
        key = (await db_session.execute(select(Project.key).where(Project.identifier == "sdk-web"))).scalar_one()
        assert issue.description == f"Split into {key}-2; see [[Home]]. Log: [the log](deploy.log)."
        assert issue.created_at == datetime(2024, 2, 1, 10, 0, tzinfo=UTC)
        assert issue.estimated_hours == Decimal("2.5")
        assert issue.remaining_estimate == Decimal("0.75")
        assert (await db_session.get(User, issue.author_id)).login == "sdk_bob"
        bob = await _user(db_session, "sdk_bob")
        assert issue.issue_metadata == {"sdk_severity": "High", "sdk_reviewer": bob.id}
        child = await _issue(db_session, "SDK session cookie is not cleared")
        assert child.parent_id == issue.id

    async def test_a_timestamp_given_with_another_offset(self, db_session, imported):
        bob = await _user(db_session, "sdk_bob")
        assert bob.created_at == datetime(2023, 1, 10, 1, 0, tzinfo=UTC)

    async def test_the_comment_and_its_status_change(self, db_session, imported):
        issue = await _issue(db_session, "SDK login fails")
        comment = (
            await db_session.execute(select(Journal).where(Journal.issue_id == issue.id, Journal.notes.is_not(None)))
        ).scalar_one()
        assert comment.notes == "Reproduced, @sdk_bob."
        assert comment.created_at == datetime(2024, 2, 2, 10, 0, tzinfo=UTC)
        assert (await db_session.get(User, comment.user_id)).login == "sdk_alice"
        detail = (
            await db_session.execute(select(JournalDetail).where(JournalDetail.journal_id == comment.id))
        ).scalar_one()
        assert (detail.prop_key, detail.old_value, detail.new_value) == ("status_id", "open", "doing")

    async def test_wiki_history(self, db_session, imported):
        home = (await db_session.execute(select(WikiPage).where(WikiPage.title == "Home"))).scalar_one()
        texts = (
            await db_session.execute(
                select(WikiContent.version, WikiContent.text)
                .where(WikiContent.page_id == home.id)
                .order_by(WikiContent.version)
            )
        ).all()
        assert [version for version, _ in texts] == [1, 2]
        assert texts[1][1] == "# Home\n\nSee [[Runbook]] and [the notes](notes.txt)."

    async def test_attachments_share_one_stored_content(self, db_session, imported):
        hashes = {a.filename: a.content_hash for a in (await db_session.execute(select(Attachment))).scalars()}
        assert hashes["deploy.log"] == hashes["deploy copy.log"]
        assert (
            len(LOG)
            == (
                await db_session.execute(select(Attachment.filesize).where(Attachment.filename == "deploy.log"))
            ).scalar_one()
        )

    async def test_time_entries(self, db_session, imported):
        assert (await db_session.execute(select(TimeEntry.hours))).scalars().all() == [Decimal("1.25")]

    async def test_loading_it_again_creates_nothing(self, db_session, build_bundle_import, sdk_bundle, imported):
        before = (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one()
        again = await build_bundle_import(sdk_bundle, continue_import=True).run()
        assert sum(again.created.values()) == 0
        assert (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one() == before
