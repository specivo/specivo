"""What an import must keep: records the source still holds, and text it did not write.

Marked ``serial`` like the other bundle loads, because a load writes
instance-wide rows (statuses, activities, accounts) that other tests insert too.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.time_entry import TimeEntry, TimeEntryActivity
from tests.fixtures.import_bundle import FULL, LOOKUPS, issue, project, user, write_bundle

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.serial,
]


class TestRetiredActivities:
    """Time logged against an activity the source has since retired."""

    def _bundle(self, root):
        lookups = {
            **LOOKUPS,
            "activities": [
                *LOOKUPS["activities"],
                {"source_ref": "retired", "name": "Retired consulting", "active": False},
            ],
        }
        return write_bundle(
            root,
            lookups=lookups,
            users=[user("u1")],
            projects=[project("p1", identifier="retired-time")],
            time_entries=[
                {
                    "source_ref": "t-old",
                    "project_ref": "p1",
                    "user_ref": "u1",
                    "activity_ref": "retired",
                    "hours": "2.5",
                    "spent_on": "2019-05-02",
                    "comments": "Workshop",
                },
                {
                    "source_ref": "t-new",
                    "project_ref": "p1",
                    "user_ref": "u1",
                    "activity_ref": "dev",
                    "hours": "1",
                    "spent_on": "2024-01-10",
                },
            ],
        )

    async def test_time_against_a_retired_activity_is_imported(self, db_session, build_bundle_import, tmp_path):
        summary = await build_bundle_import(self._bundle(tmp_path / "b")).run()

        rows = (
            await db_session.execute(
                select(TimeEntry.spent_on, TimeEntry.hours, TimeEntryActivity.name, TimeEntryActivity.active)
                .join(TimeEntryActivity, TimeEntryActivity.id == TimeEntry.activity_id)
                .order_by(TimeEntry.spent_on)
            )
        ).all()
        assert rows == [
            (date(2019, 5, 2), Decimal("2.50"), "Retired consulting", False),
            (date(2024, 1, 10), Decimal("1.00"), "Development", True),
        ]
        assert not [w for w in summary.warnings if w.message.startswith("Time entry was rejected")]


class TestPlaceholderRewriteScope:
    """The rewrite expands what this run wrote, never text somebody typed after an earlier import."""

    TYPED = "Same cause as {{issue:web-2}}, see the log."

    def _later(self, root: Path) -> Path:
        return write_bundle(
            root,
            projects=[project("p-b", identifier="bundleb")],
            issues=[issue("b-1", "p-b", description="Follows {{issue:web-1}}.")],
        )

    async def _web_1(self, session) -> Issue:
        return (
            await session.execute(select(Issue).where(Issue.subject == "Login fails after the session times out"))
        ).scalar_one()

    async def test_text_typed_into_an_imported_issue_is_left_alone(self, db_session, build_bundle_import, tmp_path):
        await build_bundle_import(FULL).run()
        web_1 = await self._web_1(db_session)
        # Somebody edits the imported issue's description after the import, and
        # the edit leaves its change in the history, as an edit in Specivo does.
        before = web_1.description
        await db_session.execute(update(Issue).where(Issue.id == web_1.id).values(description=self.TYPED))
        sequence = (
            await db_session.execute(select(func.max(Journal.sequence)).where(Journal.issue_id == web_1.id))
        ).scalar_one()
        journal = Journal(
            issue_id=web_1.id,
            project_id=web_1.project_id,
            user_id=web_1.author_id,
            notes="Typed by hand: {{issue:web-2}}",
            sequence=sequence + 1,
        )
        db_session.add(journal)
        await db_session.flush()
        change = JournalDetail(
            journal_id=journal.id,
            property="attr",
            prop_key="description",
            old_value=before,
            new_value=self.TYPED,
        )
        db_session.add(change)
        await db_session.commit()

        second = await build_bundle_import(self._later(tmp_path / "b"), continue_import=True).run()

        await db_session.refresh(web_1)
        await db_session.refresh(change)
        await db_session.refresh(journal)
        assert web_1.description == self.TYPED
        assert change.new_value == self.TYPED
        assert journal.notes == "Typed by hand: {{issue:web-2}}"
        # This run's own text is still expanded against the earlier import.
        added = (await db_session.execute(select(Issue).where(Issue.subject == "Issue b-1"))).scalar_one()
        assert added.description == "Follows BUNDLEWEB-1."
        assert not [w for w in second.warnings if w.message.startswith("Placeholders written as plain text")]
