"""The spend reports' model filter (a search over the price list and the usage in view) and each
breakdown chart's "Show" choice (how many groups it names before "Other").

The world of the service tests (``tests/services/test_ai_spend_reports.py``, prefix ``so``:
projects ``SOA`` and ``SOB``). In March 2026 Alice uses ``so-alpha`` in SOA, Bob uses
``so-beta`` in SOA and ``so-gamma`` in SOB, and the price list knows ``so-priced`` (alias
``so-priced-latest``), which nobody used. The manager reads every entry of SOA; Alice reads
only her own.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.ai_model_price import AiModelPrice
from specivo.models.user import User
from specivo.services.ai_usage_chart_service import BREAKDOWN_MAX, TOP_N
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from specivo.web.pages.ai_usage import fold_share_points
from tests.services.test_ai_spend_reports import World, build_world, entry

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MARCH = {"from_date": "2026-03-01", "to_date": "2026-03-31"}
PROJECT = "/projects/SOA/ai-usage/report/"
ADMIN = "/admin/ai-spend/"
PROJECT_MODELS = "/api/v1/projects/SOA/ai-usage/report/models/"
INSTANCE_MODELS = "/api/v1/ai-usage/report/models/"


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "so")
    db = db_session
    await entry(db, world.alice, project=world.p, day=date(2026, 3, 5), api="3", model="so-alpha")
    await entry(db, world.bob, project=world.p, day=date(2026, 3, 6), api="4", model="so-beta")
    await entry(db, world.bob, project=world.q, day=date(2026, 3, 7), api="5", model="so-gamma")
    db.add(
        AiModelPrice(
            provider="solab",
            model="so-priced",
            aliases=["so-priced-latest"],
            currency_code="USD",
            input=Decimal("1"),
            output=Decimal("10"),
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            source="admin",
        )
    )
    await db.commit()
    clear_role_cache(db)
    return world


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _models(client: AsyncClient, user: User, url: str, **params: Any) -> list[str]:
    resp = await client.get(url, params=params, headers=_bearer(user))
    assert resp.status_code == 200, resp.text[:300]
    return [m["name"] for m in resp.json() if m["name"].startswith("so-")]


async def _page(client: AsyncClient, user: User, url: str, **params: str) -> str:
    resp = await client.get(url, params=params, cookies=_cookies(user), follow_redirects=False)
    assert resp.status_code == 200, resp.text[:500]
    return resp.text


def _chart(html: str, testid: str) -> dict[str, Any]:
    match = re.search(rf'data-testid="{testid}"[^>]*?data-sp-spend-chart=\'([^\']*)\'', html, re.S)
    assert match, testid
    data: dict[str, Any] = json.loads(html_lib.unescape(match.group(1)))
    return data


# ---------------------------------------------------------------------------
# The model filter
# ---------------------------------------------------------------------------


async def test_project_model_search_offers_the_price_list_and_the_usage_in_view(client: AsyncClient, w: World) -> None:
    assert await _models(client, w.mgr, PROJECT_MODELS, q="so-") == ["so-alpha", "so-beta", "so-priced"]
    # Alice reads only her own entries: Bob's model is not suggested to her.
    assert await _models(client, w.alice, PROJECT_MODELS, q="so-") == ["so-alpha", "so-priced"]
    # An alias finds its model, and the suggestion carries the provider and the other names.
    resp = await client.get(PROJECT_MODELS, params={"q": "PRICED-LATEST"}, headers=_bearer(w.mgr))
    assert resp.json() == [
        {"id": "so-priced", "name": "so-priced", "provider": "solab", "aliases": ["so-priced-latest"]}
    ]
    # Paged: a page shorter than the limit is the last.
    first = await _models(client, w.mgr, PROJECT_MODELS, q="so-", limit=2)
    rest = await _models(client, w.mgr, PROJECT_MODELS, q="so-", limit=2, offset=2)
    assert first + rest == ["so-alpha", "so-beta", "so-priced"]


async def test_instance_model_search_is_for_administrators(client: AsyncClient, w: World) -> None:
    found = await _models(client, w.admin, INSTANCE_MODELS, q="so-")
    assert found == ["so-alpha", "so-beta", "so-gamma", "so-priced"]
    resp = await client.get(INSTANCE_MODELS, params={"q": "so-"}, headers=_bearer(w.mgr))
    assert resp.status_code == 403
    # Someone outside the project gets nothing about it.
    resp = await client.get("/api/v1/projects/SOB/ai-usage/report/models/", headers=_bearer(w.carol))
    assert resp.status_code in (403, 404)


async def test_the_model_filter_is_a_search_and_a_chosen_model_a_chip(client: AsyncClient, w: World) -> None:
    html = await _page(client, w.mgr, PROJECT, **MARCH)
    field = html[html.index('data-testid="spend-model-filter"') :][:3000]
    assert 'role="combobox"' in field and 'name="model"' in field and PROJECT_MODELS in field

    chosen = await _page(client, w.mgr, PROJECT, **MARCH, model="so-beta")
    chip = chosen[chosen.index('data-testid="spend-model-filter"') :][:3000]
    assert 'class="sp-filter-chip"' in chip and 'name="model" value="so-beta"' in chip
    clear = re.search(r'href="([^"]+)" class="sp-filter-chip-clear"', chip)
    assert clear and "model=" not in html_lib.unescape(clear.group(1))
    # Typed text works too (the field is submitted as is): the report counts Bob's entry only.
    assert "so-alpha" not in chosen[chosen.index('data-testid="spend-total"') :]


# ---------------------------------------------------------------------------
# The "Show" choice of each breakdown chart
# ---------------------------------------------------------------------------


async def _many_models(db: AsyncSession, w: World, count: int) -> None:
    for i in range(count):
        await entry(db, w.bob, project=w.p, day=date(2026, 3, 10), api=str(20 + i), model=f"so-m{i:02d}")
    await db.commit()


async def test_a_breakdown_shows_eight_groups_unless_the_reader_asks_for_more(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await _many_models(db_session, w, 12)  # 14 models in SOA with Alice's and Bob's
    html = await _page(client, w.mgr, PROJECT, **MARCH)
    points = _chart(html, "aiu-chart-models")["points"]
    assert len(points) == TOP_N + 1 and points[-1]["other"] == 14 - TOP_N
    assert [p["label"] for p in points[:2]] == ["so-m11", "so-m10"]  # largest value first

    switch = html[html.index('data-testid="aiu-chart-show-model"') :][:1500]
    assert 'data-show="8"' in switch and 'aria-current="true" data-show="8"' in switch
    more = re.search(r'href="([^"]+)" data-show="25"', switch)
    assert more and "show_model=25" in html_lib.unescape(more.group(1))

    wide = await _page(client, w.mgr, PROJECT, **MARCH, show_model="25")
    points = _chart(wide, "aiu-chart-models")["points"]
    assert len(points) == 14 and not any(p.get("other") for p in points)
    # The choice is kept in the page's other links (here the bucket switch), and the other charts keep 8.
    buckets = wide[wide.index('data-testid="aiu-chart-buckets"') :][:1500]
    assert "show_model=25" in html_lib.unescape(buckets)
    # An unknown choice is ignored rather than drawing everything.
    odd = await _page(client, w.mgr, PROJECT, **MARCH, show_model="7")
    assert len(_chart(odd, "aiu-chart-models")["points"]) == TOP_N + 1


async def test_my_ai_usage_keeps_eight_groups_and_offers_no_choice(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await _many_models(db_session, w, 12)
    html = await _page(client, w.bob, "/my/ai-usage/", **MARCH)
    assert len(_chart(html, "aiu-chart-models")["points"]) == TOP_N + 1
    assert "aiu-chart-show-model" not in html


# ---------------------------------------------------------------------------
# Folding a breakdown
# ---------------------------------------------------------------------------


def _points(n: int, other: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"label": f"g{i}", "spend": 1.0, "api": 2.0} for i in range(n)]
    if other:
        out.append({"label": "Other", "spend": 5.0, "api": 7.0, "other": other})
    return out


async def test_fold_share_points() -> None:
    assert fold_share_points(_points(8), 8) == _points(8)
    # One group left over is shown by name: an "other" of one is no help.
    assert fold_share_points(_points(9), 8) == _points(9)
    folded = fold_share_points(_points(12), 8)
    assert len(folded) == 9 and folded[-1] == {"label": "Other", "spend": 4.0, "api": 8.0, "other": 4}
    # An existing "other" (past BREAKDOWN_MAX) is added in, with its group count.
    folded = fold_share_points(_points(30, other=70), 25)
    assert folded[-1] == {"label": "Other", "spend": 10.0, "api": 17.0, "other": 75}
    assert fold_share_points(_points(BREAKDOWN_MAX, other=3), BREAKDOWN_MAX) == _points(BREAKDOWN_MAX, other=3)
