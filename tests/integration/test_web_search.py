"""Web search page integration tests.

Verifies search page renders with query input, mode toggle,
and proper auth checks.
"""

from __future__ import annotations

import html
import re

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Tests: search page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_page(auth_client: AsyncClient):
    """GET /search with auth returns 200 and contains search UI."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/search/?q=test",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Search" in resp.text


@pytest.mark.integration
async def test_search_page_empty_query(auth_client: AsyncClient):
    """GET /search with empty query returns 200."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/search/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Search" in resp.text


@pytest.mark.integration
async def test_search_requires_auth(unauth_client: AsyncClient):
    """GET /search without auth redirects to /login."""
    resp = await unauth_client.get("/search/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_search_page_has_tag_filter(auth_client: AsyncClient):
    """The search page renders the tag filter field wired to the global endpoint."""
    token = auth_client.state.token
    resp = await auth_client.get("/search/", cookies={"access_token": token})
    assert resp.status_code == 200
    assert "tagFilter(" in resp.text
    assert "/api/v1/tags/search/" in resp.text


@pytest.mark.integration
async def test_search_page_seeds_selected_tags(auth_client: AsyncClient):
    """A `tag=` param seeds the filter component with the selected chip."""
    token = auth_client.state.token
    resp = await auth_client.get("/search/?tag=pickup", cookies={"access_token": token})
    assert resp.status_code == 200
    assert "pickup" in resp.text


# ---------------------------------------------------------------------------
# Tests: pagination keeps the query intact
# ---------------------------------------------------------------------------

AWKWARD_QUERY = "R&D #42 a+b c%20d ข้อมูล"


def _page_links(page: str) -> dict[str, str]:
    """``{link text: unescaped href}`` for the pagination links."""
    links = re.findall(r'<a href="([^"]*)" class="sp-page-link">([^<]*)</a>', page)
    return {html.unescape(text).strip(): html.unescape(href) for href, text in links}


@pytest.mark.integration
async def test_search_pagination_keeps_a_query_with_reserved_characters(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Following "next" sends exactly the same query, mode and scope, one page further on."""
    from urllib.parse import parse_qs, urlsplit

    from specivo.schemas.search import SearchResult
    from specivo.web.pages import search as search_page

    calls: list[dict] = []

    async def fake_search(db, q, **kwargs):
        calls.append({"q": q, **kwargs})
        hit = SearchResult(result_type="issue", id=1, title="Hit", score=1.0, project_key="ACME")
        return [hit] * kwargs["limit"], 7, {"issue": 7}

    monkeypatch.setattr(search_page._search_svc, "search", fake_search)
    cookies = {"access_token": auth_client.state.token}

    first = await auth_client.get(
        "/search/", params={"q": AWKWARD_QUERY, "mode": "keyword", "scope": "issues", "limit": 2}, cookies=cookies
    )
    assert first.status_code == 200
    assert calls[-1]["q"] == AWKWARD_QUERY

    next_href = _page_links(first.text)["»"]
    split = urlsplit(next_href)
    assert split.path == "/search/"
    assert not split.fragment
    assert parse_qs(split.query) == {
        "q": [AWKWARD_QUERY],
        "mode": ["keyword"],
        "scope": ["issues"],
        "offset": ["2"],
        "limit": ["2"],
    }

    second = await auth_client.get(next_href, cookies=cookies)
    assert second.status_code == 200
    assert calls[-1]["q"] == AWKWARD_QUERY
    assert (calls[-1]["scope"], calls[-1]["offset"], calls[-1]["limit"]) == ("issues", 2, 2)

    # The page-size select carries the same base URL.
    match = re.search(r'data-search-limit="([^"]*)"', second.text)
    assert match is not None
    assert parse_qs(urlsplit(html.unescape(match.group(1))).query)["q"] == [AWKWARD_QUERY]
