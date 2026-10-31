"""``specivo_show_issue`` lists the commits linked to the issue, capped with a count."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools import _show_issue
from specivo.services.repository_hooks import process_delivery, record_delivery
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory
from tests.fixtures.forge_hooks import commit, make_integration, push_payload

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _setup(db: AsyncSession):
    lookups = (TrackerFactory.build(), StatusFactory.build(), PriorityFactory.build())
    admin = AdminUserFactory.build(login="rmcp_admin", status="active")
    project = ProjectFactory.build(key="RMCP", identifier="rmcp", name="MCP commits")
    db.add_all([*lookups, admin, project])
    await db.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key="RMCP",
        sequence_number=1,
        tracker_id=lookups[0].id,
        status_id=lookups[1].id,
        priority_id=lookups[2].id,
        author_id=admin.id,
    )
    db.add(issue)
    await db.commit()
    return admin, project


async def _push(db: AsyncSession, integration, commits, delivery_id: str) -> None:
    row = await record_delivery(
        db,
        integration,
        delivery_id=delivery_id,
        event="push",
        status="accepted",
        payload=push_payload("github", commits, branch="feat/rmcp-1-x"),
    )
    await process_delivery(db, row)
    await db.commit()


async def test_no_commits_line_without_links(db_session: AsyncSession) -> None:
    admin, _project = await _setup(db_session)
    assert "Commits:" not in await _show_issue(db_session, admin, "RMCP-1")


async def test_lists_linked_commits(db_session: AsyncSession) -> None:
    admin, project = await _setup(db_session)
    integration = await make_integration(db_session, project)
    c = commit("only", "Wire the login form\n\nmore text")
    await _push(db_session, integration, [c], "d-1")
    out = await _show_issue(db_session, admin, "RMCP-1", metadata_only=True)
    assert "Commits: 1" in out
    assert f"- {c['id'][:7]} Wire the login form [feat/rmcp-1-x] {c['url']}" in out


async def test_many_commits_are_capped_with_a_count(db_session: AsyncSession) -> None:
    admin, project = await _setup(db_session)
    integration = await make_integration(db_session, project)
    await _push(db_session, integration, [commit(f"n{i}", f"Step {i}") for i in range(13)], "d-2")
    out = await _show_issue(db_session, admin, "RMCP-1", metadata_only=True)
    assert "Commits: 13" in out
    assert sum(1 for line in out.splitlines() if line.startswith("- ") and "Step" in line) == 10
    assert "- ... and 3 more" in out


async def _pull_request(db: AsyncSession, integration, delivery_id: str, **kwargs) -> None:
    from tests.fixtures.forge_hooks import pull_request_payload

    row = await record_delivery(
        db,
        integration,
        delivery_id=delivery_id,
        event="pull_request",
        status="accepted",
        payload=pull_request_payload("github", **kwargs),
    )
    await process_delivery(db, row)
    await db.commit()


async def test_no_pull_requests_line_without_links(db_session: AsyncSession) -> None:
    admin, _project = await _setup(db_session)
    assert "Pull requests:" not in await _show_issue(db_session, admin, "RMCP-1")


async def test_lists_linked_pull_requests(db_session: AsyncSession) -> None:
    admin, project = await _setup(db_session)
    integration = await make_integration(db_session, project)
    await _pull_request(db_session, integration, "pr-1", number=12, title="Login form", body="Fixes RMCP-1")
    await _pull_request(db_session, integration, "pr-2", number=13, title="Docs for RMCP-1", state="merged")
    out = await _show_issue(db_session, admin, "RMCP-1", metadata_only=True)
    assert "Pull requests: 2" in out
    assert (
        "- #12 [open, closing] Login form (feat/login-form) https://forge.example.com/example-org/widgets/pull/12"
        in out
    )
    assert "- #13 [merged] Docs for RMCP-1 (feat/login-form)" in out
