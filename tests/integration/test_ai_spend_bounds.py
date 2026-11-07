"""Every per-person list of the AI spend pages stays bounded on an instance with many people.

The world of the service tests (``tests/services/test_ai_spend_reports.py``,
prefix ``bl``: projects ``BLA`` and ``BLB``) plus a crowd of
:data:`CROWD` people, each with one pay-per-use entry in ``BLA`` in March 2026
worth ``i + 1`` USD (so every person has a different spend), and a system
account with an entry there too. Each test checks one list: the person filter
(a search, never a list of everyone), the person cards, the per-period table,
the leverage tables, the MCP text and the JSON rows, and that the totals stay
exact.
"""

from __future__ import annotations

import csv
import html as html_lib
import io
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import AiSpendReportService, ReportFilters, fold_rows
from specivo.services.ai_usage_chart_service import LEVERAGE_TOP, AiUsageChartService
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from specivo.web.pages.ai_spend_reports import DRILL_PERIOD_GROUPS, PEOPLE_TOP
from tests.factories.user import UserFactory
from tests.services.test_ai_spend_reports import World, build_world, entry, rate

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

CROWD = 300
#: People with an entry in BLA: the crowd, alice and the system account (reports count its usage;
#: only the person filter leaves system accounts out).
IN_BLA = CROWD + 2
MARCH_1, MARCH_31 = date(2026, 3, 1), date(2026, 3, 31)
MARCH = {"from_date": "2026-03-01", "to_date": "2026-03-31"}
ADMIN = "/admin/ai-spend/"
PROJECT = "/projects/BLA/ai-usage/report/"
TAB = "/projects/BLA/ai-usage/"


def _usage(user: User, project: Project | None, day: date, api: Decimal, **kw: Any) -> AiUsageEntry:
    """An entry built in memory (the crowd is added in one flush)."""
    cost_type = kw.pop("cost_type", "api")
    return AiUsageEntry(
        project_id=project.id if project else None,
        user_id=user.id,
        reporter_user_id=user.id,
        source="manual",
        attribution_method="explicit" if project else "none",
        tool_slug="claude-code",
        provider="testlab",
        model="lab-model-1",
        usage_date=day,
        request_count=1,
        input=1000,
        output=100,
        cost_type=cost_type,
        priced_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
        marginal_amount=Decimal(0) if cost_type == "subscription" else api,
        marginal_currency="USD",
        api_equivalent_amount=api,
        api_equivalent_currency="USD",
        cost_basis="calculated",
        **kw,
    )


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "bl")
    db = db_session
    crowd = [
        UserFactory.build(login=f"bl_c{i:03d}", display_name=f"Crowd {i:03d}", status="active") for i in range(CROWD)
    ]
    system = UserFactory.build(login="$bl_bot", display_name="Bl Bot", status="active", password_hash=None)
    outsider = UserFactory.build(login="bl_outsider", display_name="Bl Outsider", status="active")
    db.add_all([*crowd, system, outsider])
    await db.flush()
    db.add_all([_usage(u, world.p, date(2026, 3, 1 + i % 28), Decimal(i + 1)) for i, u in enumerate(crowd)])
    db.add(_usage(system, world.p, date(2026, 3, 3), Decimal("0.5")))
    db.add(_usage(outsider, world.q, date(2026, 3, 3), Decimal("0.5")))
    await entry(db, world.alice, project=world.p, day=date(2026, 3, 5), api="3")
    await db.commit()
    world.users.update({"system": system, "outsider": outsider, "low": crowd[0], "high": crowd[-1]})
    clear_role_cache(db)
    return world


TOTAL_300 = 'data-testid="spend-total"><span class="sp-aiu-amount">$300.00<'


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _page(client: AsyncClient, reader: User, url: str, **params: str) -> str:
    resp = await client.get(url, params=params, cookies=_cookies(reader), follow_redirects=False)
    assert resp.status_code == 200, resp.text[:500]
    return resp.text


def _section(html: str, testid: str) -> str:
    start = html.index(f'data-testid="{testid}"')
    ends = [i for i in (html.find("</table>", start), html.find("</section>", start)) if i != -1]
    return html[start : min(ends) if ends else len(html)]


async def _people(client: AsyncClient, user: User, url: str, **params: Any) -> list[str]:
    resp = await client.get(url, params=params, headers=_bearer(user))
    assert resp.status_code == 200, resp.text[:300]
    return [p["login"] for p in resp.json()]


# ---------------------------------------------------------------------------
# The person filter: a search, scoped to the people the reader may see
# ---------------------------------------------------------------------------


async def test_report_pages_offer_a_person_search_not_a_list(client: AsyncClient, w: World) -> None:
    for user, url in ((w.admin, ADMIN), (w.mgr, PROJECT), (w.mgr, TAB)):
        html = await _page(client, user, url, **MARCH)
        assert '<select name="user_id"' not in html
        field = _section(html, "aiu-person-filter")
        assert 'role="combobox"' in field and 'name="user"' in field
        assert "report/people/" in field
        # The low spender is in no list of the page: not the filter, not the cards, not the tables.
        assert "Crowd 000" not in html


async def test_project_person_search_names_only_people_the_reader_may_see(client: AsyncClient, w: World) -> None:
    url = "/api/v1/projects/BLA/ai-usage/report/people/"
    found = await _people(client, w.mgr, url, q="bl_", limit=50)
    assert len(found) == 50  # a page, never everyone
    everyone: list[str] = []
    for offset in range(0, CROWD + 50, 50):
        everyone += await _people(client, w.mgr, url, q="bl_", limit=50, offset=offset)
    assert len(everyone) == len(set(everyone)) == IN_BLA - 1  # the crowd and alice, not the bot
    assert "$bl_bot" not in everyone and "bl_outsider" not in everyone  # no system account, nobody outside BLA
    assert await _people(client, w.mgr, url, q="Crowd 29") == [f"bl_c{i}" for i in range(290, 300)]
    # alice reads only her own entries: she finds only herself.
    assert await _people(client, w.alice, url, q="bl_") == ["bl_alice"]
    # The instance search is for administrators, and still leaves system accounts out.
    instance = "/api/v1/ai-usage/report/people/"
    assert await _people(client, w.admin, instance, q="outsider") == ["bl_outsider"]
    assert await _people(client, w.admin, instance, q="bot") == []
    assert await _people(client, w.admin, instance, q="bl_dave") == ["bl_dave"]  # a plan, no entries
    resp = await client.get(instance, params={"q": "bl"}, headers=_bearer(w.mgr))
    assert resp.status_code == 403


async def test_chosen_person_is_a_chip_and_a_typed_login_works_without_js(client: AsyncClient, w: World) -> None:
    high = w.users["high"]
    html = await _page(client, w.mgr, PROJECT, **MARCH, user_id=str(high.id))
    chip = _section(html, "aiu-person-filter")
    assert "Crowd 299" in chip and f'name="user_id" value="{high.id}"' in chip
    clear = re.search(r'href="([^"]+)" class="sp-filter-chip-clear"', chip)
    assert clear and "user_id" not in html_lib.unescape(clear.group(1))
    assert TOTAL_300 in html

    # Without JS the field sends the login as "user".
    html = await _page(client, w.mgr, PROJECT, **MARCH, user="BL_C299")
    assert TOTAL_300 in html and "Crowd 299" in _section(html, "aiu-person-filter")
    html = await _page(client, w.mgr, TAB, **MARCH, user="bl_c299")
    assert "Crowd 299" in _section(html, "aiu-person-filter")
    assert html.count("data-entry-id=") == 1
    # Someone outside the project, an unknown login or a system account: nothing matches, nobody is named.
    for login in ("bl_outsider", "nobody-here", "$bl_bot"):
        html = await _page(client, w.mgr, PROJECT, **MARCH, user=login)
        assert 'data-testid="spend-empty"' in html
        assert "Bl Outsider" not in html and "Bl Bot" not in html
    # An id of someone the project report may not name gets no name either.
    html = await _page(client, w.mgr, PROJECT, **MARCH, user_id=str(w.outsider.id))
    assert "Bl Outsider" not in html and "Unknown person" in html


# ---------------------------------------------------------------------------
# Spend by person: the top people and one summary card
# ---------------------------------------------------------------------------


async def test_person_cards_show_the_top_and_sum_the_rest(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH)
    people = _section(html, "spend-people")
    names = re.findall(r'class="sp-person-name">([^<]+)<', people)
    assert len(re.findall(r'data-testid="spend-person"', people)) == PEOPLE_TOP
    assert names[:3] == ["Crowd 299", "Crowd 298", "Crowd 297"]

    clear_role_cache(db_session)
    report = await AiSpendReportService().report(
        db_session, w.admin, project=None, date_from=MARCH_1, date_to=MARCH_31, group_by=("user",)
    )
    rows = sorted(
        (r for r in report.rows if r.key.get("user") is not None),
        key=lambda r: -(r.total_spend.total.amount if r.total_spend else Decimal(0)),
    )
    rest = rows[PEOPLE_TOP:]
    summed = fold_rows(rest, report.currency, report.seat_cost_shown)
    assert summed.total_spend is not None
    assert f"{len(rest)} more people" in people
    more = re.search(r'data-testid="spend-people-more-spend">([^<]+)<', people)
    assert more and more.group(1) == summed.total_spend.total.format("en")
    # The cards and the summary add up to the report's spend.
    top_total = sum((r.total_spend.total.amount for r in rows[:PEOPLE_TOP] if r.total_spend), Decimal(0))
    assert report.totals.total_spend is not None
    unassigned = sum(
        (r.total_spend.total.amount for r in report.rows if r.key.get("user") is None and r.total_spend), Decimal(0)
    )
    assert top_total + summed.total_spend.total.amount + unassigned == report.totals.total_spend.total.amount
    # The summary opens the Person level, largest first.
    link = re.search(r'href="([^"]+)"[^>]*data-testid="spend-people-more-open"', people)
    assert link and "group=user" in html_lib.unescape(link.group(1)) and "sort=spend" in link.group(1)


# ---------------------------------------------------------------------------
# The table with a period: the largest groups per period, the rest summed
# ---------------------------------------------------------------------------


async def test_period_table_folds_small_groups_per_period(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, PROJECT, **MARCH, group="user", period="week")
    table = _section(html, "spend-table").split("<tfoot>")[0]
    rows = len(re.findall(r'data-testid="spend-row"', table))
    others = len(re.findall(r'data-testid="spend-period-other"', table))
    weeks = 5  # 2026-02-23 .. 2026-03-23 (Mondays)
    assert rows <= DRILL_PERIOD_GROUPS * weeks and 0 < others <= weeks
    assert 'data-testid="spend-period-note"' in html
    assert f"the other {IN_BLA - DRILL_PERIOD_GROUPS} groups" in html
    # Every entry is in a row or an "other" row: the entries add up to the total.
    entries = [int(n) for n in re.findall(r'data-label="Entries">(\d+)', table)]
    total = re.search(r'sp-spend-total-row.*?data-label="Entries">(\d+)<', html, re.S)
    assert total and sum(entries) == int(total.group(1)) == IN_BLA
    # The CSV keeps every row.
    csv_link = re.search(r'href="([^"]+)"[^>]*data-testid="spend-csv"', html)
    assert csv_link
    resp = await client.get(html_lib.unescape(csv_link.group(1)), cookies=_cookies(w.mgr))
    lines = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(lines) == IN_BLA + 1  # one row per person (one entry each), then the total


async def test_period_table_without_many_groups_is_whole(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, PROJECT, **MARCH, group="tool", period="week")
    assert 'data-testid="spend-period-other"' not in html and 'data-testid="spend-period-note"' not in html


# ---------------------------------------------------------------------------
# Subscription leverage: the largest plans and people, then "others"
# ---------------------------------------------------------------------------


async def test_leverage_tables_list_the_top_and_sum_the_rest(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await _holders(db, w, 14)
    charts = await AiUsageChartService().report_charts(
        db, w.admin, project=None, date_from=MARCH_1, date_to=MARCH_31, currency="USD"
    )
    # bob's fee is in EUR without a rate: the totals and the "others" row holding it show no fee or ratio.
    assert charts.leverage_total is not None and charts.leverage_total.fee is None
    assert charts.people_others is not None and charts.people_others.fee is None

    await rate(db, MARCH_31, "EUR", "USD", "1.1")
    await db.commit()
    charts = await AiUsageChartService().report_charts(
        db, w.admin, project=None, date_from=MARCH_1, date_to=MARCH_31, currency="USD"
    )
    total = charts.leverage_total
    assert total is not None and total.fee is not None
    # Plans: 14 crowd plans plus alice's, bob's and dave's; one holder each.
    assert len(charts.leverage) == LEVERAGE_TOP and charts.leverage_others_count == 14 + 3 - LEVERAGE_TOP
    assert len(charts.people) == LEVERAGE_TOP and charts.people_others_count == 14 + 3 - LEVERAGE_TOP
    # The largest fee first; bob's yearly EUR plan (110 USD for March) leads.
    assert charts.people[0].label == "Rep Bob" and charts.leverage[0].holder == "Rep Bob"
    for listed, others in ((charts.people, charts.people_others), (charts.leverage, charts.leverage_others)):
        assert others is not None and others.fee is not None
        assert sum((p.fee or Decimal(0) for p in listed), Decimal(0)) + others.fee == total.fee
        assert sum((p.api for p in listed), Decimal(0)) + others.api == total.api
        assert others.leverage == (others.api / others.fee).quantize(Decimal("0.01"))


async def test_leverage_tables_on_the_admin_page(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await _holders(db_session, w, 14)
    html = await _page(client, w.admin, ADMIN, **MARCH, bucket="week")
    plans = _section(html, "spend-leverage-plans")
    assert len(re.findall(r'data-testid="spend-leverage-plan"', plans)) == LEVERAGE_TOP
    assert "Others (7 plans)" in plans
    people = _section(html, "spend-leverage-people")
    assert len(re.findall(r'data-testid="spend-leverage-person"', people)) == LEVERAGE_TOP
    assert "Others (7 people)" in people


async def _holders(db: AsyncSession, w: World, count: int) -> list[User]:
    """*count* crowd members with a 20 USD plan in March and a subscription entry worth 10 + i USD on it."""
    holders = []
    for i in range(count):
        login = f"bl_c{CROWD - 1 - i:03d}"
        user = (await db.execute(select(User).where(User.login == login))).scalar_one()
        sub = AiSubscription(
            user_id=user.id,
            provider="testlab",
            plan_key="custom",
            plan_name=f"Crowd plan {i:02d}",
            tool_slugs=["claude-code"],
            amount=Decimal(20),
            currency_code="USD",
            billing_period="month",
            starts_on=MARCH_1,
        )
        db.add(sub)
        await db.flush()
        db.add(
            _usage(
                user,
                w.p,
                date(2026, 3, 10),
                Decimal(10 + i),
                cost_type="subscription",
                subscription_id=sub.id,
            )
        )
        holders.append(user)
    await db.commit()
    clear_role_cache(db)
    return holders


# ---------------------------------------------------------------------------
# MCP and JSON
# ---------------------------------------------------------------------------


async def test_mcp_report_sums_the_rows_past_its_limit(db_session: AsyncSession, w: World) -> None:
    from specivo.mcp.ai_usage_tools import REPORT_MAX_ROWS, _ai_usage_report

    clear_role_cache(db_session)
    text = await _ai_usage_report(
        db_session,
        w.admin,
        project_key=None,
        date_from="2026-03-01",
        date_to="2026-03-31",
        preset=None,
        group_by="user",
        period=None,
        currency="USD",
        rate_policy=None,
        include_seat_cost=False,
        user_login=None,
        tool=None,
        model=None,
        cost_type=None,
        sort="spend",
    )
    lines = text.splitlines()
    assert sum(1 for line in lines if line.startswith("  user=")) == REPORT_MAX_ROWS
    more = next(line for line in lines if line.startswith("  ... and "))
    # 303 people (the crowd, alice, the bot and the outsider), largest first: past the limit the 103
    # smallest, crowd 1..100 USD, alice 3, the bot and the outsider 0.5 each.
    assert more.startswith("  ... and 103 more rows, together: entries=103 ")
    assert f"spend={sum(range(1, 101)) + 4}.00 USD" in more


async def test_json_report_is_a_page_with_every_total(client: AsyncClient, w: World) -> None:
    params = {**MARCH, "group_by": "user", "limit": "50", "currency": "USD"}
    resp = await client.get("/api/v1/projects/BLA/ai-usage/report/", params=params, headers=_bearer(w.mgr))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert len(body["rows"]) == 50 and body["row_count"] == IN_BLA and body["limit"] == 50
    assert body["totals"]["entries"] == IN_BLA
    resp = await client.get(
        "/api/v1/projects/BLA/ai-usage/report/",
        params={**params, "offset": str(IN_BLA - 1)},
        headers=_bearer(w.mgr),
    )
    assert len(resp.json()["rows"]) == 1


# ---------------------------------------------------------------------------
# Facts behind the table: only for the rows shown
# ---------------------------------------------------------------------------


async def test_drill_facts_cover_only_the_rows_asked_for(db_session: AsyncSession, w: World) -> None:
    clear_role_cache(db_session)
    facts = await AiSpendReportService().drill_facts(
        db_session,
        w.mgr,
        project=w.p,
        date_from=MARCH_1,
        date_to=MARCH_31,
        filters=ReportFilters(),
        dim="user",
        users=[w.users["high"].id],
    )
    assert facts.people == IN_BLA
    assert set(facts.usage) == {w.users["high"].id}
    facts = await AiSpendReportService().drill_facts(
        db_session,
        w.mgr,
        project=w.p,
        date_from=MARCH_1,
        date_to=MARCH_31,
        filters=ReportFilters(),
        dim="tool",
        keys=["claude-code"],
    )
    assert facts.people_by_key == {"claude-code": IN_BLA} and facts.usage == {}
