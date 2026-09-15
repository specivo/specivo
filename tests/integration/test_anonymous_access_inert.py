"""What opting a project in and turning the instance switch on changes, before any anonymous route exists.

Role resolution reads both settings, but no route serves the anonymous user
yet. This module snapshots the responses a signed-in non-member and an
unauthenticated visitor get from the project, issue, wiki and search
endpoints, stores both settings, and compares:

- an unauthenticated request gets exactly the same responses;
- a signed-in non-member gains exactly what the opted-in project now grants
  anonymous visitors — its wiki, in the wiki routes and in search — and
  nothing else changes, least of all anything in the private project.

JSON bodies are compared in full except for ``updated_at``: writing the
setting touches the project row, and that timestamp is not access. HTML pages
are compared by status code and redirect target.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    is_anonymous_access_enabled,
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _project(db: AsyncSession, key: str, identifier: str, is_public: bool) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, name=f"Inert {key}", is_public=is_public)
    db.add(proj)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=proj.id, name=module))
    await db.commit()
    return proj


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, client: AsyncClient) -> dict[str, Any]:
    """A public and a private project, each with an issue and a wiki page."""
    admin = AdminUserFactory.build(login="inert_admin", status="active")
    outsider = UserFactory.build(login="inert_outsider", status="active")
    db_session.add_all([admin, outsider])
    status = StatusFactory.build(name="Inert Open", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Inert Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Inert Normal", is_default=True, position=1)
    db_session.add_all([tracker, priority])
    await db_session.commit()

    public = await _project(db_session, "INPUB", "inert-public", is_public=True)
    private = await _project(db_session, "INPRIV", "inert-private", is_public=False)

    slugs = {}
    for proj in (public, private):
        resp = await client.post(
            f"/api/v1/projects/{proj.key}/issues/",
            json={
                "project_key": proj.key,
                "subject": f"Lighthouse issue in {proj.key}",
                "tracker_id": tracker.id,
                "status_id": status.id,
                "priority_id": priority.id,
            },
            headers=_bearer(admin),
        )
        assert resp.status_code == 201, resp.text
        page, _content = await WikiService().create_page(
            db_session,
            proj.id,
            f"Lighthouse page {proj.key}",
            "Lighthouse wiki text",
            admin,
            skip_search_index=True,
            skip_link_rebuild=True,
        )
        slugs[proj.key] = page.slug
    await db_session.commit()

    return {"admin": admin, "outsider": outsider, "public": public, "private": private, "slugs": slugs}


def _api_paths(world: dict[str, Any]) -> list[str]:
    paths = ["/api/v1/projects/", "/api/v1/search/?q=lighthouse"]
    for key in ("INPUB", "INPRIV"):
        paths += [
            f"/api/v1/projects/{key}/",
            f"/api/v1/projects/{key}/members/",
            f"/api/v1/projects/{key}/issues/",
            f"/api/v1/issues/{key}-1/",
            f"/api/v1/projects/{key}/wiki/",
            f"/api/v1/projects/{key}/wiki/{world['slugs'][key]}/",
        ]
    return paths


def _web_paths(world: dict[str, Any]) -> list[str]:
    paths = ["/projects/", "/search/?q=lighthouse"]
    for key in ("INPUB", "INPRIV"):
        paths += [
            f"/projects/{key}/",
            f"/projects/{key}/issues/",
            f"/projects/{key}/wiki/",
            f"/projects/{key}/wiki/{world['slugs'][key]}/",
        ]
    return paths


def _without_updated_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_updated_at(v) for k, v in value.items() if k != "updated_at"}
    if isinstance(value, list):
        return [_without_updated_at(v) for v in value]
    return value


async def _snapshot(client: AsyncClient, world: dict[str, Any], user: User | None) -> dict[str, tuple]:
    shots: dict[str, tuple] = {}
    for path in _api_paths(world):
        resp = await client.get(path, headers=_bearer(user) if user else {})
        body = _without_updated_at(resp.json()) if "json" in resp.headers.get("content-type", "") else None
        shots[path] = (resp.status_code, body)
    for path in _web_paths(world):
        resp = await client.get(path, cookies=_cookies(user) if user else {}, follow_redirects=False)
        shots[path] = (resp.status_code, resp.headers.get("location"))
    return shots


async def _opt_everything_in(db: AsyncSession, world: dict[str, Any]) -> None:
    await set_anonymous_permissions(db, world["public"], ["view_issues", "view_wiki"], world["admin"])
    await set_anonymous_access_enabled(db, True, world["admin"], confirmed_projects=["INPUB"])
    await db.commit()
    assert await is_anonymous_access_enabled(db)


async def test_enabling_anonymous_access_gives_a_signed_in_non_member_only_the_opted_in_wiki(
    client: AsyncClient, db_session: AsyncSession, world: dict[str, Any]
) -> None:
    """A signed-in user never sees less than an anonymous visitor would, and gains nothing more."""
    before = await _snapshot(client, world, world["outsider"])
    # The snapshot is only meaningful if it covers both outcomes.
    assert before["/api/v1/projects/INPUB/"][0] == 200
    assert before["/api/v1/projects/INPRIV/"][0] == 404

    await _opt_everything_in(db_session, world)
    after = await _snapshot(client, world, world["outsider"])

    slug = world["slugs"]["INPUB"]
    wiki_paths = {
        "/api/v1/projects/INPUB/wiki/",
        f"/api/v1/projects/INPUB/wiki/{slug}/",
        f"/projects/INPUB/wiki/{slug}/",
    }
    changed = {path for path in before if after[path] != before[path]}
    # The public project's issues were already visible through the Non member
    # role, so only its wiki and the search results that include it change.
    assert changed == wiki_paths | {"/api/v1/search/?q=lighthouse"}
    for path in wiki_paths:
        assert before[path][0] == 403 and after[path][0] == 200, path
    search_titles = {item["title"] for item in after["/api/v1/search/?q=lighthouse"][1]["items"]}
    assert "Lighthouse page INPUB" in search_titles
    assert "Lighthouse page INPRIV" not in search_titles


async def test_enabling_anonymous_access_changes_nothing_for_an_unauthenticated_request(
    client: AsyncClient, db_session: AsyncSession, world: dict[str, Any]
) -> None:
    before = await _snapshot(client, world, None)
    assert before["/api/v1/projects/INPUB/issues/"][0] == 401
    assert before["/projects/INPUB/"][0] in (302, 303)

    await _opt_everything_in(db_session, world)
    after = await _snapshot(client, world, None)

    assert after == before
