"""AI spend reports over REST (JSON and CSV) and on the web: the project report, the admin page and drill-through.

The world of the service tests (``tests/services/test_ai_spend_reports.py``)
with its own prefix: projects ``RWA`` and ``RWB``.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from urllib.parse import parse_qs

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from tests.services.test_ai_spend_reports import World, build_world, entry, rate

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MARCH = {"from_date": "2026-03-01", "to_date": "2026-03-31"}


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "rw")
    db = db_session
    sub = world.subs["alice"]
    await entry(
        db, world.alice, project=world.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub
    )
    await entry(
        db, world.alice, project=world.q, day=date(2026, 3, 6), api="10", cost_type="subscription", subscription=sub
    )
    await entry(
        db, world.bob, project=world.p, day=date(2026, 3, 7), api="4", issue=world.public_issue, branch="feat/x"
    )
    await entry(db, world.bob, project=None, day=date(2026, 3, 8), api="2", model="lab-model-2")
    await db.commit()
    return world


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _get(client: AsyncClient, user: User, url: str, **params: str) -> Response:
    return await client.get(url, params=params, cookies=_cookies(user), follow_redirects=False)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_project_report_json(client: AsyncClient, w: World) -> None:
    resp = await _get(
        client, w.mgr, "/api/v1/projects/RWA/ai-usage/report/", **MARCH, group_by="user,model", period="month"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "project" and body["project_key"] == "RWA"
    assert body["group_by"] == ["user", "model"] and body["period"] == "month"
    assert body["currency"] == "USD" and body["seat_cost_shown"] and not body["unallocated_shown"]
    alice = next(r for r in body["rows"] if r["key"]["user"] == w.alice.id)
    assert alice["labels"]["user"] == "Rep Alice"
    assert alice["allocated_seat_cost"]["total"] == {"amount": "75.00", "currency": "USD"}
    assert alice["total_spend"]["total"]["amount"] == "75.00"
    assert alice["api_equivalent_value"]["total"]["amount"] == "30.00"
    assert alice["leverage"] == "0.40"
    assert alice["period"] == "2026-03-01"
    assert body["totals"]["entries"] == 2


async def test_partial_viewer_gets_no_seat_cost(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.alice, "/api/v1/projects/RWA/ai-usage/report/", **MARCH)
    body = resp.json()
    assert body["seat_cost_hidden"] and not body["seat_cost_shown"]
    assert [r["key"]["user"] for r in body["rows"]] == [w.alice.id]
    assert body["rows"][0]["allocated_seat_cost"] is None
    assert "Secret plan" not in resp.text


async def test_instance_report_is_admin_only(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.mgr, "/api/v1/ai-usage/report/", **MARCH)
    assert resp.status_code == 403
    resp = await _get(client, w.admin, "/api/v1/ai-usage/report", **MARCH, group_by="project")
    if resp.status_code in (307, 308):
        resp = await _get(client, w.admin, "/api/v1/ai-usage/report/", **MARCH, group_by="project")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "instance" and body["unallocated_shown"]
    assert any(r["seat_only"] for r in body["rows"])  # dave's fee and bob's
    assert {r["labels"]["project"] for r in body["rows"] if not r["seat_only"]} == {"RWA", "RWB", ""}


async def test_report_route_is_not_an_entry_id(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.alice, "/api/v1/ai-usage/report/")
    assert resp.status_code == 403  # the report route, not a 422 on entry_id


async def test_bad_parameters(client: AsyncClient, w: World) -> None:
    for params in ({"group_by": "salary"}, {"period": "year"}, {"rate_policy": "soon"}):
        resp = await _get(client, w.admin, "/api/v1/ai-usage/report/", **MARCH, **params)
        assert resp.status_code == 422, params


async def test_csv_export(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.admin, "/api/v1/ai-usage/report/", **MARCH, group_by="user,branch", format="csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="ai-spend-instance-2026-03-01-2026-03-31.csv"' in resp.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert rows[-1]["period"] == "total"
    bob = next(r for r in rows if r["user"] == "Rep Bob" and r["branch"] == "feat/x")
    assert bob["currency"] == "USD" and bob["marginal_spend"] == "4.00" and bob["api_equivalent_value"] == "4.00"
    seat = [r for r in rows if r["branch"] == "(unallocated seat)"]
    assert seat and all(r["entries"] == "0" for r in seat)
    assert "Secret plan" not in resp.text


async def test_csv_lists_unconverted_amounts(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 9), api="100", currency="THB")
    await db_session.commit()
    resp = await _get(client, w.mgr, "/api/v1/projects/RWA/ai-usage/report/", **MARCH, group_by="", format="csv")
    total = list(csv.DictReader(io.StringIO(resp.text)))[-1]
    assert "marginal_spend: 100.00 THB (no_rate)" in total["not_converted"]
    assert "api_equivalent_value: 100.00 THB (no_rate)" in total["not_converted"]


async def test_currency_conversion_shows_the_rate_dates(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await rate(db_session, date(2026, 3, 1), "USD", "THB", "35")
    await db_session.commit()
    resp = await _get(
        client, w.mgr, "/api/v1/projects/RWA/ai-usage/report/", **MARCH, currency="THB", include_seat_cost="false"
    )
    body = resp.json()
    assert body["currency"] == "THB"
    assert body["rate_dates"] == ["2026-03-01"]
    assert body["totals"]["api_equivalent_value"]["total"] == {"amount": "1190.00", "currency": "THB"}


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


async def test_project_report_page(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.mgr, "/projects/RWA/ai-usage/report/", **MARCH, group="user", period="week")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="spend-table"' in html
    assert "data-sp-spend-chart='" in html
    assert "/static/dist/js/spend-chart.min" in html  # the chart bundle, loaded on this page only
    assert "Rep Alice" in html and "Rep Bob" in html
    assert 'data-testid="spend-seat"' in html
    assert "/api/v1/projects/RWA/ai-usage/report/?" in html and "format=csv" in html
    assert "<script>" not in html.split('data-testid="spend-presets"', 1)[1]


async def test_report_pages_note_unpriced_entries(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    resp = await _get(client, w.mgr, "/projects/RWA/ai-usage/report/", **MARCH)
    assert 'data-testid="aiu-unpriced-note"' not in resp.text

    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 9), model="lab-unpriced-9")
    await db_session.commit()
    resp = await _get(client, w.mgr, "/projects/RWA/ai-usage/report/", **MARCH)
    html = resp.text
    assert 'data-testid="aiu-unpriced-note"' in html
    assert "1 of 3 entries has no price for its model and date, so it counts as 0." in html
    assert "<code>lab-unpriced-9</code> (1)" in html
    assert 'data-testid="aiu-unpriced-prices-link"' not in html  # not an administrator

    resp = await _get(client, w.admin, "/admin/ai-spend/", **MARCH)
    assert "1 of 5 entries has no price for its model and date, so it counts as 0." in resp.text
    assert 'href="/admin/ai-model-prices/" data-testid="aiu-unpriced-prices-link"' in resp.text

    body = (await _get(client, w.mgr, "/api/v1/projects/RWA/ai-usage/report/", **MARCH)).json()
    assert body["totals"]["unpriced_entries"] == 1


async def test_report_without_rows_skips_the_chart_bundle(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.mgr, "/projects/RWA/ai-usage/report/", from_date="2025-01-01", to_date="2025-01-31")
    assert resp.status_code == 200, resp.text
    assert 'data-testid="spend-empty"' in resp.text
    assert "spend-chart.min" not in resp.text
    assert "data-sp-spend-chart" not in resp.text


async def test_other_pages_skip_the_chart_bundle(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.mgr, "/projects/RWA/")
    assert resp.status_code == 200, resp.text
    assert "spend-chart.min" not in resp.text
    # The AI usage tab draws charts only when its filters select usage.
    resp = await _get(client, w.mgr, "/projects/RWA/ai-usage/", status="excluded")
    assert resp.status_code == 200, resp.text
    assert "spend-chart.min" not in resp.text


async def test_project_report_page_hides_seat_cost(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.alice, "/projects/RWA/ai-usage/report/", **MARCH)
    assert resp.status_code == 200
    assert 'data-testid="spend-seat-hidden"' in resp.text
    assert 'data-testid="spend-seat"' not in resp.text
    assert "Rep Bob" not in resp.text
    assert "Secret plan" not in resp.text


async def test_drill_through_to_the_project_tab(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.mgr, "/projects/RWA/ai-usage/report/", **MARCH, group="issue", group2="branch")
    hrefs = [
        seg.split('"', 1)[0].replace("&amp;", "&") for seg in resp.text.split('href="/projects/RWA/ai-usage/?')[1:]
    ]
    assert hrefs
    queries = [parse_qs(h) for h in hrefs]
    issue_row = next(q for q in queries if q.get("issue") == ["RWA-1"])
    assert issue_row["branch"] == ["feat/x"] and issue_row["status"] == ["counted"]
    no_issue = next(q for q in queries if q.get("issue") == ["-"])
    assert no_issue["branch"] == ["-"]

    tab = await _get(client, w.mgr, "/projects/RWA/ai-usage/", **{k: v[0] for k, v in issue_row.items()})
    assert tab.status_code == 200
    assert 'data-testid="aiu-branch-filter"' in tab.text
    assert tab.text.count('data-entry-id="') == 1
    tab = await _get(client, w.mgr, "/projects/RWA/ai-usage/", **{k: v[0] for k, v in no_issue.items()})
    assert tab.text.count('data-entry-id="') == 1  # alice's entry without issue and branch


async def test_admin_page_and_entries(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.admin, "/admin/ai-spend/", **MARCH, group="project")
    assert resp.status_code == 200, resp.text
    assert 'data-testid="spend-unallocated"' in resp.text
    assert 'href="/admin/ai-subscriptions/"' in resp.text  # unallocated seat rows, not split by person
    hrefs = [
        seg.split('"', 1)[0].replace("&amp;", "&") for seg in resp.text.split('href="/admin/ai-spend/entries/?')[1:]
    ]
    unassigned = next(parse_qs(h) for h in hrefs if parse_qs(h).get("project") == ["-"])
    page = await _get(client, w.admin, "/admin/ai-spend/entries/", **{k: v[0] for k, v in unassigned.items()})
    assert page.status_code == 200
    assert page.text.count('data-entry-id="') == 1
    assert "lab-model-2" in page.text


async def test_admin_pages_refuse_others(client: AsyncClient, w: World) -> None:
    for url in ("/admin/ai-spend/", "/admin/ai-spend/entries/"):
        resp = await _get(client, w.mgr, url)
        assert resp.status_code in (303, 307, 403), url


async def test_bad_dates_render_an_error(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.admin, "/admin/ai-spend/", from_date="2020-01-01", to_date="2026-03-01")
    assert resp.status_code == 422
    assert 'data-testid="spend-error"' in resp.text


async def test_admin_dashboard_links_the_report(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.admin, "/admin/")
    assert 'href="/admin/ai-spend/"' in resp.text


async def test_project_tab_links_the_report(client: AsyncClient, w: World) -> None:
    resp = await _get(client, w.alice, "/projects/RWA/ai-usage/")
    assert 'href="/projects/RWA/ai-usage/report/"' in resp.text
