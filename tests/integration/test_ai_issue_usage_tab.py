"""The AI usage tab of an issue (``specivo/web/pages/ai_issue_usage.py``): what the feature cost.

The world of the spend report service tests with its own prefix (projects
``ITA`` and ``ITB``; ``ITA-1`` public, ``ITA-2`` private), plus a sub-issue
``ITA-3`` under ``ITA-1`` and ``ITA-4`` under ``ITA-3``. March 2026:

- alice, subscription (100 USD a month): 30 on ``ITA-1`` and 10 in ``ITB``, so
  ``ITA-1`` carries 75 USD of her fee;
- bob, pay per use: 4 on ``ITA-1`` (branch ``feat/x``);
- the manager sees every entry (and seat cost), alice and bob only their own,
  carol every entry but the private issue.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from tests.factories.issue import IssueFactory
from tests.services.test_ai_spend_reports import World, build_world, entry

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

TAB = "/partials/issues/{key}/ai-usage/"
PAGE = "/issue/{key}/ai-usage/"


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "it")
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
        db,
        world.bob,
        project=world.p,
        day=date(2026, 3, 7),
        api="4",
        issue=world.public_issue,
        branch="feat/x",
        tool="codex",
        model="lab-model-2",
    )
    await db.commit()
    return world


async def _child(db: AsyncSession, parent: Issue, number: int, subject: str) -> Issue:
    child = IssueFactory.build(
        project_id=parent.project_id,
        project_key=parent.project_key,
        tracker_id=parent.tracker_id,
        status_id=parent.status_id,
        priority_id=parent.priority_id,
        author_id=parent.author_id,
        sequence_number=number,
        subject=subject,
        parent_id=parent.id,
    )
    db.add(child)
    await db.flush()
    return child


async def _get(client: AsyncClient, user: User, url: str, **params: str) -> tuple[int, str]:
    cookies = {"access_token": _make_access_token(user, get_settings())}
    resp = await client.get(url, params=params, cookies=cookies, follow_redirects=False)
    return resp.status_code, resp.text


async def _tab(client: AsyncClient, user: User, key: str = "ITA-1", **params: str) -> str:
    status, html = await _get(client, user, TAB.format(key=key), **params)
    assert status == 200, html[:500]
    return html


def _testid(html: str, testid: str) -> str:
    """The text of the element with *testid* (tags stripped, whitespace collapsed)."""
    match = re.search(rf'data-testid="{testid}"[^>]*>(.*?)</(span|p|div|a)>', html, re.S)
    assert match, testid
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", "", match.group(1))).split())


def _links(html: str, testid: str) -> list[dict[str, str]]:
    """``href`` and ``hx-get`` of every link with *testid*."""
    out = []
    for tag in re.findall(rf'<a [^>]*data-testid="{testid}"[^>]*>', html):
        href = re.search(r'href="([^"]+)"', tag)
        hx = re.search(r'hx-get="([^"]+)"', tag)
        out.append(
            {
                "href": html_lib.unescape(href.group(1)) if href else "",
                "hx": html_lib.unescape(hx.group(1)) if hx else "",
            }
        )
    return out


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ---------------------------------------------------------------------------
# Headline numbers, breakdowns and permissions
# ---------------------------------------------------------------------------


async def test_manager_sees_the_feature_cost_with_seat_cost(client: AsyncClient, w: World) -> None:
    html = await _tab(client, w.mgr)
    # 75 USD of alice's fee plus bob's 4 USD of pay-per-use; value 30 + 4.
    assert _testid(html, "aiu-issue-spend") == "$79.00"
    assert _testid(html, "aiu-issue-api") == "$34.00"
    assert _testid(html, "aiu-issue-people-count") == "2"
    assert _testid(html, "aiu-issue-days") == "Mar 5, 2026 to Mar 7, 2026"
    assert "Pay-per-use plus seat cost" in html and 'data-testid="aiu-issue-seat-hidden"' not in html
    # The people, largest spend first; tools and models as bars.
    people = re.findall(r'data-testid="aiu-issue-person".*?class="sp-person-name">([^<]+)<', html, re.S)
    assert people == ["Rep Alice", "Rep Bob"]
    assert html.count('data-testid="aiu-issue-bar"') == 4  # two tools, two models
    assert "lab-model-2" in html and "feat/x" in html
    # Two sessions, the spend report and the project list one click away.
    assert html.count('data-testid="aiu-issue-session"') == 2
    assert _query(_links(html, "aiu-issue-list")[0]["href"]) == {"issue": "ITA-1"}
    report = _query(_links(html, "aiu-issue-report")[0]["href"])
    assert report["issue"] == "ITA-1" and report["from_date"] == "2026-03-05" and report["to_date"] == "2026-03-07"
    # Three days: no chart, so the chart bundle is not asked for.
    assert 'data-testid="aiu-issue-chart"' not in html and "data-sp-chart-src" not in html


async def test_developer_sees_only_own_entries_and_no_seat_cost(client: AsyncClient, w: World) -> None:
    html = await _tab(client, w.alice)
    assert _testid(html, "aiu-issue-spend") == "$0.00"  # her subscription usage costs nothing on top
    assert _testid(html, "aiu-issue-api") == "$30.00"
    assert _testid(html, "aiu-issue-people-count") == "1"
    assert 'data-testid="aiu-issue-seat-hidden"' in html
    assert "Rep Bob" not in html and "feat/x" not in html
    # Nothing has spend in view: the bars measure the API-equivalent value instead.
    assert "API-equivalent value by person" in html
    html = await _tab(client, w.bob)
    assert _testid(html, "aiu-issue-spend") == "$4.00" and "Rep Alice" not in html


async def test_issues_the_reader_cannot_see_are_404(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await entry(db_session, w.mgr, project=w.p, day=date(2026, 3, 9), api="7", issue=w.private_issue)
    await db_session.commit()
    assert (await _get(client, w.carol, TAB.format(key="ITA-2")))[0] == 404
    assert (await _get(client, w.dave, TAB.format(key="ITA-1")))[0] in (403, 404)
    html = await _tab(client, w.mgr, "ITA-2")
    assert _testid(html, "aiu-issue-api") == "$7.00"


async def test_empty_issue_explains_attribution(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    lonely = await _child(db_session, w.private_issue, 9, "Nothing yet")
    await db_session.commit()
    html = await _tab(client, w.mgr, lonely.display_key)
    assert 'data-testid="aiu-empty"' in html and "SPECIVO_ISSUE" in html
    guide = re.search(r'href="([^"]+)"[^>]*data-testid="aiu-issue-guide"', html)
    assert guide and guide.group(1).startswith("https://specivo.io/docs/specivo/ai-agents/reporting-ai-usage/")
    assert 'data-testid="aiu-issue-sessions"' not in html and 'data-testid="aiu-issue-scope"' not in html


async def test_excluded_entries_are_counted_apart(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 8), api="50", issue=w.public_issue, excluded=True)
    await db_session.commit()
    html = await _tab(client, w.mgr)
    assert _testid(html, "aiu-issue-api") == "$34.00"
    assert _testid(html, "aiu-issue-excluded") == "1 excluded entry not counted"
    assert _query(_links(html, "aiu-issue-excluded")[0]["href"]) == {"issue": "ITA-1", "status": "excluded"}


# ---------------------------------------------------------------------------
# Sub-issues
# ---------------------------------------------------------------------------


async def test_sub_issues_count_by_default_and_can_be_left_out(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    html = await _tab(client, w.mgr)
    assert 'data-testid="aiu-issue-scope"' not in html  # no sub-issues, no switch

    child = await _child(db_session, w.public_issue, 3, "Child work")
    grandchild = await _child(db_session, child, 4, "Grandchild work")
    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 10), api="6", issue=child)
    await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 11), api="5", issue=grandchild)
    await db_session.commit()

    html = await _tab(client, w.mgr)
    assert 'data-testid="aiu-issue-scope"' in html
    assert 'aria-current="true" data-testid="aiu-issue-sub-on"' in html
    assert _testid(html, "aiu-issue-api") == "$45.00"  # 34 + 6 + 5
    assert html.count('data-testid="aiu-issue-session"') == 4
    assert 'href="/issue/ITA-3/"' in html and 'href="/issue/ITA-4/"' in html  # the issue column
    # Five days: the time chart, with the bundle to load it.
    assert 'data-testid="aiu-issue-chart"' in html and "data-sp-chart-src=" in html
    off = _links(html, "aiu-issue-sub-off")[0]
    assert off == {"href": "/issue/ITA-1/ai-usage/?sub=0", "hx": "/partials/issues/ITA-1/ai-usage/?sub=0"}

    html = await _tab(client, w.mgr, sub="0")
    assert _testid(html, "aiu-issue-api") == "$34.00"
    assert html.count('data-testid="aiu-issue-session"') == 2 and 'href="/issue/ITA-3/"' not in html
    assert 'aria-current="true" data-testid="aiu-issue-sub-off"' in html

    # A sub-issue's own tab counts its own subtree only.
    html = await _tab(client, w.mgr, "ITA-3")
    assert _testid(html, "aiu-issue-api") == "$11.00"


# ---------------------------------------------------------------------------
# Many entries: totals over all of them, the sessions a page at a time
# ---------------------------------------------------------------------------


async def test_three_hundred_entries_are_totalled_and_paginated(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    start = date(2026, 1, 1)
    for i in range(300):
        await entry(db_session, w.bob, project=w.p, day=start + timedelta(days=i % 60), api="1", issue=w.private_issue)
    await db_session.commit()

    html = await _tab(client, w.mgr, "ITA-2")
    assert _testid(html, "aiu-issue-api") == "$300.00" and _testid(html, "aiu-issue-spend") == "$300.00"
    assert "300 entries" in html
    assert html.count('data-testid="aiu-issue-session"') == 10
    assert _testid(html, "aiu-issue-range") == "1–10 of 300"
    assert 'data-testid="aiu-issue-chart"' in html  # sixty days of usage
    pages = _links(html, "aiu-issue-page")
    assert [p["href"] for p in pages[:2]] == ["/issue/ITA-2/ai-usage/?offset=10", "/issue/ITA-2/ai-usage/?offset=20"]
    assert pages[-1]["hx"] == "/partials/issues/ITA-2/ai-usage/?offset=290"
    assert 'data-testid="aiu-issue-prev"' not in html and _links(html, "aiu-issue-next")
    sizes = {_query(s["href"]).get("limit", "10") for s in _links(html, "aiu-issue-size")}
    assert sizes == {"25", "50"}

    # The last page; the totals do not change with the page.
    html = await _tab(client, w.mgr, "ITA-2", offset="290")
    assert html.count('data-testid="aiu-issue-session"') == 10 and _testid(html, "aiu-issue-range") == "291–300 of 300"
    assert _testid(html, "aiu-issue-api") == "$300.00" and 'data-testid="aiu-issue-next"' not in html

    html = await _tab(client, w.mgr, "ITA-2", limit="50", offset="100")
    assert html.count('data-testid="aiu-issue-session"') == 50 and _testid(html, "aiu-issue-range") == "101–150 of 300"
    assert _links(html, "aiu-issue-next")[0]["href"] == "/issue/ITA-2/ai-usage/?offset=150&limit=50"

    # A size that is not offered falls back to 10; an offset past the end shows the last page.
    html = await _tab(client, w.mgr, "ITA-2", limit="1000")
    assert html.count('data-testid="aiu-issue-session"') == 10
    html = await _tab(client, w.mgr, "ITA-2", offset="5000")
    assert _testid(html, "aiu-issue-range") == "291–300 of 300"


# ---------------------------------------------------------------------------
# The page of its own (no JavaScript) and the issue page
# ---------------------------------------------------------------------------


async def test_standalone_page_renders_the_tab(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    status, html = await _get(client, w.mgr, PAGE.format(key="ITA-1"))
    assert status == 200 and 'data-testid="ai-usage-page"' in html and 'data-testid="aiu-issue-tab"' in html
    assert "spend-chart" not in html  # three days: no chart, no bundle
    for i in range(4):
        await entry(db_session, w.bob, project=w.p, day=date(2026, 3, 20 + i), api="1", issue=w.public_issue)
    await db_session.commit()
    status, html = await _get(client, w.mgr, PAGE.format(key="ITA-1"))
    assert re.search(r'<script src="/static/dist/js/spend-chart[^"]*" defer>', html)
    assert (await _get(client, w.carol, PAGE.format(key="ITA-2")))[0] == 404


async def test_issue_page_loads_the_tab_lazily(client: AsyncClient, w: World) -> None:
    status, html = await _get(client, w.mgr, "/issue/ITA-1/")
    assert status == 200
    assert 'hx-get="/partials/issues/ITA-1/ai-usage/"' in html
    assert "spend-chart" not in html  # the bundle comes with the tab, only when it has a chart
