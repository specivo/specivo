"""The MCP AI spend report (``specivo_ai_usage_report``): text output, scope and seat cost rules.

Calls the tool's implementation with the test's session (no MCP server state),
on the world of the service tests with the prefix ``rm``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.mcp import server
from specivo.mcp.ai_usage_tools import _ai_usage_report
from specivo.models.user import User
from specivo.services.permission_service import clear_role_cache
from tests.services.test_ai_spend_reports import World, build_world, entry

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "rm")
    sub = world.subs["alice"]
    db = db_session
    await entry(
        db, world.alice, project=world.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub
    )
    await entry(
        db, world.alice, project=world.q, day=date(2026, 3, 6), api="10", cost_type="subscription", subscription=sub
    )
    await entry(db, world.bob, project=world.p, day=date(2026, 3, 7), api="4", model="lab-model-2")
    await db.commit()
    return world


async def _report(db: AsyncSession, user: User, **kw: Any) -> str:
    args: dict[str, Any] = {
        "project_key": None,
        "date_from": "2026-03-01",
        "date_to": "2026-03-31",
        "preset": None,
        "group_by": None,
        "period": None,
        "currency": "USD",
        "rate_policy": None,
        "include_seat_cost": True,
        "user_login": None,
        "tool": None,
        "model": None,
        "cost_type": None,
        "sort": "key",
    }
    args.update(kw)
    clear_role_cache(db)
    return await _ai_usage_report(db, user, **args)


async def test_the_tool_is_registered_and_documented() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    description = tools["specivo_ai_usage_report"].description or ""
    assert "API-equivalent value" in description and "administrators" in description


async def test_project_report_text(db_session: AsyncSession, w: World) -> None:
    text = await _report(db_session, w.mgr, project_key="RMA", group_by="user,model")
    assert text.startswith("AI spend report for project RMA, 2026-03-01 to 2026-03-31")
    assert "user=Rep Alice model=lab-model-1: entries=1" in text
    assert "seat=75.00 USD" in text and "leverage=0.40x" in text
    assert "user=Rep Bob model=lab-model-2: entries=1" in text
    assert text.splitlines()[-1].startswith("Total: entries=2")


async def test_partial_viewer_text_leaves_seat_cost_out(db_session: AsyncSession, w: World) -> None:
    text = await _report(db_session, w.alice, project_key="RMA")
    assert "Seat cost is left out" in text
    assert "seat=" not in text and "Rep Bob" not in text and "Secret plan" not in text


async def test_instance_report_is_for_administrators(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(PermissionDeniedError):
        await _report(db_session, w.mgr)
    text = await _report(db_session, w.admin, group_by="project", period="month")
    assert "AI spend report for the whole instance" in text
    assert "(unallocated seat)" in text


async def test_unknown_login_matches_nothing(db_session: AsyncSession, w: World) -> None:
    text = await _report(db_session, w.mgr, project_key="RMA", user_login="nobody_here")
    assert "Rows (0):" in text


async def test_bad_arguments(db_session: AsyncSession, w: World) -> None:
    for kw in ({"group_by": "salary"}, {"preset": "forever"}, {"date_from": "March"}, {"sort": "rank"}):
        with pytest.raises(AppError):
            await _report(db_session, w.mgr, project_key="RMA", **kw)
