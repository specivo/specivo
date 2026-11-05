"""REST endpoints of the link graph: ``/issues/{ref}/links/``, ``/projects/{key}/wiki/{slug}/links/``
and the wiki graph served from the stored links."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.user import TEST_PASSWORD
from tests.integration.link_graph_helpers import World, build_world, comment, issue, page, rebuild, wiki_ref

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELA")


@pytest.fixture
async def reader_client(client: AsyncClient, world: World) -> AsyncClient:
    resp = await client.post("/api/v1/auth/login/", json={"login": world.reader.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client


async def test_issue_links(reader_client, db_session, world):
    secret = await issue(db_session, world, world.hidden, "Secret")
    doc = await page(db_session, world, world.home, "Overview")
    root = await issue(db_session, world, world.home, "Root", f"See [[Overview]] and {secret.display_key}")
    on = await issue(db_session, world, world.home, "Mentioner")
    note = await comment(db_session, world, on, f"Related to {root.display_key}")
    await rebuild(db_session, root, note)

    resp = await reader_client.get(f"/api/v1/issues/{root.display_key}/links/")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["root"]["ref"] == root.display_key
    assert {n["ref"] for n in data["nodes"]} == {wiki_ref(world.home, "overview"), on.display_key}
    assert "Secret" not in resp.text
    by_kind = {(e["kind"], e["direction"]): e for e in data["edges"]}
    assert by_kind[("wiki_link", "out")]["target"] == wiki_ref(world.home, "overview")
    assert by_kind[("mention", "in")]["via"] == f"{on.display_key}#{note.sequence}"
    assert data["truncated"] is False

    backlinks = await reader_client.get(f"/api/v1/issues/{root.display_key}/links/?direction=in")
    assert [e["source"] for e in backlinks.json()["edges"]] == [on.display_key]
    assert doc.id


async def test_hidden_root_is_not_found_and_bad_kinds_are_refused(reader_client, db_session, world):
    secret = await issue(db_session, world, world.hidden, "Secret root")
    resp = await reader_client.get(f"/api/v1/issues/{secret.display_key}/links/")
    assert resp.status_code == 404
    mine = await issue(db_session, world, world.home, "Mine")
    resp = await reader_client.get(f"/api/v1/issues/{mine.display_key}/links/?kinds=mention,bogus")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "kinds"
    resp = await reader_client.get(f"/api/v1/issues/{mine.display_key}/links/?depth=3")
    assert resp.status_code == 422


async def test_wiki_page_links(reader_client, db_session, world):
    target = await page(db_session, world, world.home, "Target Page")
    source = await issue(db_session, world, world.home, "Points at it", "[[Target Page]]")
    await rebuild(db_session, source)
    resp = await reader_client.get(f"/api/v1/projects/{world.home.key}/wiki/target-page/links/?direction=in")
    assert resp.status_code == 200, resp.text
    assert [(e["source"], e["kind"]) for e in resp.json()["edges"]] == [(source.display_key, "wiki_link")]

    resp = await reader_client.get(f"/api/v1/projects/{world.hidden.key}/wiki/target-page/links/")
    assert resp.status_code == 404
    assert target.id


async def test_wiki_graph_endpoint(reader_client, db_session, world):
    alpha = await page(db_session, world, world.home, "Alpha", "See [[Beta]] and [[Does Not Exist]]")
    beta = await page(db_session, world, world.home, "Beta", "Back to [[Alpha]]")
    await rebuild(db_session, alpha, beta)

    resp = await reader_client.get(f"/api/v1/projects/{world.home.key}/wiki/graph/")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert {n["slug"] for n in data["nodes"]} == {"alpha", "beta"}
    assert set(data["nodes"][0]) == {"id", "slug", "title"}
    broken = [e for e in data["edges"] if e["is_broken"]]
    assert [(e["target_slug"], e["target_page_id"]) for e in broken] == [("does-not-exist", None)]
    assert len(data["edges"]) == 3
