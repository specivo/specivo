"""An edit made over MCP has the same side effects as one made over REST.

Relations are recorded in both issues' history, assignment and status edits
notify, audit rows name the project and the right event, and history
entries carry the API key the agent used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import tools
from specivo.mcp.client_context import mcp_api_key_id_var
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import EnabledModule, Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.api_key_service import ApiKeyService
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    admin: User
    other: User
    project: Project
    status_id: int


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="MSE New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="MSE Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="MSE Normal", position=1, is_default=True)
    project = ProjectFactory.build(key="MSE", identifier="mse")
    admin = AdminUserFactory.build(login="mse_admin", status="active")
    other = UserFactory.build(login="mse_other", status="active")
    db.add_all([tracker, priority, project, admin, other])
    await db.flush()
    db.add_all([EnabledModule(project_id=project.id, name=m) for m in ("issue_tracking", "wiki")])
    for n in (1, 2):
        await IssueService().create(
            db, project, IssueCreate(project_key="MSE", tracker_id=tracker.id, subject=f"MSE {n}"), admin
        )
    await db.commit()
    return World(admin, other, project, status.id)


async def _relation_history(db: AsyncSession, key: str) -> list[tuple[str, str | None, str | None]]:
    rows = await db.execute(
        select(JournalDetail.prop_key, JournalDetail.old_value, JournalDetail.new_value)
        .join(Journal, JournalDetail.journal_id == Journal.id)
        .join(Issue, Journal.issue_id == Issue.id)
        .where(Issue.project_key == key.split("-")[0], Issue.sequence_number == int(key.split("-")[1]))
        .where(JournalDetail.property == "relation")
    )
    return [tuple(r) for r in rows]


async def test_relations_added_and_removed_over_mcp_are_in_both_histories(db_session: AsyncSession, w: World) -> None:
    await tools._add_relation(db_session, w.admin, "MSE-1", "MSE-2", "blocks")
    assert await _relation_history(db_session, "MSE-1") == [("blocks", None, "MSE-2")]
    assert await _relation_history(db_session, "MSE-2") == [("blocked", None, "MSE-1")]

    listed = await tools._list_relations(db_session, w.admin, "MSE-1")
    relation_id = int(re.search(r"#(\d+)", listed).group(1))
    await tools._remove_relation(db_session, w.admin, "MSE-1", relation_id)
    assert ("blocks", "MSE-2", None) in await _relation_history(db_session, "MSE-1")
    assert ("blocked", "MSE-1", None) in await _relation_history(db_session, "MSE-2")


async def test_an_mcp_update_notifies_like_a_rest_one(
    db_session: AsyncSession, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, watchers = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tools._notification_svc, "notify_assignment", assignment)
    monkeypatch.setattr(tools._notification_svc, "notify_watchers", watchers)

    await tools._update_issue(db_session, w.admin, "MSE-1", assigned_to_id=w.other.id, status_id=w.status_id)

    assert assignment.await_args.kwargs["new_assignee_id"] == w.other.id
    assert watchers.await_args.kwargs["event_type"] == "status_change"


async def test_mcp_audit_rows_name_the_project_and_the_event(db_session: AsyncSession, w: World) -> None:
    await tools._edit_description(db_session, w.admin, "MSE-1", search_text="", replace_text="x")
    await tools._create_sprint(db_session, w.admin, "MSE", "Sprint A")

    rows = (
        await db_session.execute(
            select(SecurityAuditLog.event_type, SecurityAuditLog.project_id, SecurityAuditLog.details["tool"].astext)
        )
    ).all()
    by_tool = {tool: (event, project_id) for event, project_id, tool in rows if tool}
    assert by_tool["edit_description"] == ("issue_updated", w.project.id)
    assert by_tool["create_sprint"][0] == "sprint_created"


async def test_mcp_history_entries_carry_the_api_key(db_session: AsyncSession, w: World) -> None:
    key, _raw = await ApiKeyService().create_key(db_session, user_id=w.admin.id, name="mse", check_scope_access=False)
    token = mcp_api_key_id_var.set(key.id)
    try:
        await tools._update_issue(db_session, w.admin, "MSE-2", subject="Renamed by an agent")
        await tools._add_comment(db_session, w.admin, "MSE-2", "Agent note")
    finally:
        mcp_api_key_id_var.reset(token)

    journals = (
        (
            await db_session.execute(
                select(Journal.api_key_id)
                .join(Issue, Journal.issue_id == Issue.id)
                .where(Issue.project_key == "MSE", Issue.sequence_number == 2)
            )
        )
        .scalars()
        .all()
    )
    assert journals and all(api_key_id == key.id for api_key_id in journals)
