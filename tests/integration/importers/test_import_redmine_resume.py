"""Resuming an interrupted import of the real fixture.

An import of a large instance runs for a long time, so it has to be
interruptible: whatever landed stays, and a second invocation carries on rather
than starting again or duplicating.

Requires the fixture:

    make redmine-fixture-up && make redmine-fixture-seed

Marked ``serial``: an import writes instance-wide rows — roles, statuses,
activities, accounts — that other tests insert too. Run in parallel with the
rest of the suite, two workers end up waiting on each other's uncommitted index
entries and the run deadlocks. ``make test-serial`` runs these.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import ImportPhase
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.project import Project
from specivo.models.user import User

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.redmine,
    pytest.mark.serial,
    pytest.mark.slow,
]


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestResume:
    async def test_a_stopped_run_keeps_what_it_finished(self, db_session, build_import):
        await build_import(merge_duplicate_statuses=True, stop_after_phase=ImportPhase.USERS).run()

        assert await _count(db_session, User) > 0
        assert await _count(db_session, Project) == 0
        assert await _count(db_session, Issue) == 0

    async def test_resuming_finishes_the_import(self, db_session, build_import):
        first = await build_import(merge_duplicate_statuses=True, stop_after_phase=ImportPhase.USERS).run()
        second = await build_import(merge_duplicate_statuses=True, resume_run_id=first.run_id).run()

        assert await _count(db_session, Project) == 3
        assert await _count(db_session, Issue) == 5
        assert second.created[EntityType.PROJECT] == 3

    async def test_resuming_does_not_redo_finished_phases(self, db_session, build_import):
        first = await build_import(merge_duplicate_statuses=True, stop_after_phase=ImportPhase.USERS).run()
        before = await _count(db_session, User)

        second = await build_import(merge_duplicate_statuses=True, resume_run_id=first.run_id).run()

        assert await _count(db_session, User) == before
        assert second.skipped[EntityType.USER] > 0
        assert second.created[EntityType.USER] == 0

    async def test_stopping_midway_through_issues_still_resumes(self, db_session, build_import):
        """The phase after issues is where a half-imported instance is most
        likely to be interrupted, since issues are the slow part."""
        first = await build_import(merge_duplicate_statuses=True, stop_after_phase=ImportPhase.ISSUES).run()
        assert await _count(db_session, Issue) == 5
        assert await _count(db_session, Journal) > 0  # the description baselines

        second = await build_import(merge_duplicate_statuses=True, resume_run_id=first.run_id).run()

        assert await _count(db_session, Issue) == 5
        assert second.skipped[EntityType.ISSUE] == 5
        assert second.created[EntityType.JOURNAL] > 0

    async def test_a_fresh_run_id_also_continues_rather_than_duplicating(self, db_session, build_import):
        """The identifier map is keyed on the source, not the run, so an
        operator who lost the run id is not stuck."""
        await build_import(merge_duplicate_statuses=True, stop_after_phase=ImportPhase.PROJECTS).run()
        summary = await build_import(merge_duplicate_statuses=True).run()

        assert await _count(db_session, Project) == 3
        assert summary.skipped[EntityType.PROJECT] == 3

    async def test_the_service_account_is_not_recreated(self, db_session, build_import):
        first = await build_import(merge_duplicate_statuses=True, stop_after_phase=ImportPhase.USERS).run()
        await build_import(merge_duplicate_statuses=True, resume_run_id=first.run_id).run()

        count = (
            await db_session.execute(select(func.count()).select_from(User).where(User.is_service_account.is_(True)))
        ).scalar_one()
        assert count == 1
