"""The AI usage charts of the spend reports: scopes, permissions, seat cost, leverage, stacks, caps and the cache.

The world of the spend report service tests (``tests/services/test_ai_spend_reports.py``)
with its own prefix: projects ``RCA`` (``w.p``) and ``RCB`` (``w.q``). In March:

- alice (plan 100 USD a month) uses it for 30 USD of value in RCA and 10 in RCB,
  so RCA carries 75 USD of her fee and RCB 25;
- bob pays per use: 4 USD in RCA, 1 USD in RCA on a private issue, 2 USD unassigned;
- bob's yearly plan (100 EUR a month, 110 USD at the March rate) and dave's
  (15 USD from 17 March) carry no usage, so their fees are unallocated.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import AiSpendReportService, ReportFilters
from specivo.services.ai_usage_chart_service import MAX_BUCKETS, NO_PROJECT, AiUsageChartService, UsageCharts
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.services.test_ai_spend_reports import World, build_world, entry, rate

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MARCH_1, MARCH_31 = date(2026, 3, 1), date(2026, 3, 31)
MARCH = {"from_date": "2026-03-01", "to_date": "2026-03-31"}
_charts = AiUsageChartService()


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "rc")
    db = db_session
    sub = world.subs["alice"]
    await entry(
        db, world.alice, project=world.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub
    )
    await entry(
        db, world.alice, project=world.q, day=date(2026, 3, 6), api="10", cost_type="subscription", subscription=sub
    )
    await entry(db, world.bob, project=world.p, day=date(2026, 3, 7), api="4", tool="codex")
    await entry(db, world.bob, project=world.p, day=date(2026, 3, 9), api="1", issue=world.private_issue)
    await entry(db, world.bob, project=None, day=date(2026, 3, 8), api="2", model="lab-model-2")
    await rate(db, MARCH_31, "EUR", "USD", "1.1")
    await db.commit()
    return world


async def _report_charts(
    db: AsyncSession,
    viewer: User,
    project: Project | None,
    *,
    date_from: date = MARCH_1,
    date_to: date = MARCH_31,
    **kw: Any,
) -> UsageCharts:
    clear_role_cache(db)
    return await _charts.report_charts(
        db, viewer, project=project, date_from=date_from, date_to=date_to, currency="USD", **kw
    )


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _page(client: AsyncClient, user: User, url: str, **params: str) -> str:
    resp = await client.get(url, params=params, cookies=_cookies(user))
    assert resp.status_code == 200, resp.text
    return resp.text


def _chart_data(html: str, testid: str) -> dict[str, Any]:
    match = re.search(rf'data-testid="{testid}"[^>]*?data-sp-spend-chart=\'([^\']*)\'', html, re.S)
    assert match, testid
    data: dict[str, Any] = json.loads(match.group(1).replace("&#34;", '"').replace("&amp;", "&"))
    return data


def _stack_switch(html: str) -> list[str]:
    """The stack switch's options, in order (the first is the chart's mode)."""
    options = re.findall(r'name="aiu-chart-stack" value="(\w+)"', html)
    mode = re.search(r'data-sp-chart-mode="(\w+)"', html)
    assert mode and options and mode.group(1) == options[0]
    return options


# ---------------------------------------------------------------------------
# Scopes and permissions
# ---------------------------------------------------------------------------


async def test_instance_scope_is_for_administrators(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(PermissionDeniedError):
        await _report_charts(db_session, w.mgr, None)
    charts = await _report_charts(db_session, w.admin, None)
    assert charts.scope == "instance" and charts.entries == 5
    assert sum(b.api for b in charts.buckets) == Decimal(47)


async def test_project_scope_counts_only_the_projects_entries(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.mgr, w.p)
    assert charts.scope == "project" and charts.entries == 3
    assert sum(b.api for b in charts.buckets) == Decimal(35)
    # Nothing of RCB (alice's 10) nor of the unassigned entry.
    assert {s.label for s in charts.by_project} == {w.p.key}
    assert NO_PROJECT not in charts.projects


async def test_partial_viewer_sees_own_entries_and_no_seat_cost(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.alice, w.p)
    assert charts.entries == 1 and sum(b.api for b in charts.buckets) == Decimal(30)
    assert not charts.seat_shown and not charts.has_seat
    assert charts.people == [] and charts.leverage == [] and charts.leverage_total is None
    assert set(charts.user_labels.values()) == {"Rep Alice"}


async def test_viewer_who_misses_a_private_entry_gets_no_seat_cost(db_session: AsyncSession, w: World) -> None:
    """carol reads every entry of RCA but the one on the private issue: the report's rule leaves seat cost out."""
    charts = await _report_charts(db_session, w.carol, w.p)
    assert charts.entries == 2 and sum(b.api for b in charts.buckets) == Decimal(34)
    assert not charts.seat_shown and all(b.seat == 0 for b in charts.buckets)
    assert charts.people == []


async def test_project_member_without_access_is_refused(db_session: AsyncSession, w: World) -> None:
    # RCB is private and carol is not a member there: it does not exist for her.
    with pytest.raises((PermissionDeniedError, NotFoundError)):
        await _report_charts(db_session, w.carol, w.q)


# ---------------------------------------------------------------------------
# Seat cost and leverage
# ---------------------------------------------------------------------------


async def test_instance_seat_cost_adds_up_to_the_report(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.admin, None)
    report = await AiSpendReportService().report(
        db_session, w.admin, project=None, date_from=MARCH_1, date_to=MARCH_31, group_by=(), currency="USD"
    )
    seat = report.totals.seat_allocated.total.amount + report.totals.seat_unallocated.total.amount  # type: ignore[union-attr]
    assert seat == Decimal(225)
    assert charts.has_seat and sum(b.seat for b in charts.buckets) == seat
    assert sum(b.spend for b in charts.buckets) == report.totals.marginal.total.amount  # type: ignore[union-attr]
    # Unallocated fees run all month, so the range covers the plans' days, not only the days with entries.
    assert (charts.first, charts.last, charts.bucket) == (MARCH_1, MARCH_31, "day")


async def test_instance_leverage_per_plan_person_and_total(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.admin, None)
    plans = {(p.holder, p.plan_name): (p.fee, p.api, p.leverage) for p in charts.leverage}
    assert plans == {
        ("Rep Alice", "Secret plan"): (Decimal(100), Decimal(40), Decimal("0.40")),
        ("Rep Bob", "Secret plan"): (Decimal(110), Decimal(0), Decimal("0.00")),
        ("Rep Dave", "Secret plan"): (Decimal(15), Decimal(0), Decimal("0.00")),
    }
    assert [p.label for p in charts.people] == ["Rep Bob", "Rep Alice", "Rep Dave"]  # largest fee first
    total = charts.leverage_total
    assert total is not None and (total.fee, total.api, total.leverage) == (Decimal(225), Decimal(40), Decimal("0.18"))


async def test_a_fee_without_a_rate_leaves_the_ratio_out(db_session: AsyncSession, w: World) -> None:
    other = await _charts.report_charts(
        db_session, w.admin, project=None, date_from=MARCH_1, date_to=MARCH_31, currency="THB"
    )
    # No rate into THB at all: every fee is unconverted, so no fee and no ratio are shown.
    assert all(p.fee is None and p.leverage is None for p in other.leverage)
    assert other.leverage_total is not None and other.leverage_total.leverage is None
    assert other.unconverted > 0


async def test_project_leverage_is_the_projects_share_per_person(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.mgr, w.p)
    assert charts.seat_shown and sum(b.seat for b in charts.buckets) == Decimal(75)
    # No plan details on a project report, and only the share RCA's entries carry (not alice's whole fee).
    assert charts.leverage == []
    assert [(p.label, p.fee, p.api, p.leverage) for p in charts.people] == [
        ("Rep Alice", Decimal(75), Decimal(30), Decimal("0.40"))
    ]
    # bob's and dave's unallocated fees never reach a project report.
    assert charts.leverage_total is not None and charts.leverage_total.fee == Decimal(75)


async def test_seats_left_out_on_request(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.admin, None, include_seat_cost=False)
    assert not charts.seat_shown and not charts.has_seat and charts.leverage_total is None
    # Without unallocated fees the range is the days with entries.
    assert (charts.first, charts.last) == (date(2026, 3, 5), date(2026, 3, 9))


async def test_filters_apply_to_the_charts(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.admin, None, filters=ReportFilters(user_id=w.bob.id))
    assert charts.entries == 3 and sum(b.api for b in charts.buckets) == Decimal(7)
    # bob's own unallocated fee is still his.
    assert [p.label for p in charts.people] == ["Rep Bob"]
    charts = await _report_charts(db_session, w.admin, None, filters=ReportFilters(tool="codex"))
    assert charts.entries == 1 and charts.leverage_total is None


# ---------------------------------------------------------------------------
# Stacks, breakdowns and caps
# ---------------------------------------------------------------------------


async def test_stack_keys_per_scope(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.admin, None)
    assert charts.users == [str(w.alice.id), str(w.bob.id)]
    assert charts.projects == [str(w.p.id), str(w.q.id), NO_PROJECT]
    assert charts.tools == ["claude-code", "codex"]
    assert charts.models == ["lab-model-1", "lab-model-2"]
    assert [s.label for s in charts.by_user] == ["Rep Alice", "Rep Bob"]
    assert [s.key for s in charts.by_tool] == ["claude-code", "codex"]
    assert [s.label for s in charts.by_tool] == [charts.tool_labels["claude-code"], charts.tool_labels["codex"]]
    day = next(b for b in charts.buckets if b.start == date(2026, 3, 7))
    assert day.api_by_user == {str(w.bob.id): Decimal(4)} and day.api_by_tool == {"codex": Decimal(4)}


async def test_own_scope_keeps_its_stacks(db_session: AsyncSession, w: World) -> None:
    charts = await _charts.charts(db_session, w.bob, currency="USD")
    assert charts.scope == "own" and charts.users == [] and charts.projects == [] and charts.by_user == []


async def test_extreme_range_is_capped_and_clamped(db_session: AsyncSession, w: World) -> None:
    await entry(db_session, w.bob, project=w.p, day=date(2024, 1, 2), api="1")
    await entry(db_session, w.bob, project=w.p, day=date(2026, 12, 30), api="1")
    await db_session.commit()
    charts = await _report_charts(
        db_session, w.mgr, w.p, date_from=date(2024, 1, 1), date_to=date(2027, 1, 4), bucket="day"
    )
    # Day buckets over three years would be far over the cap: the automatic size is used instead.
    assert charts.bucket == "month" and len(charts.buckets) <= MAX_BUCKETS
    assert (charts.first, charts.last) == (date(2024, 1, 2), date(2026, 12, 30))
    # A wide date range with few entries is clamped to them.
    narrow = await _report_charts(db_session, w.mgr, w.p, date_from=date(2026, 3, 1), date_to=date(2026, 3, 8))
    assert (narrow.first, narrow.last, narrow.bucket, len(narrow.buckets)) == (
        date(2026, 3, 5),
        date(2026, 3, 7),
        "day",
        3,
    )


async def test_chosen_bucket_follows_the_seat_cost(db_session: AsyncSession, w: World) -> None:
    charts = await _report_charts(db_session, w.admin, None, bucket="week")
    assert charts.bucket == "week"
    assert sum(b.seat for b in charts.buckets) == Decimal(225)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


async def test_admin_page_charts(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, "/admin/ai-spend/", **MARCH)
    time = _chart_data(html, "aiu-chart-time")
    assert _stack_switch(html) == ["user", "tool", "model", "project"]
    assert time["stacks"]["user"]["names"] == ["Rep Alice", "Rep Bob"]
    assert time["seat"] and sum(time["seat"]) == 225
    assert 'data-testid="aiu-chart-users"' in html and 'data-testid="aiu-chart-projects"' in html
    assert 'data-testid="aiu-chart-tools"' in html and 'data-testid="aiu-chart-models"' in html
    assert 'data-testid="spend-leverage-plans"' in html and "Secret plan" in html
    total = re.search(r'data-testid="spend-leverage-total">(.*?)</tr>', html, re.S)
    assert total and "0.18×" in total.group(1)
    # The charts sit above the report table, which still works.
    assert html.index('data-testid="aiu-charts"') < html.index('data-testid="spend-table"')
    assert "spend-chart.min" in html


async def test_project_page_charts_for_a_manager(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, "/projects/RCA/ai-usage/report/", **MARCH)
    time = _chart_data(html, "aiu-chart-time")
    assert _stack_switch(html) == ["user", "tool", "model"]
    assert sum(time["seat"]) == 75
    assert 'data-testid="aiu-chart-projects"' not in html
    assert 'data-testid="spend-leverage-people"' in html and 'data-testid="spend-leverage-plans"' not in html
    assert "Secret plan" not in html


async def test_project_page_charts_for_a_partial_viewer(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.alice, "/projects/RCA/ai-usage/report/", **MARCH)
    time = _chart_data(html, "aiu-chart-time")
    assert time["stacks"]["user"]["names"] == ["Rep Alice"]
    assert "seat" not in time and "Rep Bob" not in html
    assert 'data-testid="spend-leverage-charts"' not in html and "Secret plan" not in html


async def test_cache_is_not_shared_between_readers(client: AsyncClient, w: World) -> None:
    assert get_settings().ai_usage_chart_cache_seconds > 0
    first = await _page(client, w.mgr, "/projects/RCA/ai-usage/report/", **MARCH)
    assert "Rep Bob" in json.dumps(_chart_data(first, "aiu-chart-time"))
    # Same scope and filters, another reader with fewer rights: a payload of their own.
    second = await _page(client, w.alice, "/projects/RCA/ai-usage/report/", **MARCH)
    data = _chart_data(second, "aiu-chart-time")
    assert "Rep Bob" not in json.dumps(data) and "seat" not in data
    # And the manager's payload is served again from the cache unchanged.
    again = await _page(client, w.mgr, "/projects/RCA/ai-usage/report/", **MARCH)
    assert _chart_data(again, "aiu-chart-time") == _chart_data(first, "aiu-chart-time")


async def test_cache_follows_new_entries(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    before = _chart_data(await _page(client, w.mgr, "/projects/RCA/ai-usage/report/", **MARCH), "aiu-chart-time")
    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 20), api="5")
    await db_session.commit()
    after = _chart_data(await _page(client, w.mgr, "/projects/RCA/ai-usage/report/", **MARCH), "aiu-chart-time")
    assert len(after["periods"]) > len(before["periods"])


async def test_bucket_links_keep_the_report_filters(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, "/admin/ai-spend/", **MARCH, group="model", bucket="week")
    assert 'data-testid="aiu-chart-buckets"' in html
    links = re.findall(r'href="(/admin/ai-spend/\?[^"]*)"[^>]*data-bucket="(\w+)"', html)
    by_bucket = {b: url.replace("&amp;", "&") for url, b in links}
    assert "bucket=day" in by_bucket["day"] and "group=model" in by_bucket["day"]
    assert "bucket=" not in by_bucket["auto"]
    assert "Per week (from Monday)" in html


async def test_empty_report_has_no_charts(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, "/admin/ai-spend/", from_date="2025-01-01", to_date="2025-01-31")
    assert 'data-testid="aiu-charts"' not in html and "spend-chart.min" not in html


# ---------------------------------------------------------------------------
# The project's AI usage tab
# ---------------------------------------------------------------------------


async def test_project_tab_charts_for_a_manager(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, "/projects/RCA/ai-usage/")
    time = _chart_data(html, "aiu-chart-time")
    assert _stack_switch(html) == ["user", "tool", "model"]
    assert sum(time["seat"]) == 75
    assert sum(sum(v) for v in time["stacks"]["user"]["values"]) == 35
    assert 'data-testid="spend-leverage-people"' in html and "Secret plan" not in html
    assert 'data-testid="aiu-chart-projects"' not in html and "spend-chart.min" in html


async def test_project_tab_charts_for_a_partial_viewer(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.alice, "/projects/RCA/ai-usage/")
    time = _chart_data(html, "aiu-chart-time")
    assert time["stacks"]["user"]["names"] == ["Rep Alice"] and "seat" not in time
    assert 'data-testid="spend-leverage-charts"' not in html


async def test_project_tab_filters_a_fee_cannot_follow_leave_seat_cost_out(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, "/projects/RCA/ai-usage/", branch="-")
    time = _chart_data(html, "aiu-chart-time")
    assert sum(sum(v) for v in time["stacks"]["user"]["values"]) == 35
    assert "seat" not in time and 'data-testid="spend-leverage-charts"' not in html
    # Only the excluded entries: none here, so no charts and no chart bundle.
    html = await _page(client, w.mgr, "/projects/RCA/ai-usage/", status="excluded")
    assert 'data-testid="aiu-charts"' not in html and "spend-chart.min" not in html


async def test_project_tab_bucket_links_stay_on_the_tab(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, "/projects/RCA/ai-usage/", tool="claude-code", bucket="week")
    links = dict((b, u.replace("&amp;", "&")) for u, b in re.findall(r'href="([^"]*)"[^>]*data-bucket="(\w+)"', html))
    assert links["day"].startswith("/projects/RCA/ai-usage/?") and "tool=claude-code" in links["day"]
    assert 'name="bucket" value="week"' in html
    assert "Per week (from Monday)" in html


async def test_project_tab_cache_is_per_reader(client: AsyncClient, w: World) -> None:
    first = _chart_data(await _page(client, w.mgr, "/projects/RCA/ai-usage/"), "aiu-chart-time")
    second = _chart_data(await _page(client, w.alice, "/projects/RCA/ai-usage/"), "aiu-chart-time")
    assert "seat" in first and "seat" not in second
    assert "Rep Bob" not in json.dumps(second)
