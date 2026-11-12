"""What an import must keep: records the source still holds, and text it did not write.

Marked ``serial`` like the other bundle loads, because a load writes
instance-wide rows (statuses, activities, accounts) that other tests insert too.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from specivo.models.time_entry import TimeEntry, TimeEntryActivity
from tests.fixtures.import_bundle import LOOKUPS, project, user, write_bundle

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
