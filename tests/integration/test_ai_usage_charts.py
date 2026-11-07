"""The charts of My AI usage: buckets, stacks, top groups, filters, own entries only, unpriced periods, leverage.

The world of the REST tests (``test_ai_usage_api.build_world``) with its own
prefix: ``alice`` and ``bob`` log their own usage in project ``AUCA``; the
price of ``lab-model-1`` (2 USD per million input, 10 per million output)
makes each default entry worth 3 USD.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.user import User
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.ai_spend_report_service import AiSpendReportService
from specivo.services.ai_usage_chart_service import MAX_BUCKETS, NO_PROJECT, OTHER, TOP_N, AiUsageChartService
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService
from specivo.services.auth_service import _make_access_token
from tests.integration.test_ai_usage_api import World, build_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

DAY = date(2026, 3, 10)
_service = AiUsageService()
_charts = AiUsageChartService()


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    return await build_world(db_session, "auc")


async def _log(
    db: AsyncSession,
    w: World,
    user: str,
    *,
    day: date = DAY,
    model: str = "lab-model-1",
    tool: str = "claude-code",
    project: str | None = "a",
    tokens: dict[str, int] | None = None,
    cost_type: str = "api",
) -> AiUsageEntry:
    """One entry through the service (by default 1M input and 100k output tokens of lab-model-1: 3 USD)."""
    entry = await _service.log(
        db,
        w.users[user],
        AiUsageInput(
            tool=tool,
            provider="testlab",
            model=model,
            usage_date=day,
            tokens=tokens or {"input": 1_000_000, "output": 100_000},
            cost_type=cost_type,
            project_id=getattr(w, project).id if project else None,
        ),
    )
    await db.commit()
    return entry


async def _backdate(db: AsyncSession, entry: AiUsageEntry, day: date) -> None:
    """Move *entry* to *day*, past what the service accepts (old data, or data written around it)."""
    await db.execute(update(AiUsageEntry).where(AiUsageEntry.id == entry.id).values(usage_date=day))
    await db.commit()


async def _price(db: AsyncSession, w: World, model: str, input_price: str = "2") -> None:
    await AiModelPriceService().create(
        db,
        PriceInput(
            provider="testlab",
            model=model,
            currency="USD",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            prices={"input": input_price, "output": "10"},
        ),
        actor=w.users["admin"],
    )
    await db.commit()


async def _chart(db: AsyncSession, user: User, **kw: Any) -> Any:
    return await _charts.charts(db, user, currency="USD", **kw)


def _chart_data(html: str, testid: str) -> dict[str, Any]:
    match = re.search(rf'data-testid="{testid}"[^>]*?data-sp-spend-chart=\'([^\']*)\'', html, re.S)
    assert match, testid
    data: dict[str, Any] = json.loads(match.group(1).replace("&#34;", '"').replace("&amp;", "&"))
    return data


async def _page(client: AsyncClient, user: User, query: str = "") -> str:
    cookies = {"access_token": _make_access_token(user, get_settings())}
    resp = await client.get(f"/my/ai-usage/{query}", cookies=cookies)
    assert resp.status_code == 200
    return resp.text


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


async def test_only_the_viewers_own_entries(db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", project=None)
    await _log(db_session, w, "bob")
    charts = await _chart(db_session, w.users["alice"])
    assert charts.entries == 2
    assert sum(b.api for b in charts.buckets) == Decimal("6")
    # Even an administrator sees only their own entries on their own page.
    assert (await _chart(db_session, w.users["admin"])).empty


async def test_day_buckets_list_empty_days_and_sum_per_day(db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice", day=DAY)
    await _log(db_session, w, "alice", day=DAY)
    await _log(db_session, w, "alice", day=DAY + timedelta(days=3))
    charts = await _chart(db_session, w.users["alice"])
    assert charts.bucket == "day"
    assert [b.start for b in charts.buckets] == [DAY + timedelta(days=i) for i in range(4)]
    assert [b.api for b in charts.buckets] == [Decimal("6"), 0, 0, Decimal("3")]
    assert [b.tokens["input"] for b in charts.buckets] == [2_000_000, 0, 0, 1_000_000]
    # The page dates filter the entries; the range is the days with entries.
    charts = await _chart(
        db_session, w.users["alice"], date_from=DAY - timedelta(days=2), date_to=DAY + timedelta(days=5)
    )
    assert len(charts.buckets) == 4 and (charts.first, charts.last) == (DAY, DAY + timedelta(days=3))


async def test_extreme_date_ranges_stay_bounded(db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    alice = w.users["alice"]
    charts = await _chart(db_session, alice, date_from=date.min, date_to=date.max)
    assert charts.bucket == "day" and len(charts.buckets) == 1 and charts.entries == 1
    # No matching entry: no buckets at all, whatever the range.
    assert (await _chart(db_session, alice, date_from=date(9999, 1, 1), date_to=date.max)).buckets == []
    # Decades of entries: one point per year, whatever bucket is asked for.
    await _backdate(db_session, await _log(db_session, w, "alice"), date(1990, 1, 1))
    for chosen in (None, "day", "week", "month", "hour"):
        charts = await _chart(db_session, alice, date_from=date.min, date_to=date.max, bucket=chosen)
        assert charts.bucket == "year" and len(charts.buckets) == 37 <= MAX_BUCKETS, chosen
    # Entries spread over more than MAX_BUCKETS years are not charted.
    await _backdate(db_session, await _log(db_session, w, "alice"), date(1850, 1, 1))
    charts = await _chart(db_session, alice, date_from=date.min, date_to=date.max, bucket="day")
    assert charts.too_long and charts.buckets == [] and charts.leverage == []


async def test_a_chosen_bucket_is_kept_only_within_the_cap(db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    await _log(db_session, w, "alice", day=date(2025, 1, 6))
    await _log(db_session, w, "alice", day=date(2026, 3, 10))
    assert (await _chart(db_session, alice)).bucket == "month"  # 14 months
    assert (await _chart(db_session, alice, bucket="week")).bucket == "week"  # 62 weeks
    assert (await _chart(db_session, alice, bucket="day")).bucket == "month"  # 429 days: too many
    charts = await _chart(db_session, alice, bucket="week")
    assert len(charts.buckets) == 62 and sum(b.entries for b in charts.buckets) == 2


async def test_long_ranges_use_weeks_then_months(db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice", day=date(2026, 1, 5))
    await _log(db_session, w, "alice", day=date(2026, 3, 11))
    charts = await _chart(db_session, w.users["alice"])
    assert charts.bucket == "week"
    assert charts.buckets[0].start == date(2026, 1, 5) and charts.buckets[-1].start == date(2026, 3, 9)
    assert sum(b.entries for b in charts.buckets) == 2
    await _log(db_session, w, "alice", day=date(2023, 1, 20))
    charts = await _chart(db_session, w.users["alice"])
    assert charts.bucket == "month"
    assert charts.buckets[0].start == date(2023, 1, 1) and len(charts.buckets) == 39
    # Past ten years of months: quarters (entries from before the service's own limits).
    await _backdate(db_session, await _log(db_session, w, "alice"), date(2012, 5, 1))
    charts = await _chart(db_session, w.users["alice"])
    assert charts.bucket == "quarter"
    assert charts.buckets[0].start == date(2012, 4, 1) and len(charts.buckets) == 56
    assert sum(b.entries for b in charts.buckets) == 4


async def test_stacks_by_tool_and_by_model(db_session: AsyncSession, w: World):
    await _price(db_session, w, "lab-model-2", input_price="4")
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", tool="codex", model="lab-model-2")
    charts = await _chart(db_session, w.users["alice"])
    bucket = charts.buckets[0]
    assert charts.tools == ["codex", "claude-code"]  # largest first
    assert charts.tool_labels == {"claude-code": "Claude Code", "codex": "Codex"}
    assert bucket.api_by_tool == {"claude-code": Decimal("3"), "codex": Decimal("5")}
    assert charts.models == ["lab-model-2", "lab-model-1"]
    assert bucket.api_by_model == {"lab-model-1": Decimal("3"), "lab-model-2": Decimal("5")}
    assert sum(bucket.api_by_tool.values()) == sum(bucket.api_by_model.values()) == bucket.api


async def test_top_models_plus_other(db_session: AsyncSession, w: World):
    count = TOP_N + 3
    for i in range(count):
        model = f"lab-top-{i:02d}"
        # Prices grow with i, so the largest models are the last ones.
        await _price(db_session, w, model, input_price=str(i + 1))
        await _log(db_session, w, "alice", model=model, project=None if i % 2 else "a")
    charts = await _chart(db_session, w.users["alice"])
    expected = [f"lab-top-{i:02d}" for i in range(count - 1, count - 1 - TOP_N, -1)]
    assert charts.models == [*expected, OTHER]
    # The three smallest: (1 + 2 + 3) USD of input plus 1 USD of output each.
    assert charts.buckets[0].api_by_model[OTHER] == Decimal("9")
    assert [s.key for s in charts.by_model] == [*expected, OTHER]
    assert charts.by_model[-1].api == Decimal("9") and charts.by_model[-1].label == OTHER
    assert sum(s.api for s in charts.by_model) == charts.buckets[0].api
    # Two projects only (AUCA and none): no "other".
    assert {s.key for s in charts.by_project} == {str(w.a.id), NO_PROJECT}
    assert {s.label for s in charts.by_project} == {w.a.key, NO_PROJECT}


async def test_filters_are_those_of_the_page(db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", tool="codex")
    await _log(db_session, w, "alice", project=None)
    dropped = await _log(db_session, w, "alice", day=DAY + timedelta(days=1))
    await _service.set_excluded(db_session, w.users["alice"], dropped, True)
    await db_session.commit()
    user = w.users["alice"]
    assert (await _chart(db_session, user)).entries == 3
    assert (await _chart(db_session, user, tool="codex")).entries == 1
    assert (await _chart(db_session, user, project=w.a)).entries == 2
    assert (await _chart(db_session, user, unassigned=True)).entries == 1
    assert (await _chart(db_session, user, cost_type="subscription")).empty
    assert (await _chart(db_session, user, date_from=DAY + timedelta(days=1))).empty
    excluded = await _chart(db_session, user, excluded=True)
    assert excluded.entries == 1 and excluded.buckets[0].start == DAY + timedelta(days=1)


async def test_unpriced_entries_are_counted_per_bucket(db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", model="lab-unknown-9", day=DAY + timedelta(days=2))
    await _log(db_session, w, "alice", model="lab-unknown-9", day=DAY + timedelta(days=2))
    charts = await _chart(db_session, w.users["alice"])
    assert charts.unpriced == 2
    assert [b.unpriced for b in charts.buckets] == [0, 0, 2]
    assert charts.buckets[2].api == 0


async def test_leverage_per_plan(db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    db_session.add(
        AiSubscription(
            user_id=alice.id,
            provider="testlab",
            plan_key="custom",
            plan_name="Lab Plan",
            tool_slugs=["claude-code"],
            amount=Decimal("20"),
            currency_code="USD",
            billing_period="month",
            starts_on=date(2026, 1, 1),
        )
    )
    await db_session.commit()
    first = await _log(db_session, w, "alice", cost_type="subscription")
    assert first.subscription_id is not None
    await _log(db_session, w, "alice", cost_type="subscription")
    period = {"date_from": date(2026, 3, 1), "date_to": date(2026, 3, 31)}
    charts = await _chart(db_session, alice, **period)
    assert len(charts.leverage) == 1
    plan = charts.leverage[0]
    assert (plan.plan_name, plan.fee, plan.api, plan.leverage) == (
        "Lab Plan",
        Decimal("20"),
        Decimal("6"),
        Decimal("0.30"),
    )
    # A filter that leaves part of the plan's usage out of view leaves leverage out.
    assert (await _chart(db_session, alice, tool="claude-code", **period)).leverage == []
    assert (await _chart(db_session, alice, project=w.a, **period)).leverage == []
    # Nobody else's plans.
    await _log(db_session, w, "bob")
    assert (await _chart(db_session, w.users["bob"], **period)).leverage == []


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


async def test_page_draws_the_charts(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", day=DAY + timedelta(days=1), tool="codex", project=None)
    await _log(db_session, w, "bob", tool="cursor")
    html = await _page(client, w.users["alice"])
    assert 'data-testid="aiu-charts"' in html
    assert "spend-chart.min" in html
    time = _chart_data(html, "aiu-chart-time")
    assert time["kind"] == "usage-time" and time["currency"] == "USD"
    assert [p["label"] for p in time["periods"]] == ["Mar 10", "Mar 11"]
    assert time["stacks"]["tool"]["names"] == ["Claude Code", "Codex"]  # no Cursor: Bob's entry
    assert time["stacks"]["tool"]["values"] == [[3.0, 0.0], [0.0, 3.0]]
    assert time["stacks"]["model"]["names"] == ["lab-model-1"]
    assert time["spend"] == [3.0, 3.0]
    fresh = _chart_data(html, "aiu-chart-tokens-fresh")
    assert [s["name"] for s in fresh["series"]] == ["Input", "Output"]
    assert 'data-testid="aiu-chart-tokens-cache"' not in html  # no cache tokens
    projects = _chart_data(html, "aiu-chart-projects")
    assert sorted(p["label"] for p in projects["points"]) == ["AUCA", "Unassigned"]
    assert 'data-testid="aiu-chart-switch"' in html and 'data-testid="aiu-leverage"' not in html


async def test_page_marks_unpriced_periods(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", model="lab-unknown-9", day=DAY + timedelta(days=1))
    html = await _page(client, w.users["alice"])
    time = _chart_data(html, "aiu-chart-time")
    assert [p["note"] for p in time["periods"]] == ["", "1 entry without a price"]
    assert 'data-testid="aiu-chart-unpriced"' in html
    assert 'data-testid="aiu-unpriced-note"' in html  # the totals note stays


async def test_page_follows_the_filters(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    await _log(db_session, w, "alice", tool="codex", day=DAY + timedelta(days=1))
    html = await _page(client, w.users["alice"], "?tool=codex&from_date=2026-03-01&to_date=2026-03-31")
    time = _chart_data(html, "aiu-chart-time")
    assert [p["label"] for p in time["periods"]] == ["Mar 11"]
    assert time["stacks"]["tool"]["names"] == ["Codex"]
    await _log(db_session, w, "alice", day=date(2026, 1, 1))
    html = await _page(client, w.users["alice"], "?from_date=2026-01-01&to_date=2026-06-30")
    assert _chart_data(html, "aiu-chart-time")["periods"][0]["title"] == "Week of Dec 29, 2025"


async def test_page_with_an_extreme_range(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    html = await _page(client, w.users["alice"], "?from_date=0001-01-01&to_date=9999-12-31")
    assert len(_chart_data(html, "aiu-chart-time")["periods"]) == 1
    await _backdate(db_session, await _log(db_session, w, "alice"), date(1850, 1, 1))
    html = await _page(client, w.users["alice"], "?from_date=0001-01-01&to_date=9999-12-31&bucket=day")
    assert 'data-testid="aiu-charts-too-long"' in html and "spend-chart.min" not in html


async def test_page_without_counted_usage(client: AsyncClient, db_session: AsyncSession, w: World):
    html = await _page(client, w.users["alice"])
    assert 'data-testid="aiu-charts"' not in html and "spend-chart.min" not in html
    assert 'data-testid="aiu-charts-empty"' not in html  # the page's own empty state says it
    entry = await _log(db_session, w, "alice")
    await _service.set_excluded(db_session, w.users["alice"], entry, True)
    await db_session.commit()
    html = await _page(client, w.users["alice"])
    assert 'data-testid="aiu-charts-empty"' in html and "spend-chart.min" not in html
    # Showing the excluded entries charts them.
    html = await _page(client, w.users["alice"], "?status=excluded")
    assert 'data-testid="aiu-charts"' in html


async def test_page_lists_leverage_per_plan(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    db_session.add(
        AiSubscription(
            user_id=alice.id,
            provider="testlab",
            plan_key="custom",
            plan_name="Lab Plan",
            tool_slugs=["claude-code"],
            amount=Decimal("20"),
            currency_code="USD",
            billing_period="month",
            starts_on=date(2026, 1, 1),
        )
    )
    await db_session.commit()
    await _log(db_session, w, "alice", cost_type="subscription")
    html = await _page(client, alice, "?from_date=2026-03-01&to_date=2026-03-31")
    assert 'data-testid="aiu-leverage"' in html
    row = re.search(r'data-testid="aiu-leverage-row">(.*?)</tr>', html, re.S)
    assert row and "Lab Plan" in row.group(1) and "$20.00" in row.group(1) and "0.15×" in row.group(1)


async def test_page_bucket_switch(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice", day=date(2025, 1, 6))
    await _log(db_session, w, "alice", day=date(2026, 3, 10))
    html = await _page(client, w.users["alice"], "?bucket=week")
    assert len(_chart_data(html, "aiu-chart-time")["periods"]) == 62
    assert 'aria-current="true" data-bucket="week"' in html
    assert '<input type="hidden" name="bucket" value="week">' in html
    # A size that would draw too many points is not honoured; nor is a made-up one.
    for query in ("?bucket=day", "?bucket=hour"):
        html = await _page(client, w.users["alice"], query)
        assert len(_chart_data(html, "aiu-chart-time")["periods"]) == 15, query
        assert _chart_data(html, "aiu-chart-time")["periods"][0]["label"] == "Jan 2025"


async def test_cached_charts_follow_new_entries(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    first = _chart_data(await _page(client, w.users["alice"]), "aiu-chart-time")
    assert first == _chart_data(await _page(client, w.users["alice"]), "aiu-chart-time")
    await _log(db_session, w, "alice", tool="codex")
    again = _chart_data(await _page(client, w.users["alice"]), "aiu-chart-time")
    assert again["stacks"]["tool"]["names"] == ["Claude Code", "Codex"]


async def test_spend_report_periods_are_capped_whoever_asks(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice")
    report = await AiSpendReportService().report(
        db_session, w.users["mgr"], project=w.a, date_from=date(2024, 1, 1), date_to=date(2026, 12, 31), period="day"
    )
    assert (report.period, report.requested_period) == ("month", "day")
    cookies = {"access_token": _make_access_token(w.users["mgr"], get_settings())}
    resp = await client.get(
        f"/projects/{w.a.key}/ai-usage/report/?from_date=2026-01-01&to_date=2026-06-30&period=day", cookies=cookies
    )
    assert resp.status_code == 200
    assert "Grouped by week instead of day: the range would have too many periods." in resp.text
