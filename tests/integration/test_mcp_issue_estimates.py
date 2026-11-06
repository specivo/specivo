"""``specivo_show_issue`` lists the original and remaining estimates."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools import _show_issue
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


async def _issue(db: AsyncSession, **fields):
    admin = AdminUserFactory.build(login="mcpest_admin", status="active")
    project = ProjectFactory.build(key="MCPEST", name="MCP Estimates")
    status = StatusFactory.build(name="Mcpest New", position=1, category="backlog")
    db.add_all([admin, project, status])
    await db.flush()
    tracker = TrackerFactory.build(name="Mcpest Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Mcpest Normal", position=1, is_default=True)
    db.add_all([tracker, priority])
    await db.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=admin.id,
        subject="Estimated work",
        **fields,
    )
    db.add(issue)
    await db.commit()
    return admin, issue


async def test_show_issue_lists_both_estimates(db_session: AsyncSession) -> None:
    admin, issue = await _issue(
        db_session, estimated_hours=Decimal("6"), original_estimate=Decimal("8.50"), remaining_estimate=Decimal("0.00")
    )
    text = await _show_issue(db_session, admin, issue.display_key, metadata_only=True)
    assert "Original estimate hours: 8.50" in text
    # Zero hours left is worth saying.
    assert "Remaining estimate hours: 0.00" in text


async def test_show_issue_without_estimates_says_nothing(db_session: AsyncSession) -> None:
    admin, issue = await _issue(db_session)
    text = await _show_issue(db_session, admin, issue.display_key, metadata_only=True)
    assert "Original estimate" not in text
    assert "Remaining estimate" not in text
