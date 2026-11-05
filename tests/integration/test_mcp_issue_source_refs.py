"""``specivo_show_issue`` names the number an imported issue had."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.tools import _show_issue
from specivo.models.issue_source_ref import IssueSourceRef
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


async def _issue(db: AsyncSession):
    admin = AdminUserFactory.build(login="mcpsrc_admin", status="active")
    project = ProjectFactory.build(key="MCPSRC", name="MCP Source")
    status = StatusFactory.build(name="Mcpsrc New", position=1, category="backlog")
    db.add_all([admin, project, status])
    await db.flush()
    tracker = TrackerFactory.build(name="Mcpsrc Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Mcpsrc Normal", position=1, is_default=True)
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
        subject="Printer jams",
    )
    db.add(issue)
    await db.commit()
    return admin, issue


async def test_mcp_show_issue_includes_imported_from(db_session: AsyncSession) -> None:
    admin, issue = await _issue(db_session)
    db_session.add(IssueSourceRef(issue_id=issue.id, source_system="rt", ref="123", label="RT #123", is_primary=True))
    await db_session.commit()
    assert "Imported from: RT #123" in await _show_issue(db_session, admin, issue.display_key, metadata_only=True)


async def test_mcp_show_issue_without_a_number_says_nothing(db_session: AsyncSession) -> None:
    admin, issue = await _issue(db_session)
    assert "Imported from" not in await _show_issue(db_session, admin, issue.display_key, metadata_only=True)


def test_the_setup_guide_mentions_it() -> None:
    assert "Imported from: RT #123" in generate_setup_guide()
