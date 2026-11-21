"""REST: search with ``expand=1``, and the project's ``graph_metadata_keys`` in the settings API."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.user import TEST_PASSWORD
from tests.integration.link_graph_helpers import World, build_world, grant, issue, page, rebuild, wiki_ref

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "GRA")


async def _login(client: AsyncClient, login: str) -> AsyncClient:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client


async def test_search_expand_adds_filtered_links_to_the_top_hits(db_session, client, world):
    secret = await issue(db_session, world, world.hidden, "Wombat secret")
    await page(db_session, world, world.home, "Wombat Burrows", "Where wombats live")
    root = await issue(
        db_session, world, world.home, "Wombat survey", f"See [[Wombat Burrows]] and {secret.display_key}"
    )
    await rebuild(db_session, root)
    await _login(client, world.reader.login)

    plain = await client.get("/api/v1/search/", params={"q": "wombat"})
    assert plain.status_code == 200, plain.text
    assert all(item["links"] is None for item in plain.json()["items"])

    resp = await client.get("/api/v1/search/", params={"q": "wombat", "expand": 1})
    assert resp.status_code == 200, resp.text
    items = {item["title"]: item for item in resp.json()["items"]}
    issue_hit = next(item for item in items.values() if item["result_type"] == "issue")
    graph = issue_hit["links"]
    assert graph["root"]["ref"] == root.display_key
    assert [n["ref"] for n in graph["nodes"]] == [wiki_ref(world.home, "wombat-burrows")]
    assert graph["edges"][0]["kind"] == "wiki_link"
    assert "secret" not in resp.text.lower()

    bad = await client.get("/api/v1/search/", params={"q": "wombat", "expand": 2})
    assert bad.status_code == 422


async def test_graph_metadata_keys_are_read_and_written_through_the_project_api(db_session, client, world):
    await grant(db_session, world.reader, world.hidden, ["view_issues", "manage_project"])
    await _login(client, world.reader.login)
    url = f"/api/v1/projects/{world.hidden.key}/"

    shown = await client.get(url)
    assert shown.status_code == 200, shown.text
    assert shown.json()["graph_metadata_keys"] == ["component"]

    saved = await client.patch(url, json={"graph_metadata_keys": [" team ", "component", "team", ""]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["graph_metadata_keys"] == ["team", "component"]
    await db_session.refresh(world.hidden)
    assert world.hidden.settings["graph_metadata_keys"] == ["team", "component"]

    cleared = await client.patch(url, json={"graph_metadata_keys": []})
    assert cleared.json()["graph_metadata_keys"] == []

    for payload in (["a/b"], [f"k{i}" for i in range(11)], ["x" * 65]):
        refused = await client.patch(url, json={"graph_metadata_keys": payload})
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["field"] == "graph_metadata_keys"


async def test_graph_metadata_keys_need_manage_project(db_session, client, world):
    await _login(client, world.reader.login)
    refused = await client.patch(f"/api/v1/projects/{world.home.key}/", json={"graph_metadata_keys": ["team"]})
    assert refused.status_code in (403, 404)


async def test_project_settings_page_shows_the_graph_metadata_keys_field(admin_client: AsyncClient, db_session, world):
    world.home.settings = {"graph_metadata_keys": ["component", "team"]}
    await db_session.commit()
    resp = await admin_client.get(
        f"/projects/{world.home.key}/settings/", cookies={"access_token": admin_client.state.token}
    )
    assert resp.status_code == 200
    assert 'id="project-graph-metadata-keys"' in resp.text
    assert 'graphMetadataKeys: ["component", "team"]' in resp.text
    assert "Metadata keys in the link graph" in resp.text
