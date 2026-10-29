"""Sprint code decides "done" by status category, never by status name.

A project may rename its statuses. Here the done status is called "Shipped"
(category done) and a status called "Resolved" is still in progress (category
active). Every place that counts or moves done issues must agree: the board
and sprint list counts, analytics, burndown, completing a sprint and its
velocity snapshot, and the dashboard's "resolved" agent activity.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.lookups import IssueStatus
from specivo.models.project import EnabledModule, Project
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.services.sprint_service import SprintService, count_done
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory
from tests.integration.test_issue_visibility_listings import _cookie

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = SprintService()


class Setup:
    admin: User
    agent: User
    project: Project
    sprint: Sprint
    shipped: IssueStatus
    resolved: IssueStatus
    done_issue: Issue
    other_done_issue: Issue
    open_issue: Issue


@pytest_asyncio.fixture
async def setup(db_session: AsyncSession) -> Setup:
    s = Setup()
    s.admin = AdminUserFactory.build(login="sdc_admin", status="active")
    s.agent = ServiceAccountFactory.build(login="sdc_agent", status="active")
    db_session.add_all([s.admin, s.agent])
    await db_session.flush()

    s.shipped = IssueStatus(name="Shipped", position=3, category="done")
    s.resolved = IssueStatus(name="Resolved", position=2, category="active")
    db_session.add_all([s.shipped, s.resolved])
    await db_session.flush()
    tracker = TrackerFactory.build(name="SDC Task", default_status_id=s.resolved.id)
    priority = PriorityFactory.build(name="SDC Normal", position=1, is_default=True)
    db_session.add_all([tracker, priority])
    await db_session.flush()

    s.project = ProjectFactory.build(key="SDC", identifier="sdc", name="Project SDC")
    db_session.add(s.project)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=s.project.id, name="issue_tracking"))
    s.sprint = Sprint(
        project_id=s.project.id,
        name="SDC sprint",
        status="active",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 14),
    )
    db_session.add(s.sprint)
    await db_session.flush()

    def issue(number: int, status: IssueStatus, hours: str) -> Issue:
        return IssueFactory.build(
            project_id=s.project.id,
            project_key=s.project.key,
            sequence_number=number,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=s.admin.id,
            subject=f"SDC issue {number}",
            sprint_id=s.sprint.id,
            estimated_hours=Decimal(hours),
        )

    # Two done, one in progress: counting by name ("Resolved") would say one.
    s.done_issue = issue(1, s.shipped, "3")
    s.other_done_issue = issue(2, s.shipped, "2")
    s.open_issue = issue(3, s.resolved, "5")
    db_session.add_all([s.done_issue, s.other_done_issue, s.open_issue])
    s.project.issue_sequence = 3
    await db_session.commit()
    return s


async def test_the_board_counts_a_renamed_done_status(
    client: AsyncClient, db_session: AsyncSession, setup: Setup
) -> None:
    s = setup
    board = await _svc.board_data(db_session, s.sprint, s.admin)
    assert set(board) == {"Shipped", "Resolved"}
    assert count_done(board) == 2

    resp = await client.get(f"/projects/{s.project.key}/sprints/{s.sprint.id}/board/", cookies=_cookie(s.admin))
    assert resp.status_code == 200, resp.text
    assert "2 of 3 done" in " ".join(resp.text.split())


async def test_the_sprint_list_and_analytics_count_by_category(
    client: AsyncClient, db_session: AsyncSession, setup: Setup
) -> None:
    s = setup
    resp = await client.get(f"/projects/{s.project.key}/sprints/", cookies=_cookie(s.admin))
    assert resp.status_code == 200, resp.text
    done = re.search(r'color:var\(--sp-success\)">(\d+)</span>\s*<span class="sprint-list-stat-label">Done', resp.text)
    assert done is not None and done.group(1) == "2"

    resp = await client.get(f"/projects/{s.project.key}/sprints/analytics/", cookies=_cookie(s.admin))
    assert resp.status_code == 200, resp.text
    text = " ".join(resp.text.split())
    assert 'title="Completed: 2"' in text
    assert 'title="Completed: 1"' not in text


async def test_burndown_counts_a_renamed_done_status(db_session: AsyncSession, setup: Setup) -> None:
    data = await _svc.burndown_data(db_session, setup.sprint, setup.admin)
    assert (data["total_estimated_hours"], data["completed_hours"]) == (10, 5)


async def test_completing_a_sprint_keeps_done_issues_and_moves_the_rest(db_session: AsyncSession, setup: Setup) -> None:
    s = setup
    await _svc.complete_sprint(db_session, s.sprint)
    await db_session.commit()
    await db_session.refresh(s.done_issue)
    await db_session.refresh(s.open_issue)

    assert s.done_issue.sprint_id == s.sprint.id
    assert s.open_issue.sprint_id is None
    velocity = (await _svc.velocity_for(db_session, [s.sprint], s.admin))[s.sprint.id]
    assert velocity == {"total_issues": 3, "completed_issues": 2}


async def test_dashboard_reads_a_move_to_a_done_status_as_resolved(
    client: AsyncClient, db_session: AsyncSession, setup: Setup
) -> None:
    s = setup
    for sequence, issue, status in ((1, s.done_issue, s.shipped), (1, s.open_issue, s.resolved)):
        journal = Journal(issue_id=issue.id, project_id=s.project.id, user_id=s.agent.id, sequence=sequence)
        db_session.add(journal)
        await db_session.flush()
        db_session.add(
            JournalDetail(
                journal_id=journal.id, property="attr", prop_key="status_id", old_value=None, new_value=str(status.id)
            )
        )
    await db_session.commit()

    resp = await client.get("/", cookies=_cookie(s.admin))
    assert resp.status_code == 200
    text = " ".join(resp.text.split())
    assert re.search(rf'resolved</span> <a class="target" href="/issue/{s.done_issue.display_key}/"', text)
    assert not re.search(rf'resolved</span> <a class="target" href="/issue/{s.open_issue.display_key}/"', text)
