"""Suggested links in the graph view's own routes: the project graph and ``/links/?ref=``.

A ``similar`` edge (``suggested: true``, drawn dashed by the view) comes back only with
``include_suggestions=true``, and only when the reader may see both of its ends.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.services.link_suggestion_service import compute_for_project
from tests.factories.user import TEST_PASSWORD
from tests.integration.link_graph_helpers import World, build_world, issue, page, wiki_ref
from tests.integration.link_suggestion_helpers import _no_queued_tasks, default_model, embed

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]
NEAR_A = [0.95, 0.31, 0.0, 0.0]
# Close to A, and to NEAR_A as well: a private issue the reader may not see.
PRIVATE = [0.97, 0.1, 0.2, 0.0]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "LGS")


@pytest.fixture
async def suggested(db_session: AsyncSession, world: World):
    """Root issue ~ near page ~ a private issue, as open suggestions, and nothing else linked."""
    model = await default_model(db_session, "lgs-mock")
    root = await issue(db_session, world, world.home, "Suggestion root")
    near = await page(db_session, world, world.home, "Near graph page")
    hidden = await issue(db_session, world, world.home, "Private twin", private=True)
    for item, vector in ((root, A), (near, NEAR_A), (hidden, PRIVATE)):
        await embed(db_session, model, item, vector)
    await compute_for_project(db_session, world.home.id)
    await db_session.commit()
    return root, near, hidden


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _similar(data: dict) -> set[tuple[str, str]]:
    edges = [e for e in data["edges"] if e["kind"] == "similar"]
    assert all(e["suggested"] is True and e["label"] for e in edges)
    return {tuple(sorted((e["source"], e["target"]))) for e in edges}  # type: ignore[misc]


async def test_project_graph_returns_suggestions_only_when_asked(client, db_session, world, suggested):
    root, near, hidden = suggested
    headers = await _headers(client, world.reader.login)
    url = f"/api/v1/projects/{world.home.key}/links/"

    plain = await client.get(url, headers=headers)
    assert plain.status_code == 200, plain.text
    assert _similar(plain.json()) == set()
    assert all("suggested" not in e or e["suggested"] is False for e in plain.json()["edges"])

    resp = await client.get(url, params={"include_suggestions": "true"}, headers=headers)
    assert resp.status_code == 200, resp.text
    pair = tuple(sorted((root.display_key, wiki_ref(world.home, near.slug))))
    assert _similar(resp.json()) == {pair}
    assert hidden.display_key not in resp.text and "Private twin" not in resp.text

    # The admin sees the private issue, and so its suggestions.
    admin = await client.get(
        url, params={"include_suggestions": "true"}, headers=await _headers(client, world.admin.login)
    )
    assert hidden.display_key in {ref for edge in _similar(admin.json()) for ref in edge}


async def test_links_by_ref_returns_suggestions_only_when_asked(client, db_session, world, suggested):
    root, near, hidden = suggested
    headers = await _headers(client, world.reader.login)

    plain = await client.get("/api/v1/links/", params={"ref": root.display_key}, headers=headers)
    assert plain.status_code == 200, plain.text
    assert _similar(plain.json()) == set()

    resp = await client.get(
        "/api/v1/links/", params={"ref": root.display_key, "include_suggestions": "true"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert _similar(resp.json()) == {tuple(sorted((root.display_key, wiki_ref(world.home, near.slug))))}
    assert hidden.display_key not in resp.text

    # A suggestion with a hidden end is left out from the visible end too.
    resp = await client.get(
        "/api/v1/links/",
        params={"ref": f"wiki:{world.home.key}/{near.slug}", "include_suggestions": "true"},
        headers=headers,
    )
    assert _similar(resp.json()) == {tuple(sorted((root.display_key, wiki_ref(world.home, near.slug))))}
    assert hidden.display_key not in resp.text
