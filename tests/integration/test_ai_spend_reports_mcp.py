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
from tests.factories.issue import IssueFactory
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
    assert "specivo_ai_usage_report" in tools
    description = server.mcp.tool_help("specivo_ai_usage_report") or ""
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


async def test_issue_report_with_and_without_sub_issues(db_session: AsyncSession, w: World) -> None:
    parent = w.public_issue
    child = IssueFactory.build(
        project_id=parent.project_id,
        project_key=parent.project_key,
        tracker_id=parent.tracker_id,
        status_id=parent.status_id,
        priority_id=parent.priority_id,
        author_id=parent.author_id,
        parent_id=parent.id,
        sequence_number=3,
    )
    db_session.add(child)
    await db_session.flush()
    await entry(db_session, w.bob, project=w.p, issue=parent, day=date(2026, 3, 8), api="6")
    await entry(db_session, w.bob, project=w.p, issue=child, day=date(2026, 3, 9), api="2")
    await db_session.commit()

    # No project_key: the issue's project report, narrowed to the issue.
    text = await _report(db_session, w.mgr, issue_ref=parent.display_key, group_by="")
    assert text.startswith(f"AI spend report for issue {parent.display_key} in project RMA")
    assert text.splitlines()[-1].startswith("Total: entries=1")
    assert "api_equivalent=6.00 USD" in text.splitlines()[-1]

    tree = await _report(db_session, w.mgr, issue_ref=parent.display_key, include_sub_issues=True, group_by="")
    assert f"issue {parent.display_key} and its sub-issues" in tree
    assert tree.splitlines()[-1].startswith("Total: entries=2")
    assert "api_equivalent=8.00 USD" in tree.splitlines()[-1]


async def test_issue_report_checks_the_project_and_visibility(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(AppError, match="is not in project RMB"):
        await _report(db_session, w.admin, project_key="RMB", issue_ref=w.public_issue.display_key)
    # Bob may not see the private issue: it is not found rather than reported empty.
    with pytest.raises(AppError):
        await _report(db_session, w.bob, issue_ref=w.private_issue.display_key)


async def test_a_system_account_login_matches_nothing(db_session: AsyncSession, w: World) -> None:
    from tests.factories.user import UserFactory

    system = UserFactory.build(login="$rm_git", status="active", display_name="Git", password_hash=None)
    db_session.add(system)
    await db_session.flush()
    await entry(db_session, system, project=w.p, day=date(2026, 3, 9), api="1")
    await db_session.commit()
    # Its usage counts in the report (totals stay exact), but its login names nobody.
    assert "Git" in await _report(db_session, w.mgr, project_key="RMA")
    assert "Rows (0):" in await _report(db_session, w.mgr, project_key="RMA", user_login="$rm_git")


_FULL_TEXT = {
    "instance": (
        "AI spend report for the whole instance, 2026-03-01 to 2026-03-31 (UTC days), amounts in USD, "
        "rate policy entry_date.\n"
        "spend = marginal spend + seat cost (subscription fees spread over the entries that used them); "
        "api_equivalent = the same tokens at pay-per-use prices, not a bill.\n"
        "Rows (3):\n"
        "  2026-03-01 project=RMA: entries=2 tokens=2200 marginal=4.00 USD seat=75.00 USD spend=79.00 "
        "USD api_equivalent=34.00 USD leverage=0.40x\n"
        "  2026-03-01 project=RMB: entries=1 tokens=1100 marginal=0.00 USD seat=25.00 USD spend=25.00 "
        "USD api_equivalent=10.00 USD leverage=0.40x\n"
        "  2026-03-01 project=(unallocated seat): entries=0 tokens=0 marginal=0.00 USD seat=0.00 USD "
        "unallocated_seat=15.00 USD (+ not converted: 100.00000000 EUR no_rate) spend=15.00 USD (+ not "
        "converted: 100.00000000 EUR no_rate) api_equivalent=0.00 USD leverage=0.00x\n"
        "Total: entries=3 tokens=3300 marginal=4.00 USD seat=100.00 USD unallocated_seat=15.00 USD (+ "
        "not converted: 100.00000000 EUR no_rate) spend=119.00 USD (+ not converted: 100.00000000 EUR "
        "no_rate) api_equivalent=44.00 USD leverage=0.35x"
    ),
    "mgr_noseat": (
        "AI spend report for project RMA, 2026-03-01 to 2026-03-31 (UTC days), amounts in USD, rate "
        "policy entry_date.\n"
        "spend = marginal spend + seat cost (subscription fees spread over the entries that used them); "
        "api_equivalent = the same tokens at pay-per-use prices, not a bill.\n"
        "Seat cost is left out (include_seat_cost=false).\n"
        "Rows (2):\n"
        "  user=Rep Bob model=lab-model-2: entries=1 tokens=1100 spend=4.00 USD api_equivalent=4.00 USD\n"
        "  user=Rep Alice model=lab-model-1: entries=1 tokens=1100 spend=0.00 USD api_equivalent=30.00 "
        "USD\n"
        "Total: entries=2 tokens=2200 spend=4.00 USD api_equivalent=34.00 USD"
    ),
    "alice": (
        "AI spend report for project RMA, 2026-03-01 to 2026-03-31 (UTC days), amounts in USD, rate "
        "policy entry_date.\n"
        "spend = marginal spend + seat cost (subscription fees spread over the entries that used them); "
        "api_equivalent = the same tokens at pay-per-use prices, not a bill.\n"
        "Seat cost is left out: you do not read every entry of this project in the period.\n"
        "Rows (1):\n"
        "  issue=(no issue): entries=1 tokens=1100 spend=0.00 USD api_equivalent=30.00 USD\n"
        "Total: entries=1 tokens=1100 spend=0.00 USD api_equivalent=30.00 USD"
    ),
    "issue": (
        "AI spend report for issue RMA-1 in project RMA, 2026-03-01 to 2026-03-31 (UTC days), amounts "
        "in USD, rate policy entry_date.\n"
        "spend = marginal spend + seat cost (subscription fees spread over the entries that used them); "
        "api_equivalent = the same tokens at pay-per-use prices, not a bill.\n"
        "Rows (0):\n"
        "  (none)\n"
        "Total: entries=0 tokens=0 marginal=0.00 USD seat=0.00 USD spend=0.00 USD api_equivalent=0.00 "
        "USD"
    ),
}


@pytest.mark.parametrize("case", sorted(_FULL_TEXT))
async def test_full_report_text(db_session: AsyncSession, w: World, case: str) -> None:
    """The whole text of a few reports, line for line (scope line, seat notes, rows, folded total)."""
    calls: dict[str, tuple[User, dict[str, Any]]] = {
        "instance": (w.admin, {"group_by": "project", "period": "month"}),
        "mgr_noseat": (
            w.mgr,
            {"project_key": "RMA", "include_seat_cost": False, "group_by": "user,model", "sort": "spend"},
        ),
        "alice": (w.alice, {"project_key": "RMA", "group_by": "issue"}),
        "issue": (w.mgr, {"project_key": "RMA", "issue_ref": w.public_issue.display_key, "group_by": "user"}),
    }
    user, kw = calls[case]
    assert await _report(db_session, user, **kw) == _FULL_TEXT[case]


async def test_scope_is_resolved_before_the_dates_are_read(db_session: AsyncSession, w: World) -> None:
    """An unreachable project is refused before a malformed date is looked at; a bad preset before both."""
    with pytest.raises(AppError) as exc:
        await _report(db_session, w.mgr, project_key="RMB", date_from="March")
    assert exc.value.status_code == 404
    with pytest.raises(AppError) as exc:
        await _report(db_session, w.mgr, project_key="RMB", preset="forever")
    assert (exc.value.status_code, exc.value.field) == (422, "preset")
    with pytest.raises(AppError) as exc:
        await _report(db_session, w.admin, project_key="RMB", issue_ref=w.public_issue.display_key, date_from="x")
    assert exc.value.field == "issue_ref"
