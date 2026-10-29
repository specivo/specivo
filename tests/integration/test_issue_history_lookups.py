"""An issue's history names statuses, trackers and priorities as they are now.

The dropdowns read them from a per-process cache. The history must not: an
import, or an admin working through another worker, creates or renames them
where this worker's cache cannot see, and the history would show raw ids.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import lookup_cache
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import EnabledModule
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


def _change(label: str, old: str, new: str) -> str:
    return (
        f'<strong class="sp-change-label">{label}</strong> changed from '
        f'<span class="sp-change-old">{old}</span> to <span class="sp-change-new">{new}</span>'
    )


@pytest.mark.parametrize("partial", [False, True], ids=["page", "partial"])
async def test_history_names_lookups_this_worker_has_not_cached(
    admin_client: AsyncClient, db_session: AsyncSession, partial: bool
) -> None:
    status = StatusFactory.build(name="HistlkNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="HistlkBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="HistlkNormal", position=1)
    project = ProjectFactory.build(key="HISTLK", identifier="histlk", name="History lookups")
    db_session.add_all([tracker, priority, project])
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=admin_client.state.user.id,
    )
    db_session.add(issue)
    await db_session.commit()

    # This worker caches the lookups as they are now.
    lookup_cache.invalidate_lookups()
    assert (await admin_client.get(f"/issue/{issue.display_key}/")).status_code == 200

    # Then records appear that it never cached, as an import or another worker makes them;
    # an inactive priority is no longer offered, but history still names it.
    later_status = StatusFactory.build(name="HistlkTriaged", position=2, category="active")
    later_tracker = TrackerFactory.build(name="HistlkChore", default_status_id=status.id)
    retired = PriorityFactory.build(name="HistlkRetired", position=2, active=False)
    db_session.add_all([later_status, later_tracker, retired])
    await db_session.flush()
    journal = Journal(issue_id=issue.id, project_id=project.id, user_id=admin_client.state.user.id, sequence=5)
    db_session.add(journal)
    await db_session.flush()
    db_session.add_all(
        [
            JournalDetail(
                journal_id=journal.id,
                property="attr",
                prop_key="status_id",
                old_value=str(status.id),
                new_value=str(later_status.id),
            ),
            JournalDetail(
                journal_id=journal.id,
                property="attr",
                prop_key="tracker_id",
                old_value=str(tracker.id),
                new_value=str(later_tracker.id),
            ),
            JournalDetail(
                journal_id=journal.id,
                property="attr",
                prop_key="priority_id",
                old_value=str(retired.id),
                new_value=str(priority.id),
            ),
        ]
    )
    await db_session.commit()

    url = f"/partials/issues/{issue.display_key}/activity/" if partial else f"/issue/{issue.display_key}/"
    resp = await admin_client.get(url)
    assert resp.status_code == 200
    text = " ".join(resp.text.split())
    assert _change("Status", "HistlkNew", "HistlkTriaged") in text
    assert _change("Tracker", "HistlkBug", "HistlkChore") in text
    assert _change("Priority", "HistlkRetired", "HistlkNormal") in text
    lookup_cache.invalidate_lookups()


async def test_the_lookup_cache_expires(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker's copy is read again once it is older than the TTL, so other workers' changes reach it."""
    lookup_cache.invalidate_lookups()
    now = [1000.0]
    monkeypatch.setattr(lookup_cache.time, "monotonic", lambda: now[0])
    first = await lookup_cache.get_lookups(db_session)
    assert await lookup_cache.get_lookups(db_session) is first

    now[0] += lookup_cache.TTL_SECONDS + 1
    assert await lookup_cache.get_lookups(db_session) is not first
    lookup_cache.invalidate_lookups()
