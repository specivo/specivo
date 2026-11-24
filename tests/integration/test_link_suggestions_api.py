"""Suggested links over REST and on the web pages.

- ``/links/`` returns ``similar`` edges only with ``include_suggestions=true``
  or ``kinds=similar``, in the shape the graph view draws (kind, score label,
  ``suggested: true``).
- ``POST /api/v1/link-suggestions/accept/`` and ``/dismiss/`` decide one,
  with the permission that makes the link, and 404 for what the caller cannot see.
- "Possibly related" on the issue and wiki pages lists them, offers Accept and
  Dismiss only to those allowed, and posts back to re-render the list.
- The project setting ``similar_links_threshold`` is validated and shown in the detail.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.link_suggestion import EntitySuggestion
from specivo.models.relation import IssueRelation
from specivo.services.link_suggestion_service import THRESHOLD_SETTING, compute_for_project
from tests.factories.user import TEST_PASSWORD
from tests.integration.link_graph_helpers import World, build_world, issue, page, wiki_ref
from tests.integration.link_suggestion_helpers import _no_queued_tasks, add_permission, cos, default_model, embed

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]
NEAR_A = [0.95, 0.31, 0.0, 0.0]
# Close to A (0.90), not to NEAR_A (0.77): the twin is suggested for the root only.
TWIN = [0.9, -0.3, 0.3, 0.0]
SCORE = f"{cos(A, NEAR_A):.3f}"


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "SGA")


@pytest.fixture
async def suggested(db_session: AsyncSession, world: World):
    """Root issue ~ near page, and root ~ twin issue, as open suggestions (and nothing else)."""
    model = await default_model(db_session, "sga-mock")
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near page")
    twin = await issue(db_session, world, world.home, "Twin")
    for item, vector in ((root, A), (near, NEAR_A), (twin, TWIN)):
        await embed(db_session, model, item, vector)
    await compute_for_project(db_session, world.home.id)
    await db_session.commit()
    assert cos(TWIN, NEAR_A) < 0.8
    return root, near, twin


async def _token(client: AsyncClient, login: str) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
async def reader_headers(client: AsyncClient, world: World) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _token(client, world.reader.login)}"}


async def test_links_endpoint_returns_similar_edges_only_when_asked(client, reader_headers, suggested, world):
    root, near, twin = suggested
    url = f"/api/v1/issues/{root.display_key}/links/"
    plain = (await client.get(url, headers=reader_headers)).json()
    assert all(e["kind"] != "similar" for e in plain["edges"])

    for query in ("?include_suggestions=true", "?kinds=similar"):
        data = (await client.get(url + query, headers=reader_headers)).json()
        similar = {e["target"]: e for e in data["edges"] if e["kind"] == "similar"}
        assert set(similar) == {wiki_ref(world.home, "near-page"), twin.display_key}
        edge = similar[wiki_ref(world.home, "near-page")]
        assert (edge["label"], edge["suggested"], edge["source"]) == (SCORE, True, root.display_key)

    page_url = f"/api/v1/projects/{world.home.key}/wiki/near-page/links/?include_suggestions=true"
    data = (await client.get(page_url, headers=reader_headers)).json()
    assert [(e["kind"], e["target"]) for e in data["edges"]] == [("similar", root.display_key)]


async def test_accept_and_dismiss_over_rest(client, reader_headers, suggested, db_session, world):
    root, near, twin = suggested
    body = {"ref": root.display_key, "other_ref": twin.display_key, "link_type": "duplicates"}
    resp = await client.post("/api/v1/link-suggestions/accept/", json=body, headers=reader_headers)
    assert resp.status_code == 403, resp.text

    await add_permission(db_session, world.reader, world.home, "manage_issue_relations")
    resp = await client.post("/api/v1/link-suggestions/accept/", json=body, headers=reader_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["status"], data["link_kind"], data["link_type"]) == ("accepted", "relation", "duplicates")
    relation = (await db_session.execute(select(IssueRelation))).scalar_one()
    assert relation.id == data["relation_id"]

    # Dismissing the page needs edit_issues on the issue it is decided from.
    body = {"ref": root.display_key, "other_ref": wiki_ref(world.home, "near-page")}
    resp = await client.post("/api/v1/link-suggestions/dismiss/", json=body, headers=reader_headers)
    assert resp.status_code == 403
    await add_permission(db_session, world.reader, world.home, "edit_issues")
    resp = await client.post("/api/v1/link-suggestions/dismiss/", json=body, headers=reader_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dismissed"
    again = await client.post("/api/v1/link-suggestions/dismiss/", json=body, headers=reader_headers)
    assert again.status_code == 422
    statuses = {row.status for row in (await db_session.execute(select(EntitySuggestion))).scalars()}
    assert statuses == {"accepted", "dismissed"}


async def test_rest_refuses_what_the_caller_cannot_see(client, reader_headers, suggested, db_session, world):
    root, _near, _twin = suggested
    secret = await issue(db_session, world, world.hidden, "Secret")
    body = {"ref": root.display_key, "other_ref": secret.display_key}
    resp = await client.post("/api/v1/link-suggestions/accept/", json=body, headers=reader_headers)
    assert resp.status_code == 404
    assert "Secret" not in resp.text
    resp = await client.post("/api/v1/link-suggestions/accept/", json={"ref": root.display_key}, headers=reader_headers)
    assert resp.status_code == 422
    unauth = await client.post("/api/v1/link-suggestions/accept/", json=body)
    assert unauth.status_code == 401


async def test_issue_page_lists_suggestions_with_actions_for_those_allowed(client, suggested, db_session, world):
    root, _near, twin = suggested
    token = await _token(client, world.reader.login)
    cookies = {"access_token": token}
    resp = await client.get(f"/partials/issues/{root.display_key}/suggestions/", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert "Near page" in resp.text and "Twin" in resp.text and SCORE in resp.text
    assert "Accept" not in resp.text  # the reader may not link anything

    await add_permission(db_session, world.reader, world.home, "manage_issue_relations")
    resp = await client.get(f"/partials/issues/{root.display_key}/suggestions/", cookies=cookies)
    html = resp.text
    assert html.count('value="accept"') == 1  # the twin issue only; the page needs edit_issues
    assert 'value="relates"' in html

    form = {"ref": root.display_key, "other_ref": twin.display_key, "op": "accept", "link_type": "relates"}
    resp = await client.post("/partials/link-suggestions/", data=form, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert "Linked." in resp.text
    assert "Twin" not in resp.text  # now a relation, no longer a suggestion
    assert (await db_session.execute(select(IssueRelation))).scalar_one().relation_type == "relates"

    form = {"ref": root.display_key, "other_ref": wiki_ref(world.home, "near-page"), "op": "dismiss"}
    resp = await client.post("/partials/link-suggestions/", data=form, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "You do not have permission to link these items." in resp.text
    assert "Near page" in resp.text


async def test_wiki_page_lists_suggestions_and_hides_invisible_ones(client, suggested, db_session, world):
    root, _near, _twin = suggested
    root.is_private = True
    await db_session.commit()
    token = await _token(client, world.reader.login)
    resp = await client.get(
        f"/partials/projects/{world.home.key}/wiki/near-page/suggestions/", cookies={"access_token": token}
    )
    assert resp.status_code == 200
    assert "Root" not in resp.text
    assert "No suggestions" in resp.text
    missing = await client.get(
        f"/partials/projects/{world.hidden.key}/wiki/near-page/suggestions/", cookies={"access_token": token}
    )
    assert missing.status_code == 404


async def test_project_threshold_setting(admin_client, world):
    url = f"/api/v1/projects/{world.home.key}/"
    resp = await admin_client.patch(url, json={THRESHOLD_SETTING: 0.42})
    assert resp.status_code == 422
    resp = await admin_client.patch(url, json={THRESHOLD_SETTING: 0.9})
    assert resp.status_code == 200, resp.text
    assert resp.json()[THRESHOLD_SETTING] == 0.9
    assert (await admin_client.get(url)).json()[THRESHOLD_SETTING] == 0.9
