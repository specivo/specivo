"""Who sees which suggested links, over REST, on the web pages and in the service.

- A reader who may view issues but not the wiki sees no wiki page among the
  suggestions, and cannot open them from a page.
- A page in the trash is never the other end of a suggestion, and deciding a
  suggestion with it fails as for a page that does not exist.
- On a public project, a signed-in non-member and the anonymous reader see only
  suggestions whose two ends they may see.
- "N less similar suggestions not shown" counts only items the reader may see.
- Accepting or dismissing with a ref the caller may not see answers exactly as
  with a ref that does not exist.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.role import Role, RoleBuiltin
from specivo.services import link_suggestion_service as svc
from specivo.services.entity_link_service import KIND_SIMILAR, EntityLinkService
from specivo.services.permission_service import clear_role_cache
from specivo.services.wiki_service import WikiService
from tests.factories.user import TEST_PASSWORD, UserFactory
from tests.integration.link_graph_helpers import World, build_world, grant, issue, page, wiki_ref
from tests.integration.link_suggestion_helpers import _no_queued_tasks, default_model, embed, fill, key

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]
NEAR_A = [0.95, 0.31, 0.0, 0.0]

links = EntityLinkService()


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    clear_role_cache(db_session)
    world = await build_world(db_session, "SGX")
    yield world
    clear_role_cache(db_session)


@pytest.fixture
async def suggested(db_session: AsyncSession, world: World):
    """A root issue suggested with a page, a twin issue, a private issue and a page now in the trash."""
    model = await default_model(db_session, "sgx-mock")
    await fill(db_session, world, model, world.home)
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near page")
    twin = await issue(db_session, world, world.home, "Twin")
    private = await issue(db_session, world, world.home, "Private twin", private=True)
    trashed = await page(db_session, world, world.home, "Old page")
    for item, vector in ((root, A), (near, NEAR_A), (twin, A), (private, A), (trashed, A)):
        await embed(db_session, model, item, vector)
    await svc.compute_for_project(db_session, world.home.id)
    await db_session.commit()
    stored = {frozenset({row.source, row.target}) for row in await _rows(db_session)}
    assert frozenset({key(root), key(trashed)}) in stored and frozenset({key(root), key(private)}) in stored
    await WikiService().delete_page(db_session, trashed.id, world.admin)
    await db_session.commit()
    return root, near, twin, private, trashed


async def _rows(db: AsyncSession):
    from specivo.models.link_suggestion import EntitySuggestion

    return (await db.execute(select(EntitySuggestion))).scalars().all()


async def _user(db: AsyncSession, login: str):
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    return user


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _similar_refs(db: AsyncSession, user, root) -> set[str]:
    graph = await links.get_links(db, user, key(root), kinds=[KIND_SIMILAR])
    return {node.ref for node in graph.nodes}


# -- a reader without the wiki ---------------------------------------------------


async def test_a_reader_without_wiki_access_sees_no_page_suggestions(client, db_session, world, suggested):
    root, _near, twin, _private, _trashed = suggested
    wikiless = await _user(db_session, "sgx_wikiless")
    await grant(db_session, wikiless, world.home, ["view_issues"])
    headers = await _headers(client, wikiless.login)

    # REST, from the issue: the twin only.
    data = (
        await client.get(f"/api/v1/issues/{root.display_key}/links/?include_suggestions=true", headers=headers)
    ).json()
    targets = {e["target"] for e in data["edges"] if e["kind"] == "similar"}
    assert targets == {twin.display_key}
    assert "wiki:" not in str(data)

    # REST, from the page: refused, so no issue suggestion is reached from it either.
    page_url = f"/api/v1/projects/{world.home.key}/wiki/near-page/links/?include_suggestions=true"
    resp = await client.get(page_url, headers=headers)
    assert resp.status_code in (403, 404)
    assert root.display_key not in resp.text

    # Web: the issue's Relations tab lists no page; the page's list is refused.
    cookies = {"access_token": headers["Authorization"].removeprefix("Bearer ")}
    resp = await client.get(f"/partials/issues/{root.display_key}/suggestions/", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert "Twin" in resp.text
    assert "Near page" not in resp.text and "Old page" not in resp.text
    resp = await client.get(f"/partials/projects/{world.home.key}/wiki/near-page/suggestions/", cookies=cookies)
    assert resp.status_code == 404
    assert "Root" not in resp.text


# -- pages in the trash -------------------------------------------------------------


async def test_a_trashed_page_is_never_the_other_end(client, db_session, world, suggested):
    root, near, twin, private, _trashed = suggested
    assert await _similar_refs(db_session, world.admin, root) == {
        wiki_ref(world.home, "near-page"),
        twin.display_key,
        private.display_key,
    }
    assert wiki_ref(world.home, "old-page") not in await _similar_refs(db_session, world.reader, near)

    headers = await _headers(client, world.reader.login)
    data = (
        await client.get(f"/api/v1/issues/{root.display_key}/links/?include_suggestions=true", headers=headers)
    ).json()
    assert "old-page" not in str(data)
    cookies = {"access_token": headers["Authorization"].removeprefix("Bearer ")}
    resp = await client.get(f"/partials/issues/{root.display_key}/suggestions/", cookies=cookies)
    assert "Old page" not in resp.text

    # Recomputed with the page in the trash, it stays out.
    await svc.compute_for_project(db_session, world.home.id)
    assert wiki_ref(world.home, "old-page") not in await _similar_refs(db_session, world.admin, root)


async def test_deciding_with_a_trashed_page_fails_like_a_missing_one(client, db_session, world, suggested):
    root, _near, _twin, _private, trashed = suggested
    # Service: the same error for the trashed page and a page id that never existed.
    errors = []
    for other in (key(trashed), ("wiki_page", 2_000_000_000)):
        with pytest.raises(NotFoundError) as exc:
            await svc.decide(db_session, world.admin, "dismiss", key(root), other)
        errors.append(str(exc.value))
    assert errors[0] == errors[1]

    # REST: the same status and body for the trashed slug and a slug that never existed.
    headers = await _headers(client, world.reader.login)
    answers = []
    for slug in ("old-page", "never-was"):
        ref = wiki_ref(world.home, slug)
        for op in ("accept", "dismiss"):
            resp = await client.post(
                f"/api/v1/link-suggestions/{op}/", json={"ref": root.display_key, "other_ref": ref}, headers=headers
            )
            answers.append((op, resp.status_code, resp.text.replace(slug, "<slug>")))
    assert answers[:2] == answers[2:]
    assert answers[0][1] == 404
    assert {row.status for row in await _rows(db_session)} == {"suggested"}


# -- a public project -------------------------------------------------------------------


async def test_public_project_readers_see_only_suggestions_with_both_ends_visible(db_session, world, suggested):
    from specivo.services.anonymous_access_service import set_anonymous_access_enabled
    from specivo.services.anonymous_user_service import get_anonymous_user

    root, _near, twin, private, _trashed = suggested
    world.home.is_public = True
    world.home.anonymous_permissions = ["view_issues"]
    non_member = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()
    non_member.permissions = ["view_issues"]
    await db_session.commit()
    await set_anonymous_access_enabled(db_session, True, world.admin, confirmed_projects=[world.home.key])
    await db_session.commit()
    clear_role_cache(db_session)

    outsider = await _user(db_session, "sgx_outsider")
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    for user in (outsider, anonymous):
        # Issues only (no wiki), and no private issue: the twin alone.
        assert await _similar_refs(db_session, user, root) == {twin.display_key}, user.login
        graph = await links.get_links(db_session, user, key(twin), kinds=[KIND_SIMILAR])
        assert {n.ref for n in graph.nodes} == {root.display_key}
        assert private.display_key not in str(graph.as_dict())
    # A member who may read the wiki also sees the page, still not the private issue.
    assert await _similar_refs(db_session, world.reader, root) == {twin.display_key, wiki_ref(world.home, "near-page")}

    # Once the non-member role may read the wiki, the outsider sees the page too.
    non_member.permissions = ["view_issues", "view_wiki"]
    await db_session.commit()
    clear_role_cache(db_session)
    assert await _similar_refs(db_session, outsider, root) == {twin.display_key, wiki_ref(world.home, "near-page")}


# -- the "less similar" count ---------------------------------------------------------


async def test_the_not_shown_count_never_counts_invisible_items(client, db_session, world, suggested, monkeypatch):
    import specivo.core.constants as constants

    root, _near, _twin, _private, _trashed = suggested
    monkeypatch.setattr(constants, "SIMILAR_LINKS_WEB_LIMIT_PER_TYPE", 1)

    # The admin sees the twin and the private issue: one listed, one not shown.
    admin_cookies = {"access_token": (await _headers(client, world.admin.login))["Authorization"][7:]}
    resp = await client.get(f"/partials/issues/{root.display_key}/suggestions/", cookies=admin_cookies)
    assert "1 less similar suggestion not shown." in resp.text

    # The reader may not see the private issue (nor the trashed page): nothing is left over.
    cookies = {"access_token": (await _headers(client, world.reader.login))["Authorization"][7:]}
    resp = await client.get(f"/partials/issues/{root.display_key}/suggestions/", cookies=cookies)
    assert resp.status_code == 200
    assert "less similar" not in resp.text
    assert "Private twin" not in resp.text


# -- hidden refs answer like missing ones ------------------------------------------------


async def test_a_hidden_ref_answers_exactly_like_a_missing_one(client, db_session, world, suggested):
    root, _near, _twin, private, _trashed = suggested
    secret = await issue(db_session, world, world.hidden, "Secret")
    secret_page = await page(db_session, world, world.hidden, "Secret page")
    await svc.compute_for_project(db_session, world.home.id)
    headers = await _headers(client, world.reader.login)

    cases = [
        # A private issue of a project the reader can see, and an issue number never used there.
        (private.display_key, f"{world.home.key}-999999"),
        # An issue of a project the reader cannot see, and a number never used there.
        (secret.display_key, f"{world.hidden.key}-999999"),
        # A page of a project the reader cannot see, and a slug never used there.
        (wiki_ref(world.hidden, secret_page.slug), wiki_ref(world.hidden, "never-was")),
    ]
    for hidden_ref, missing_ref in cases:
        for op in ("accept", "dismiss"):
            answers = []
            for ref in (hidden_ref, missing_ref):
                resp = await client.post(
                    f"/api/v1/link-suggestions/{op}/",
                    json={"ref": root.display_key, "other_ref": ref},
                    headers=headers,
                )
                answers.append((resp.status_code, resp.text.replace(ref, "<ref>")))
            assert answers[0] == answers[1], (op, hidden_ref, answers)
            assert answers[0][0] == 404
            assert "Secret" not in answers[0][1] and "Private" not in answers[0][1]

    # The service itself refuses a hidden item and a missing one with the same error.
    errors = []
    for other in (key(private), ("issue", 2_000_000_000)):
        with pytest.raises(NotFoundError) as exc:
            await svc.decide(db_session, world.reader, "dismiss", key(root), other)
        errors.append(str(exc.value))
    assert errors[0] == errors[1]
