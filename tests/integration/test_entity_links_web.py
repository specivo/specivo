"""The web blocks of the link graph: "Mentioned in" on the issue page, "Linked from" on a wiki page.

Both are htmx partials loaded on first opening; they show only the texts the
reader may see (no hidden title ever), and an empty state otherwise.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.user import TEST_PASSWORD
from tests.integration.link_graph_helpers import World, build_world, comment, issue, page, rebuild

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELW")


async def _cookie(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"access_token": resp.json()["access_token"]}


async def test_issue_mentions_are_grouped_and_filtered(client, db_session, world):
    root = await issue(db_session, world, world.home, "Mentioned issue")
    spec = await page(db_session, world, world.home, "Spec Page", f"Implements {root.display_key}")
    other = await issue(db_session, world, world.home, "Sibling", f"After {root.display_key}")
    discussed = await issue(db_session, world, world.home, "Discussion")
    note = await comment(db_session, world, discussed, f"Blocked by [{root.display_key}](/issue/{root.display_key}/)")
    private_note = await comment(db_session, world, discussed, f"Secret plan for {root.display_key}", private=True)
    hidden = await page(db_session, world, world.hidden, "Confidential Roadmap", f"Ships {root.display_key}")
    await rebuild(db_session, spec, other, note, private_note, hidden)

    cookies = await _cookie(client, world.reader.login)
    resp = await client.get(f"/partials/issues/{root.display_key}/mentions/", cookies=cookies)
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Spec Page" in html and "Sibling" in html
    assert f"{discussed.display_key}#{note.sequence}" in html
    assert f"/issue/{discussed.display_key}/#journal-{note.id}" in html
    assert "Confidential Roadmap" not in html
    assert f"#{private_note.sequence}<" not in html
    assert html.index("Wiki pages") < html.index("Issues") < html.index("Comments")


async def test_issue_mentions_empty_state_and_hidden_issue(client, db_session, world):
    lonely = await issue(db_session, world, world.home, "Nobody links here")
    cookies = await _cookie(client, world.reader.login)
    resp = await client.get(f"/partials/issues/{lonely.display_key}/mentions/", cookies=cookies)
    assert resp.status_code == 200
    assert "sp-backlinks-empty" in resp.text and "sp-backlinks-row" not in resp.text

    secret = await issue(db_session, world, world.hidden, "Hidden issue")
    resp = await client.get(f"/partials/issues/{secret.display_key}/mentions/", cookies=cookies)
    assert resp.status_code == 404
    assert "Hidden issue" not in resp.text


async def test_partials_need_a_signed_in_user(client, db_session, world):
    lonely = await issue(db_session, world, world.home, "Anonymous visitor")
    resp = await client.get(f"/partials/issues/{lonely.display_key}/mentions/", follow_redirects=False)
    assert resp.status_code == 302


async def test_wiki_linked_from(client, db_session, world):
    target = await page(db_session, world, world.home, "Glossary")
    user_issue = await issue(db_session, world, world.home, "Uses terms", "See [[Glossary]]")
    secret = await page(
        db_session, world, world.hidden, "Secret Glossary Fork", f"[x](/projects/{world.home.key}/wiki/glossary/)"
    )
    await rebuild(db_session, user_issue, secret)

    cookies = await _cookie(client, world.reader.login)
    resp = await client.get(f"/partials/projects/{world.home.key}/wiki/glossary/backlinks/", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert user_issue.display_key in resp.text
    assert "Secret Glossary Fork" not in resp.text

    empty = await page(db_session, world, world.home, "Orphan")
    resp = await client.get(f"/partials/projects/{world.home.key}/wiki/orphan/backlinks/", cookies=cookies)
    assert "sp-backlinks-empty" in resp.text
    resp = await client.get(f"/partials/projects/{world.hidden.key}/wiki/orphan/backlinks/", cookies=cookies)
    assert resp.status_code == 404
    assert target.id and empty.id


async def test_pages_carry_the_lazy_blocks(client, db_session, world):
    root = await issue(db_session, world, world.home, "Has a relations tab")
    await page(db_session, world, world.home, "Readable")
    cookies = await _cookie(client, world.reader.login)
    resp = await client.get(f"/issue/{root.display_key}/", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert f'hx-get="/partials/issues/{root.display_key}/mentions/"' in resp.text
    assert 'hx-trigger="sp-show-tab-relations from:body once"' in resp.text

    resp = await client.get(f"/projects/{world.home.key}/wiki/readable/", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert f'hx-get="/partials/projects/{world.home.key}/wiki/readable/backlinks/"' in resp.text
    assert 'hx-trigger="toggle from:closest details once"' in resp.text
