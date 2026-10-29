"""The MCP version and sprint issue lists apply the same visibility as the issue list.

The world of ``test_issue_visibility_listings``: public project P holds a
public and a private issue in a sprint and a version, and a public and a
private issue in the backlog (no sprint, no version). Each tool is called
through the server's own entry point with the reader's key, so the key is
authenticated and its project limit bound exactly as for a real client.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from tests.integration.test_issue_visibility_listings import World, build_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@pytest.fixture
def use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every tool call on the test's rolled-back session."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    token = mcp_raw_key_var.set(None)
    yield
    mcp_raw_key_var.reset(token)


async def _key(db: AsyncSession, user: User) -> str:
    _row, raw = await ApiKeyService().create_key(session=db, user_id=user.id, name=f"{user.login}-mcp")
    await db.commit()
    return raw


async def _lists(w: World, project_key: str, sprint_id: int, version_id: int) -> dict[str, str]:
    """Every sprint and version list of P, rendered with full fields."""
    return {
        "sprint": await server.specivo_list_sprint_issues(project_key=project_key, sprint_id=sprint_id, fields="full"),
        "backlog": await server.specivo_list_sprint_issues(project_key=project_key, sprint_id=0, fields="full"),
        "version": await server.specivo_list_version_issues(
            project_key=project_key, version_id=version_id, fields="full"
        ),
        "unversioned": await server.specivo_list_version_issues(project_key=project_key, version_id=0, fields="full"),
    }


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_lists_hide_private_issues(use_test_session, db_session: AsyncSession, reader: str) -> None:
    clear_role_cache(db_session)
    w = await build_world(db_session, "MVZ")
    # Plain values: a tool call may expire the ORM rows.
    p_key, sprint_id, version_id = w.p.key, w.sprint.id, w.version.id
    pub_key, pub_backlog_key = w.pub.display_key, w.pub_backlog.display_key
    hidden = (w.priv.display_key, w.priv_backlog.display_key)
    mcp_raw_key_var.set(await _key(db_session, getattr(w, reader)))

    lists = await _lists(w, p_key, sprint_id, version_id)
    assert pub_key in lists["sprint"] and pub_key in lists["version"]
    assert pub_backlog_key in lists["backlog"] and pub_backlog_key in lists["unversioned"]
    for name, text in lists.items():
        assert "secret" not in text, name
        for key in hidden:
            assert key not in text, name
        # The totals count the visible issues only.
        assert "(1 total" in text, (name, text)


async def test_lists_show_everything_to_a_member_who_sees_all(use_test_session, db_session: AsyncSession) -> None:
    clear_role_cache(db_session)
    w = await build_world(db_session, "MVA")
    p_key, sprint_id, version_id = w.p.key, w.sprint.id, w.version.id
    priv_key, priv_backlog_key = w.priv.display_key, w.priv_backlog.display_key
    mcp_raw_key_var.set(await _key(db_session, w.manager))

    lists = await _lists(w, p_key, sprint_id, version_id)
    assert priv_key in lists["sprint"] and priv_key in lists["version"]
    assert priv_backlog_key in lists["backlog"] and priv_backlog_key in lists["unversioned"]


async def test_lists_are_not_found_for_a_key_limited_to_another_project(
    use_test_session, db_session: AsyncSession
) -> None:
    clear_role_cache(db_session)
    w = await build_world(db_session, "MVL")
    p_key, sprint_id, version_id = w.p.key, w.sprint.id, w.version.id
    mcp_raw_key_var.set(w.limited_raw)

    for call in (
        server.specivo_list_sprint_issues(project_key=p_key, sprint_id=sprint_id),
        server.specivo_list_sprint_issues(project_key=p_key, sprint_id=0),
        server.specivo_list_version_issues(project_key=p_key, version_id=version_id),
        server.specivo_list_version_issues(project_key=p_key, version_id=0),
    ):
        with pytest.raises(ToolError) as exc:
            await call
        assert "not found" in str(exc.value)
