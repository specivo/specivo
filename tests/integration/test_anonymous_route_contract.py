"""The boundary of anonymous access, checked against the routing table itself.

Anonymous access has two surfaces — the JSON API and the web pages — and each
declares its own allowlist. The properties below hold for the whole
application rather than for the handful of routes somebody remembered to test:

- the set of routes that depend on ``get_reader`` equals
  ``ANONYMOUS_READ_ROUTES`` exactly, and the set of pages that depend on
  ``get_web_reader`` equals ``ANONYMOUS_WEB_ROUTES`` exactly. Set equality
  both ways, so a route cannot join either surface by accident and one that is
  declared cannot silently stop being served;
- every route on either surface is a GET;
- with the switch on and a project opted in, every non-GET route under
  ``/api/v1`` still refuses an unauthenticated caller.

These tests read the routing table, so they fail the moment a handler is given
the wrong dependency — before anything is deployed. The web pages' own
behaviour is covered in ``test_anonymous_web_access.py``.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.security import ANONYMOUS_READ_ROUTES, get_reader
from specivo.models.project import EnabledModule, Project
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.permission_service import Permission
from specivo.testing.conftest_base import _test_app
from specivo.web.deps import ANONYMOUS_WEB_ROUTES, get_web_reader
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

# No module-level asyncio mark: most of this file inspects the routing table
# synchronously, and ``asyncio_mode = "auto"`` already handles the async tests.
pytestmark = [pytest.mark.integration]

# Everything under this prefix authenticates somebody or lets them recover an
# account, so it is unauthenticated by design and outside the enumeration.
_AUTH_PREFIX = "/api/v1/auth/"

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _reader_routes() -> dict[str, set[str]]:
    """Return every API route reached through ``get_reader``, by path.

    The walk is recursive because a route can depend on ``get_reader``
    indirectly — the search route reaches it through the dependency that
    meters anonymous searches.
    """

    def uses_reader(dependant) -> bool:
        if dependant.call is get_reader:
            return True
        return any(uses_reader(sub) for sub in dependant.dependencies)

    return {
        route.path: set(route.methods)
        for route in _test_app.routes
        if isinstance(route, APIRoute) and uses_reader(route.dependant)
    }


# ---------------------------------------------------------------------------
# The allowlist is the whole surface
# ---------------------------------------------------------------------------


def test_reader_routes_match_the_declared_allowlist() -> None:
    wired = _reader_routes()
    assert set(wired) == set(ANONYMOUS_READ_ROUTES), (
        "A route's get_reader dependency and ANONYMOUS_READ_ROUTES disagree. "
        "Adding a route to the anonymous surface means doing both, deliberately."
    )


def test_every_anonymous_route_is_a_get() -> None:
    for path, methods in _reader_routes().items():
        assert methods <= {"GET", "HEAD"}, f"{path} is served to anonymous visitors with {sorted(methods)}"


def test_the_allowlist_is_the_set_that_was_reviewed() -> None:
    """Spelled out so widening it shows up as a change to this file too."""
    assert set(ANONYMOUS_READ_ROUTES) == {
        "/api/v1/projects/",
        "/api/v1/projects/{project_key}/issues/",
        "/api/v1/issues/{issue_ref}/",
        "/api/v1/projects/{project_key}/wiki/{slug}/",
        "/api/v1/search/",
    }


# ---------------------------------------------------------------------------
# The same two properties, for the web pages
# ---------------------------------------------------------------------------


def _web_reader_routes() -> dict[str, set[str]]:
    """Return every web route reached through ``get_web_reader``, by path."""

    def uses_web_reader(dependant) -> bool:
        if dependant.call is get_web_reader:
            return True
        return any(uses_web_reader(sub) for sub in dependant.dependencies)

    return {
        route.path: set(route.methods)
        for route in _test_app.routes
        if isinstance(route, APIRoute) and uses_web_reader(route.dependant)
    }


def test_web_reader_routes_match_the_declared_allowlist() -> None:
    wired = _web_reader_routes()
    assert set(wired) == set(ANONYMOUS_WEB_ROUTES), (
        "A page's get_web_reader dependency and ANONYMOUS_WEB_ROUTES disagree. "
        "Adding a page to the anonymous surface means doing both, deliberately."
    )


def test_every_anonymous_web_route_is_a_get() -> None:
    for path, methods in _web_reader_routes().items():
        assert methods <= {"GET", "HEAD"}, f"{path} is served to anonymous visitors with {sorted(methods)}"


def test_the_two_allowlists_do_not_overlap() -> None:
    """The API and the web surfaces are declared separately and stay separate."""
    assert not (set(ANONYMOUS_READ_ROUTES) & set(ANONYMOUS_WEB_ROUTES))


# ---------------------------------------------------------------------------
# Nothing that writes is reachable
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def anonymous_access_live(db_session: AsyncSession) -> Project:
    """The most permissive configuration: switch on, project fully opted in."""
    admin = AdminUserFactory.build(login="anonroute_admin", status="active")
    db_session.add(admin)
    await db_session.commit()

    project = ProjectFactory.build(key="ANONROUTE", identifier="anonroute", is_public=True)
    db_session.add(project)
    await db_session.flush()
    for module in ("issue_tracking", "wiki"):
        db_session.add(EnabledModule(project_id=project.id, name=module))
    await db_session.commit()

    await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
    await db_session.commit()
    return project


def _mutating_api_routes() -> list[tuple[str, str]]:
    """Every non-GET API route, as ``(method, concrete path)`` pairs."""
    out: list[tuple[str, str]] = []
    for route in _test_app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
            continue
        if route.path.startswith(_AUTH_PREFIX):
            continue
        for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
            out.append((method, _PATH_PARAM.sub("1", route.path)))
    return out


def test_there_are_mutating_routes_to_check() -> None:
    """Guards the enumeration below against silently checking nothing."""
    assert len(_mutating_api_routes()) > 30


async def test_no_mutating_route_serves_an_anonymous_caller(
    client: AsyncClient, anonymous_access_live: Project
) -> None:
    """Anonymous access is read-only, and that is a property of every route."""
    served: list[str] = []
    for method, path in _mutating_api_routes():
        resp = await client.request(method, path, json={})
        if resp.status_code != 401:
            served.append(f"{method} {path} -> {resp.status_code}")

    assert not served, "these routes did not refuse an unauthenticated caller:\n" + "\n".join(served)


async def test_mcp_without_an_api_key_is_refused(client: AsyncClient, anonymous_access_live: Project) -> None:
    """MCP stays API-key only; the anonymous principal never reaches it."""
    resp = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401
