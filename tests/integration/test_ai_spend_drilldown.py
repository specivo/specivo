"""The drill-down and the "Spend by person" cards of the AI spend report pages, and the narrowing filters behind them.

The world of the service tests (``tests/services/test_ai_spend_reports.py``)
with its own prefix: projects ``RDA`` and ``RDB``. March 2026:

- alice, subscription (100 USD a month): RDA (API value 30, on the public
  issue, branch ``feat/a``) and RDB (10), so RDA carries 75 of her fee, RDB 25;
- bob, pay per use: RDA 4 (public issue, branch ``feat/x``), unassigned 2;
- the manager sees every entry of RDA (and seat cost), alice and bob only
  their own (no seat cost), carol every entry but the private issue.
"""

from __future__ import annotations

import csv
import html as html_lib
import io
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.web.pages.ai_spend_reports import DRILL_MAX_ROWS, SESSIONS
from tests.services.test_ai_spend_reports import World, build_world, entry

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MARCH = {"from_date": "2026-03-01", "to_date": "2026-03-31"}
ADMIN = "/admin/ai-spend/"
PROJECT = "/projects/RDA/ai-usage/report/"


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "rd")
    db = db_session
    sub = world.subs["alice"]
    await entry(
        db,
        world.alice,
        project=world.p,
        day=date(2026, 3, 5),
        api="30",
        cost_type="subscription",
        subscription=sub,
        issue=world.public_issue,
        branch="feat/a",
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


async def _page(client: AsyncClient, user: User, url: str, **params: str) -> str:
    cookies = {"access_token": _make_access_token(user, get_settings())}
    resp = await client.get(url, params=params, cookies=cookies, follow_redirects=False)
    assert resp.status_code == 200, resp.text[:500]
    return resp.text


def _links(html: str, testid: str) -> list[dict[str, str]]:
    """The query of every link with *testid*, one value per parameter."""
    found = re.findall(rf'<a [^>]*href="([^"]+)"[^>]*data-testid="{testid}"', html)
    found += re.findall(rf'<a [^>]*data-testid="{testid}"[^>]*href="([^"]+)"', html)
    return [{k: v[0] for k, v in parse_qs(urlsplit(html_lib.unescape(u)).query).items()} for u in found]


def _section(html: str, testid: str) -> str:
    """The markup from the element with *testid* to the end of its table or section."""
    start = html.index(f'data-testid="{testid}"')
    ends = [i for i in (html.find("</table>", start), html.find("</section>", start)) if i != -1]
    return html[start : min(ends) if ends else len(html)]


# ---------------------------------------------------------------------------
# The URL state
# ---------------------------------------------------------------------------


async def test_admin_page_starts_with_projects_and_drills_down(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH)
    assert 'data-testid="spend-drill"' in html and 'data-testid="spend-crumbs"' not in html
    tabs = re.findall(r'class="sp-drill-tab active"[^>]*data-group="(\w+)"', html)
    assert tabs == ["project"]
    opened = {q["project"]: q for q in _links(html, "spend-open")}
    assert set(opened) == {"RDA", "RDB", "-"}
    assert opened["RDA"]["group"] == "user" and opened["RDA"]["from_date"] == "2026-03-01"
    assert 'data-testid="spend-people-count">2<' in html  # alice and bob; dave logged nothing

    # Project level: people of RDA; the project tab is disabled, the path leads back.
    html = await _page(client, w.admin, ADMIN, **MARCH, project="RDA", group="user")
    assert re.search(r'sp-drill-tab is-disabled"[^>]*data-group="project"', html)
    assert 'aria-current="page">RDA<' in html
    crumbs = _links(html, "spend-crumb")
    assert crumbs[0]["group"] == "project" and "project" not in crumbs[0]
    opened = {q["user_id"]: q for q in _links(html, "spend-open")}
    assert set(opened) == {str(w.alice.id), str(w.bob.id)}
    assert opened[str(w.alice.id)]["group"] == "issue" and opened[str(w.alice.id)]["project"] == "RDA"
    assert "Rep Alice" in html and "lab-model-2" not in html

    # Person level: alice's issues in RDA; then the issue's sessions.
    html = await _page(client, w.admin, ADMIN, **MARCH, project="RDA", user_id=str(w.alice.id), group="issue")
    crumbs = _links(html, "spend-crumb")
    assert [c.get("user_id") for c in crumbs] == [None, None]  # root, then RDA without alice
    assert crumbs[1]["group"] == "user" and crumbs[1]["project"] == "RDA"
    issue_rows = _links(html, "spend-open")
    assert [(q["issue"], q["group"]) for q in issue_rows] == [("RDA-1", SESSIONS)]
    assert "Public work" in html  # the issue's subject under its key

    html = await _page(client, w.admin, ADMIN, **issue_rows[0])
    assert 'data-testid="spend-sessions"' in html
    assert html.count('data-testid="spend-session"') == 1
    assert "RDA-1 · Public work" in html
    # Columns fixed by the path are left out; the entry list link keeps the path.
    sessions = _section(html, "spend-sessions")
    assert '<th scope="col">Person</th>' not in sessions and '<th scope="col">Issue</th>' not in sessions
    entry_list = _links(html, "spend-entry-list")[0]
    assert entry_list["project"] == "RDA" and entry_list["issue"] == "RDA-1"
    assert entry_list["user_id"] == str(w.alice.id)


async def test_tool_rows_lead_to_people(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH, group="tool")
    opened = _links(html, "spend-open")
    assert [(q["tool"], q["group"]) for q in opened] == [("claude-code", "user")]
    assert "2 people" in html


async def test_period_rows_open_their_dates(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH, group="project", period="week")
    opened = [q for q in _links(html, "spend-open") if q["project"] == "RDA"]
    assert {(q["from_date"], q["to_date"]) for q in opened} == {("2026-03-02", "2026-03-08")}
    assert all(q["preset"] == "custom" and "period" not in q for q in opened)


async def test_unknown_project_or_hidden_issue_matches_nothing(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH, project="NOPE", group="user")
    assert 'data-testid="spend-empty"' in html
    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 9), api="3", issue=w.private_issue)
    await db_session.commit()
    private_key = f"{w.p.key}-2"
    html = await _page(client, w.mgr, PROJECT, **MARCH, issue=private_key, group="user")
    assert re.search(r'data-testid="spend-entries">1<', html)
    # carol may not see the private issue: its key is no filter she can use.
    html = await _page(client, w.carol, PROJECT, **MARCH, issue=private_key, group="user")
    assert 'data-testid="spend-empty"' in html and "Private work" not in html


# ---------------------------------------------------------------------------
# Project scope and visibility
# ---------------------------------------------------------------------------


async def test_project_report_ignores_other_projects(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, PROJECT, **MARCH)
    assert re.findall(r'class="sp-drill-tab active"[^>]*data-group="(\w+)"', html) == ["user"]
    assert 'data-group="project"' not in _section(html, "spend-tabs")[:1500]
    total = re.search(r'data-testid="spend-entries">(\d+)<', html)
    assert total and total.group(1) == "2"
    # A project filter is not part of a project report: the same numbers.
    other = await _page(client, w.mgr, PROJECT, **MARCH, project="RDB")
    assert re.search(r'data-testid="spend-entries">(\d+)<', other).group(1) == "2"  # type: ignore[union-attr]
    # An issue of another project matches nothing.
    issue_b = await _page(client, w.mgr, PROJECT, **MARCH, issue="RDB-1")
    assert re.search(r'data-testid="spend-entries">(\d+)<', issue_b).group(1) == "0"  # type: ignore[union-attr]
    # The sessions of the project are its own only.
    sessions = await _page(client, w.mgr, PROJECT, **MARCH, group="sessions")
    assert sessions.count('data-testid="spend-session"') == 2
    assert "lab-model-2" not in sessions


async def test_seat_share_per_level_follows_who_may_see_it(client: AsyncClient, w: World) -> None:
    alice_only = {"user_id": str(w.alice.id)}
    # The manager sees seat cost: alice's RDA entry carries 75 of her 100 USD.
    html = await _page(client, w.mgr, PROJECT, **MARCH, **alice_only, group="sessions")
    assert 'data-testid="spend-seat"' in html
    assert "<strong>$75.00</strong>" in _section(html, "spend-sessions")
    # alice reads only her own entries there: no seat cost at any level.
    for group in ("user", "issue", "sessions"):
        html = await _page(client, w.alice, PROJECT, **MARCH, group=group)
        assert 'data-testid="spend-seat-hidden"' in html, group
        assert 'data-testid="spend-seat"' not in html and "$75.00" not in html, group
        assert "Rep Bob" not in html and "Secret plan" not in html, group


async def test_sessions_are_paginated(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    for i in range(12):
        await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 10) + timedelta(days=i), api="1")
    await db_session.commit()
    first = await _page(client, w.mgr, PROJECT, **MARCH, group="sessions", user_id=str(w.bob.id), limit="10")
    assert first.count('data-testid="spend-session"') == 10
    pages = _section(first, "spend-sessions-pages")
    assert "1–10 of 13" in pages
    assert "offset=10&limit=10" in pages and "group=sessions" in pages and f"user_id={w.bob.id}" in pages
    second = await _page(
        client, w.mgr, PROJECT, **MARCH, group="sessions", user_id=str(w.bob.id), limit="10", offset="10"
    )
    assert second.count('data-testid="spend-session"') == 3
    # Newest first, and the second page does not repeat the first.
    ids_first = set(re.findall(r'data-entry-id="(\d+)"', first))
    ids_second = set(re.findall(r'data-entry-id="(\d+)"', second))
    assert ids_first.isdisjoint(ids_second)
    assert "2026-03-21" in first and "2026-03-07" in second


async def test_long_groupings_fold_into_other(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    for i in range(DRILL_MAX_ROWS + 3):
        await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 12), api=str(i + 1), branch=f"feat/b{i:02d}")
    await db_session.commit()
    html = await _page(client, w.mgr, PROJECT, **MARCH, group="branch")
    assert html.count('data-testid="spend-row"') == DRILL_MAX_ROWS - 1
    assert 'data-testid="spend-other"' in html
    assert "Other (6 groups)" in html  # 35 branches (feat/a, feat/x and 33 more): 29 shown
    # The CSV still lists every branch.
    csv_link = re.search(r'href="([^"]+)"[^>]*data-testid="spend-csv"', html)
    assert csv_link
    cookies = {"access_token": _make_access_token(w.mgr, get_settings())}
    resp = await client.get(html_lib.unescape(csv_link.group(1)), cookies=cookies)
    assert len(list(csv.DictReader(io.StringIO(resp.text)))) == DRILL_MAX_ROWS + 3 + 2 + 1


# ---------------------------------------------------------------------------
# Spend by person
# ---------------------------------------------------------------------------


async def test_person_cards_on_the_admin_page_name_plans(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH)
    people = _section(html, "spend-people")
    names = re.findall(r'class="sp-person-name">([^<]+)<', people)
    assert names[:2] == ["Rep Alice", "Rep Bob"]
    payments = re.findall(r'data-testid="spend-person-payment">([^<]+)<', people)
    assert payments[0] == "Secret plan"  # alice: her plan, administrators only
    assert "Pay-per-use API" in payments[1]
    opened = _links(people, "spend-person-open")
    assert opened[0]["user_id"] == str(w.alice.id) and opened[0]["group"] == "issue"
    # The avatar has the colour of the person in the chart on the same page.
    slot = re.search(r"sp-person-avatar sp-slot-(\d)", people)
    assert slot and slot.group(1) == "0"


async def test_person_cards_on_a_project_report(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, PROJECT, **MARCH)
    people = _section(html, "spend-people")
    payments = re.findall(r'data-testid="spend-person-payment">([^<]+)<', people)
    assert payments == ["Subscription seat", "Pay-per-use API"]
    assert "Secret plan" not in html
    # alice sees only her own card, without seat cost: the cost type, no plan or seat.
    html = await _page(client, w.alice, PROJECT, **MARCH)
    people = _section(html, "spend-people")
    assert re.findall(r'class="sp-person-name">([^<]+)<', people) == ["Rep Alice"]
    assert re.findall(r'data-testid="spend-person-payment">([^<]+)<', people) == ["Subscription"]
    assert "Rep Bob" not in html
    # One person in the path: no cards.
    html = await _page(client, w.mgr, PROJECT, **MARCH, user_id=str(w.bob.id))
    assert 'data-testid="spend-people"' not in html


async def test_person_cards_are_cached_per_reader(client: AsyncClient, w: World) -> None:
    assert get_settings().ai_usage_chart_cache_seconds > 0
    first = await _page(client, w.mgr, PROJECT, **MARCH)
    assert "Rep Bob" in _section(first, "spend-people")
    second = await _page(client, w.alice, PROJECT, **MARCH)
    assert 'class="sp-person-name">Rep Bob<' not in second
    again = await _page(client, w.mgr, PROJECT, **MARCH)
    assert _section(again, "spend-people")[:3000] == _section(first, "spend-people")[:3000]


# ---------------------------------------------------------------------------
# REST: the narrowing filters
# ---------------------------------------------------------------------------


async def test_api_takes_project_issue_and_branch(client: AsyncClient, w: World) -> None:
    cookies = {"access_token": _make_access_token(w.admin, get_settings())}

    async def get(url: str, **params: str) -> dict:  # type: ignore[type-arg]
        resp = await client.get(url, params={**MARCH, **params}, cookies=cookies)
        assert resp.status_code == 200, resp.text
        return resp.json()  # type: ignore[no-any-return]

    body = await get("/api/v1/ai-usage/report/", project="RDA", group_by="user")
    assert body["totals"]["entries"] == 2 and not body["unallocated_shown"]
    body = await get("/api/v1/ai-usage/report/", project="-", group_by="user")
    assert body["totals"]["entries"] == 1
    body = await get("/api/v1/ai-usage/report/", issue="RDA-1", group_by="user")
    assert body["totals"]["entries"] == 2
    body = await get("/api/v1/ai-usage/report/", issue="-", group_by="user")
    assert body["totals"]["entries"] == 2  # alice's RDB entry and bob's unassigned one
    body = await get("/api/v1/projects/RDA/ai-usage/report/", branch="feat/x")
    assert body["totals"]["entries"] == 1
    body = await get("/api/v1/projects/RDA/ai-usage/report/", issue="RDB-1")
    assert body["totals"]["entries"] == 0
    # Seat shares follow the entries: alice's RDA issue carries 75 USD.
    body = await get("/api/v1/projects/RDA/ai-usage/report/", issue="RDA-1", group_by="user")
    alice = next(r for r in body["rows"] if r["key"]["user"] == w.alice.id)
    assert alice["total_spend"]["total"]["amount"] == "75.00"


async def test_csv_link_keeps_the_path(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.admin, ADMIN, **MARCH, project="RDA", issue="RDA-1", group="user")
    link = re.search(r'href="/api/v1/ai-usage/report/\?([^"]+)"[^>]*data-testid="spend-csv"', html)
    assert link
    query = {k: v[0] for k, v in parse_qs(html_lib.unescape(link.group(1))).items()}
    assert query["project"] == "RDA" and query["issue"] == "RDA-1" and query["group_by"] == "user"


async def test_project_report_names_a_person_only_through_their_entries(client: AsyncClient, w: World) -> None:
    # dave is no member of RDA and logged nothing there: his id names no one.
    html = await _page(client, w.mgr, PROJECT, **MARCH, user_id=str(w.dave.id), group="issue")
    assert "Rep Dave" not in html
    assert f'aria-current="page">{w.dave.id}<' in html
    html = await _page(client, w.mgr, PROJECT, **MARCH, user_id=str(w.alice.id), group="issue")
    assert 'aria-current="page">Rep Alice<' in html
