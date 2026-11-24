"""The link graph view: the project graph and hub-root JSON it reads, and the pages that hold the graph.

- ``GET /api/v1/projects/{key}/links/``: the most linked issues and pages of a project with their links,
  filtered for the reader; no root, no direction.
- ``GET /api/v1/links/?ref=``: any root the graph can start from, hubs included (expanding a tag node).
- The issue's Graph tab, the wiki page's Graph block and the project's Graph page.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.relation import IssueRelation
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.integration.link_graph_helpers import World, build_world, grant, issue, page, rebuild, wiki_ref

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "LGV")


async def _token(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _cookies(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"access_token": resp.json()["access_token"]}


async def _stranger(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    return user


async def _relate(db: AsyncSession, a: Issue, b: Issue, relation_type: str = "relates") -> None:
    db.add(IssueRelation(issue_from_id=a.id, issue_to_id=b.id, relation_type=relation_type))
    await db.commit()


async def _tag(db: AsyncSession, world: World, name: str, *items: Issue | WikiPage) -> Tag:
    tag = Tag(project_id=world.home.id, name=name)
    db.add(tag)
    await db.flush()
    for item in items:
        if isinstance(item, Issue):
            db.add(TagLink(tag_id=tag.id, issue_id=item.id))
        else:
            db.add(TagLink(tag_id=tag.id, wiki_page_id=item.id))
    await db.commit()
    return tag


# ---------------------------------------------------------------------------
# The project graph JSON
# ---------------------------------------------------------------------------


async def test_project_graph_lists_linked_items_for_a_member(client, db_session, world):
    spec = await page(db_session, world, world.home, "Graph Spec")
    hub = await issue(db_session, world, world.home, "Hub issue", "Implements [[Graph Spec]]")
    other = await issue(db_session, world, world.home, "Other issue")
    await _relate(db_session, hub, other, "blocks")
    secret = await issue(db_session, world, world.hidden, "Secret neighbour")
    leaky = await issue(db_session, world, world.home, "Leaky", f"Depends on {secret.display_key}")
    lonely = await issue(db_session, world, world.home, "Lonely issue")
    await rebuild(db_session, hub, leaky)

    resp = await client.get(
        f"/api/v1/projects/{world.home.key}/links/", headers=await _token(client, world.reader.login)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["root"] is None
    refs = {n["ref"] for n in data["nodes"]}
    assert {hub.display_key, other.display_key, wiki_ref(world.home, spec.slug)} <= refs
    assert lonely.display_key not in refs
    assert secret.display_key not in refs and "Secret neighbour" not in resp.text
    edges = {(e["source"], e["target"], e["kind"]) for e in data["edges"]}
    assert (hub.display_key, wiki_ref(world.home, spec.slug), "wiki_link") in edges
    assert (hub.display_key, other.display_key, "relation") in edges
    assert all(e["direction"] is None for e in data["edges"])
    # A relation read from both of its ends is listed once.
    assert sum(1 for e in data["edges"] if e["kind"] == "relation") == 1
    assert data["truncated"] is False


async def test_project_graph_kinds_and_limit(client, db_session, world):
    spec = await page(db_session, world, world.home, "Kinds Spec")
    a = await issue(db_session, world, world.home, "A", "See [[Kinds Spec]]")
    b = await issue(db_session, world, world.home, "B")
    c = await issue(db_session, world, world.home, "C")
    await _relate(db_session, a, b)
    await _relate(db_session, a, c)
    await rebuild(db_session, a)
    headers = await _token(client, world.reader.login)

    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/?kinds=relation", headers=headers)
    assert resp.status_code == 200, resp.text
    assert {e["kind"] for e in resp.json()["edges"]} == {"relation"}
    assert wiki_ref(world.home, spec.slug) not in {n["ref"] for n in resp.json()["nodes"]}

    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/?limit=2", headers=headers)
    assert len(resp.json()["nodes"]) == 2 and resp.json()["truncated"] is True

    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/?kinds=bogus", headers=headers)
    assert resp.status_code == 422

    # Tags are leaves with their degree; asking for them seeds the recently updated items too.
    await _tag(db_session, world, "graph-tag", b, c)
    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/?kinds=tagged", headers=headers)
    tag = next(n for n in resp.json()["nodes"] if n["type"] == "tag")
    assert tag["ref"] == f"tag:{world.home.key}/graph-tag" and tag["degree"] == 2


async def test_project_graph_is_not_found_outside_the_readers_projects(client, db_session, world):
    a = await issue(db_session, world, world.hidden, "Hidden A", "x")
    b = await issue(db_session, world, world.hidden, "Hidden B")
    await _relate(db_session, a, b)

    reader = await _token(client, world.reader.login)
    resp = await client.get(f"/api/v1/projects/{world.hidden.key}/links/", headers=reader)
    assert resp.status_code == 404
    assert "Hidden" not in resp.text
    resp = await client.get("/api/v1/projects/NOSUCHKEY/links/", headers=reader)
    assert resp.status_code == 404

    stranger = await _token(client, (await _stranger(db_session, "lgv_stranger")).login)
    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/", headers=stranger)
    assert resp.status_code == 404

    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/")
    assert resp.status_code == 401

    admin = await _token(client, world.admin.login)
    resp = await client.get(f"/api/v1/projects/{world.hidden.key}/links/", headers=admin)
    assert resp.status_code == 200
    assert {n["ref"] for n in resp.json()["nodes"]} == {a.display_key, b.display_key}


async def test_project_graph_needs_issues_or_wiki_permission(client, db_session, world):
    member = await _stranger(db_session, "lgv_noperm")
    await grant(db_session, member, world.home, ["add_issue_notes"])
    resp = await client.get(f"/api/v1/projects/{world.home.key}/links/", headers=await _token(client, member.login))
    assert resp.status_code == 404


async def test_project_graph_hides_private_issues_of_others(client, db_session, world):
    hidden = await issue(db_session, world, world.home, "Private plan", private=True)
    shown = await issue(db_session, world, world.home, "Public plan")
    await _relate(db_session, hidden, shown)
    resp = await client.get(
        f"/api/v1/projects/{world.home.key}/links/", headers=await _token(client, world.reader.login)
    )
    assert resp.status_code == 200
    assert "Private plan" not in resp.text and hidden.display_key not in resp.text


async def test_suggestion_flag_is_accepted_by_every_graph_route(client, db_session, world):
    a = await issue(db_session, world, world.home, "Flag A")
    b = await issue(db_session, world, world.home, "Flag B")
    await _relate(db_session, a, b)
    headers = await _token(client, world.reader.login)
    for url in (
        f"/api/v1/projects/{world.home.key}/links/?include_suggestions=true",
        f"/api/v1/issues/{a.display_key}/links/?include_suggestions=true&depth=2",
        f"/api/v1/links/?ref={a.display_key}&include_suggestions=true",
    ):
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, (url, resp.text)


# ---------------------------------------------------------------------------
# Any root, hubs included: /api/v1/links/?ref=
# ---------------------------------------------------------------------------


async def test_links_by_ref_expands_a_tag(client, db_session, world):
    a = await issue(db_session, world, world.home, "Tagged A")
    b = await issue(db_session, world, world.home, "Tagged B")
    doc = await page(db_session, world, world.home, "Tagged Page")
    await _tag(db_session, world, "release", a, b, doc)
    headers = await _token(client, world.reader.login)

    resp = await client.get(f"/api/v1/links/?ref=tag:{world.home.key}/release", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["root"]["ref"] == f"tag:{world.home.key}/release"
    assert {n["ref"] for n in data["nodes"]} == {a.display_key, b.display_key, wiki_ref(world.home, doc.slug)}
    assert {e["kind"] for e in data["edges"]} == {"tagged"}

    resp = await client.get(f"/api/v1/links/?ref={a.display_key}&kinds=tagged", headers=headers)
    assert resp.status_code == 200
    assert data["root"]["ref"] in {n["ref"] for n in resp.json()["nodes"]}


async def test_links_by_ref_refuses_what_the_reader_may_not_see(client, db_session, world):
    secret = await issue(db_session, world, world.hidden, "Hidden root")
    hidden_tag = Tag(project_id=world.hidden.id, name="secret-tag")
    db_session.add(hidden_tag)
    await db_session.commit()
    headers = await _token(client, world.reader.login)

    for ref in (secret.display_key, f"tag:{world.hidden.key}/secret-tag", f"wiki:{world.hidden.key}/home", "NOPE-1"):
        resp = await client.get("/api/v1/links/", params={"ref": ref}, headers=headers)
        assert resp.status_code == 404, (ref, resp.text)
        assert "Hidden root" not in resp.text

    resp = await client.get("/api/v1/links/", params={"ref": "wiki:nokey"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "ref"
    resp = await client.get("/api/v1/links/", params={"ref": f"tag:{world.home.key}"}, headers=headers)
    assert resp.status_code == 422
    resp = await client.get("/api/v1/links/", headers=headers)
    assert resp.status_code == 422
    resp = await client.get("/api/v1/links/", params={"ref": secret.display_key})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


async def test_issue_page_has_a_graph_tab_for_a_signed_in_reader(client, db_session, world):
    root = await issue(db_session, world, world.home, "Graph tab issue")
    resp = await client.get(f"/issue/{root.display_key}/", cookies=await _cookies(client, world.reader.login))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="graph-tab"' in html and 'data-testid="issue-graph"' in html
    assert f'data-endpoint="/api/v1/issues/{root.display_key}/links/"' in html
    assert 'data-expand-endpoint="/api/v1/links/"' in html
    assert 'data-scope="item"' in html
    # The bundle is named for the loader, not loaded with the page.
    assert 'data-sp-graph-src="/static/dist/js/link-graph.min' in html
    assert '<script src="/static/dist/js/link-graph.min' not in html
    assert "data-sp-graph-open" not in html.split('data-testid="issue-graph"')[1].split(">")[0]


async def test_wiki_page_has_a_graph_block(client, db_session, world):
    doc = await page(db_session, world, world.home, "Graph Block Page")
    resp = await client.get(
        f"/projects/{world.home.key}/wiki/{doc.slug}/", cookies=await _cookies(client, world.reader.login)
    )
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="wiki-graph-block"' in html and "data-sp-graph-block" in html
    assert f'data-endpoint="/api/v1/projects/{world.home.key}/wiki/{doc.slug}/links/"' in html
    assert '<script src="/static/dist/js/link-graph.min' not in html


async def test_project_graph_page(client, db_session, world):
    resp = await client.get(f"/projects/{world.home.key}/graph/", cookies=await _cookies(client, world.reader.login))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="project-graph"' in html and 'data-scope="project"' in html
    assert f'data-endpoint="/api/v1/projects/{world.home.key}/links/"' in html
    assert '<script src="/static/dist/js/link-graph.min' in html
    assert f'href="/projects/{world.home.key}/graph/"' in html  # the sidebar entry
    # Attachments start unchecked on the project graph; the other default kinds are checked.
    assert 'value="attachment" data-graph-kind>' in html
    assert 'value="relation" data-graph-kind checked>' in html
    assert 'value="tagged" data-graph-kind>' in html


async def test_project_graph_page_refusals(client, db_session, world):
    reader = await _cookies(client, world.reader.login)
    resp = await client.get(f"/projects/{world.hidden.key}/graph/", cookies=reader)
    assert resp.status_code == 404
    stranger = await _cookies(client, (await _stranger(db_session, "lgv_webstranger")).login)
    resp = await client.get(f"/projects/{world.home.key}/graph/", cookies=stranger)
    assert resp.status_code == 404
    resp = await client.get(f"/projects/{world.home.key}/graph/", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers["location"]


async def test_graph_labels_are_escaped_in_the_page(client, db_session, world):
    root = await issue(db_session, world, world.home, "<img src=x onerror=alert(1)>")
    resp = await client.get(f"/issue/{root.display_key}/", cookies=await _cookies(client, world.reader.login))
    assert "<img src=x onerror=alert(1)>" not in resp.text
    # In the JSON the title is data; the view only ever sets it as text.
    resp = await client.get(f"/api/v1/links/?ref={root.display_key}", headers=await _token(client, world.reader.login))
    assert resp.json()["root"]["title"] == "<img src=x onerror=alert(1)>"
