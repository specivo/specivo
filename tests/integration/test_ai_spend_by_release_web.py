"""AI spend per release and sprint over REST, CSV, MCP and on the web: report, drill-down, version and sprint pages.

The world of ``tests/services/test_ai_spend_by_release.py`` with its own
prefix: projects ``RXA`` (the reports' project), ``RXB`` (a shared release
mgr can open, carol cannot) and ``RXH`` (a shared release nobody but the
admin can open). Every entry is on 2026-03-10.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import ValidationError
from specivo.mcp.ai_usage_tools import _ai_usage_report
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.factories.time_entry import TimeEntryActivityFactory
from tests.services.test_ai_spend_by_release import DAY, Release, build_release

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

ON_DAY = {"from_date": DAY.isoformat(), "to_date": DAY.isoformat(), "currency": "USD"}
REPORT = "/api/v1/projects/RXA/ai-usage/report/"


@pytest_asyncio.fixture
async def rx(db_session: AsyncSession) -> Release:
    return await build_release(db_session, "rx")


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _get(client: AsyncClient, user: User, url: str, **params: Any) -> Response:
    # Without params, keep the query the URL carries (httpx replaces it with any params given).
    return await client.get(url, params=params or None, cookies=_cookies(user), follow_redirects=False)


def _api_total(body: dict[str, Any]) -> Decimal:
    return Decimal(body["totals"]["api_equivalent_value"]["total"]["amount"])


def _api_by_label(body: dict[str, Any], dim: str) -> dict[str, Decimal]:
    return {
        r["labels"][dim]: Decimal(r["api_equivalent_value"]["total"]["amount"])
        for r in body["rows"]
        if not r["seat_only"]
    }


def _hrefs(html: str) -> list[str]:
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]+)"', html)]


# ---------------------------------------------------------------------------
# REST and CSV
# ---------------------------------------------------------------------------


async def test_rest_groups_by_release_and_sprint(client: AsyncClient, rx: Release) -> None:
    resp = await _get(client, rx.users["mgr"], REPORT, **ON_DAY, group_by="version")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["group_by"] == ["version"]
    assert _api_by_label(body, "version") == {
        "1.0": Decimal(1 + 2 + 8 + 32 + 1024 + 2048),
        "1.1": Decimal(4 + 4096),
        "1.2": Decimal(256),
        "RXB 2.0": Decimal(64),
        "": Decimal(16 + 128 + 512),
    }
    keys = {r["labels"]["version"]: r["key"]["version"] for r in body["rows"]}
    assert keys["RXB 2.0"] == rx.versions["2.0"].id and keys[""] is None

    resp = await _get(client, rx.users["mgr"], REPORT, **ON_DAY, group_by="sprint,version")
    body = resp.json()
    assert body["group_by"] == ["version", "sprint"]
    pairs = {(r["labels"]["version"], r["labels"]["sprint"]) for r in body["rows"]}
    assert ("1.0", "Sprint 2") in pairs and ("1.1", "Sprint 1") in pairs


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"version": "-"}, 16 + 128 + 512),
        ({"version": "1.1"}, 4 + 4096),
        ({"version": "2.0"}, 64),  # a version shared with the project, by name
        ({"sprint": "Sprint 1"}, 1 + 2 + 4 + 2048 + 4096),
        ({"sprint": "-"}, 16 + 32 + 1024 + 64 + 128 + 256 + 512),
        ({"version": "1.0", "sprint": "-"}, 32 + 1024),
        ({"version": "9.9"}, 0),  # unknown: matches nothing rather than being ignored
    ],
)
async def test_rest_release_and_sprint_filters(
    client: AsyncClient, rx: Release, params: dict[str, str], expected: int
) -> None:
    resp = await _get(client, rx.users["mgr"], REPORT, **ON_DAY, group_by="", **params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (_api_total(body) if body["totals"]["entries"] else Decimal(0)) == Decimal(expected)


async def test_rest_filter_by_id_respects_visibility(client: AsyncClient, rx: Release) -> None:
    for user, version, entries in (
        ("mgr", "2.0", 1),
        ("carol", "2.0", 0),  # carol cannot open RXB
        ("mgr", "9.0", 0),  # nobody but the admin can open RXH
        ("carol", "1.2", 0),  # set only on a private parent carol cannot see
    ):
        version_id = str(rx.versions[version].id)
        resp = await _get(client, rx.users[user], REPORT, **ON_DAY, group_by="", version=version_id)
        assert resp.json()["totals"]["entries"] == entries, (user, version)
    resp = await _get(client, rx.users["carol"], REPORT, **ON_DAY, group_by="version")
    assert set(_api_by_label(resp.json(), "version")) == {"1.0", "1.1", ""}
    assert "RXB" not in resp.text


async def test_instance_csv_labels_releases_with_their_project(client: AsyncClient, rx: Release) -> None:
    resp = await _get(client, rx.users["admin"], "/api/v1/ai-usage/report/", **ON_DAY, group_by="version", format="csv")
    assert resp.status_code == 200, resp.text
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert {r["version"] for r in rows[:-1]} >= {"RXA 1.0", "RXA 1.1", "RXA 1.2", "RXB 2.0", "RXH 9.0", ""}
    resp = await _get(
        client, rx.users["admin"], "/api/v1/ai-usage/report/", **ON_DAY, group_by="", version=str(rx.versions["9.0"].id)
    )
    assert _api_total(resp.json()) == Decimal(128)


# ---------------------------------------------------------------------------
# The report page and its drill-down
# ---------------------------------------------------------------------------


async def test_report_page_groups_by_release_and_opens_a_release(client: AsyncClient, rx: Release) -> None:
    mgr = rx.users["mgr"]
    resp = await _get(client, mgr, "/projects/RXA/ai-usage/report/", **ON_DAY, group="version")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "No release" in html and "RXB 2.0" in html and "9.0" not in html
    assert 'data-group="version"' in html and 'data-group="sprint"' in html
    v10 = rx.versions["1.0"].id
    opens = [h for h in _hrefs(html) if h.startswith("/projects/RXA/ai-usage/report/?") and f"version={v10}" in h]
    assert opens
    query = parse_qs(urlsplit(opens[0]).query)
    assert query["group"] == ["user"] and query["version"] == [str(v10)]

    level = await _get(client, mgr, "/projects/RXA/ai-usage/report/", **ON_DAY, group="user", version=str(v10))
    assert level.status_code == 200
    crumbs = level.text.split('data-testid="spend-crumbs"', 1)[1].split("</nav>", 1)[0]
    assert "Release" in crumbs and "1.0" in crumbs
    assert 'data-testid="spend-entries">6<' in level.text
    # The CSV link keeps the release.
    assert f"version={v10}" in level.text.split('data-testid="spend-csv"', 1)[0].rsplit("<a ", 1)[1]


async def test_none_rows_open_the_none_filter(client: AsyncClient, rx: Release) -> None:
    resp = await _get(client, rx.users["mgr"], "/projects/RXA/ai-usage/report/", **ON_DAY, group="sprint")
    opens = [h for h in _hrefs(resp.text) if h.startswith("/projects/RXA/ai-usage/report/?") and "sprint=-" in h]
    assert opens
    level = await _get(client, rx.users["mgr"], opens[0])
    assert "No sprint" in level.text
    assert 'data-testid="spend-entries">7<' in level.text


async def test_instance_root_offers_no_release_tab(client: AsyncClient, rx: Release) -> None:
    admin = rx.users["admin"]
    root = await _get(client, admin, "/admin/ai-spend/", **ON_DAY)
    assert 'data-group="version"' not in root.text.split('data-testid="spend-tabs"', 1)[1].split("</nav>", 1)[0]
    narrowed = await _get(client, admin, "/admin/ai-spend/", **ON_DAY, project="RXA", group="user")
    assert 'data-group="version"' in narrowed.text.split('data-testid="spend-tabs"', 1)[1].split("</nav>", 1)[0]
    # The "Group by" choice offers both at every level.
    assert '<option value="version"' in root.text and '<option value="sprint"' in root.text


async def test_entries_behind_a_release(client: AsyncClient, rx: Release) -> None:
    mgr = rx.users["mgr"]
    v11 = rx.versions["1.1"].id
    resp = await _get(client, mgr, "/projects/RXA/ai-usage/", version=str(v11), status="counted")
    assert resp.status_code == 200, resp.text
    chip = resp.text.split('data-testid="aiu-version-filter"', 1)[1].split("</span></span>", 1)[0]
    assert "1.1" in chip
    assert resp.text.count('data-entry-id="') == 2
    sessions = await _get(
        client, mgr, "/projects/RXA/ai-usage/report/", **ON_DAY, group="sessions", sprint=str(rx.sprints["s2"].id)
    )
    assert 'data-testid="spend-sessions"' in sessions.text
    assert sessions.text.count('data-entry-id="') in (0, 1)
    assert 'data-testid="spend-entries">1<' in sessions.text
    admin = await _get(client, rx.users["admin"], "/admin/ai-spend/entries/", project="RXA", version="-")
    assert admin.status_code == 200
    assert "No release" in admin.text


# ---------------------------------------------------------------------------
# Version and sprint pages
# ---------------------------------------------------------------------------


async def test_version_page_ai_spend_card(client: AsyncClient, rx: Release) -> None:
    v10 = rx.versions["1.0"].id
    resp = await _get(client, rx.users["mgr"], f"/projects/RXA/versions/{v10}/")
    assert resp.status_code == 200, resp.text
    card = resp.text.split('data-testid="ai-spend-card"', 1)[1].split('data-testid="ai-spend-card-link"', 1)[0]
    assert "3,115.00" in card
    assert 'data-testid="ai-spend-card-people">2<' in card
    link = next(h for h in _hrefs(resp.text) if h.startswith("/projects/RXA/ai-usage/report/?"))
    query = parse_qs(urlsplit(link).query)
    assert query["version"] == [str(v10)] and query["preset"] == ["custom"]
    assert query["from_date"] == [DAY.isoformat()] and query["to_date"] == [DAY.isoformat()]

    # Carol cannot see the private parent that carries 1.2: nothing to show, nothing revealed.
    hidden = await _get(client, rx.users["carol"], f"/projects/RXA/versions/{rx.versions['1.2'].id}/")
    assert hidden.status_code == 200
    assert 'data-testid="ai-spend-card-empty"' in hidden.text


async def test_sprint_page_time_and_ai_spend(client: AsyncClient, db_session: AsyncSession, rx: Release) -> None:
    db = db_session
    i = rx.issues
    for key, hours in (("E", "1"), ("A", "2"), ("A1", "4"), ("G", "8"), ("B", "16")):
        i[key].estimated_hours = Decimal(hours)
    activity = TimeEntryActivityFactory.build(name="rx Development")
    db.add(activity)
    await db.flush()
    for key, hours in (("A", "1.5"), ("K", "2"), ("B", "4")):
        db.add(
            TimeEntry(
                project_id=rx.p.id,
                issue_id=i[key].id,
                user_id=rx.users["alice"].id,
                activity_id=activity.id,
                hours=Decimal(hours),
                spent_on=DAY,
            )
        )
    await db.commit()
    resp = await _get(client, rx.users["mgr"], f"/projects/RXA/sprints/{rx.sprints['s1'].id}/")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="sprint-time-estimated">15.0h<' in html
    assert 'data-testid="sprint-time-logged">3.5h<' in html
    card = html.split('data-testid="ai-spend-card"', 1)[1].split('data-testid="ai-spend-card-link"', 1)[0]
    assert "6,151.00" in card

    empty = await _get(client, rx.users["mgr"], f"/projects/RXA/sprints/{rx.sprints['s2'].id}/")
    assert 'data-testid="sprint-time-estimated">16.0h<' in empty.text


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def _mcp(db: AsyncSession, user: User, **kw: Any) -> str:
    args: dict[str, Any] = {
        "project_key": "RXA",
        "date_from": DAY.isoformat(),
        "date_to": DAY.isoformat(),
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


async def test_mcp_release_and_sprint(db_session: AsyncSession, rx: Release) -> None:
    mgr = rx.users["mgr"]
    text = await _mcp(db_session, mgr, release="1.1", group_by="")
    assert "project RXA (release 1.1)" in text.splitlines()[0]
    assert "api_equivalent=4100.00 USD" in text.splitlines()[-1]
    text = await _mcp(db_session, mgr, release="none", sprint="Sprint 1", group_by="")
    assert "(no release, sprint Sprint 1)" in text.splitlines()[0]
    assert text.splitlines()[-1].startswith("Total: entries=0")
    text = await _mcp(db_session, mgr, group_by="version")
    assert "version=RXB 2.0:" in text and "version=(no release):" in text
    text = await _mcp(db_session, mgr, group_by="sprint", release=str(rx.versions["1.0"].id))
    assert "sprint=Sprint 2:" in text and "sprint=(no sprint):" in text
    for kw in ({"release": "9.0"}, {"release": str(rx.versions["9.0"].id)}, {"sprint": "Sprint 9"}):
        with pytest.raises(ValidationError) as caught:
            await _mcp(db_session, mgr, **kw)
        assert caught.value.field in ("release", "sprint")
    # Carol cannot see RXB's release: the same refusal as for one that does not exist.
    with pytest.raises(ValidationError):
        await _mcp(db_session, rx.users["carol"], release="2.0")
