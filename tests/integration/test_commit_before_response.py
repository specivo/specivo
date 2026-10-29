"""A request's DB transaction is committed before its response is sent.

The session dependency commits after ``yield``. Under FastAPI's default
request scope that code runs only after the response has gone out, so a
client that writes and then reads straight away (a browser reload after a
PATCH, an API client, an MCP agent) could read the old row. The dependency is
declared with ``scope="function"`` so the commit runs before the response.

Two checks:

- an ordering test on a real write (the sprint PATCH): it records when the
  session commits and when the ASGI ``http.response.start`` message is sent,
  and requires the commit first. No timing is involved, so it is
  deterministic;
- a static check over every route, so a new endpoint that depends on the
  session with a bare ``Depends(get_db)`` fails the suite.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Iterator

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import _IncludedRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from starlette.routing import BaseRoute
from starlette.types import Message, Receive, Scope, Send

from specivo.core.database import get_db
from specivo.models.project import EnabledModule
from specivo.testing.conftest_base import _test_app
from tests.factories.project import ProjectFactory

pytestmark = pytest.mark.integration


def _recording_get_db(connection: AsyncConnection, events: list[str]):
    """The test suite's session override, noting when it commits."""

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        await connection.begin_nested()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
            await session.commit()
            events.append("commit")
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    return _override


def _recording_app(events: list[str]):
    """Wrap the test app, noting when the response starts going out."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                events.append("response")
            await send(message)

        await _test_app(scope, receive, recording_send)

    return app


async def test_sprint_patch_commits_before_the_response_is_sent(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _test_connection: AsyncConnection,
) -> None:
    project = ProjectFactory.build(key="CBR", identifier="commit-before-response")
    db_session.add(project)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db_session.flush()

    created = await admin_client.post(f"/api/v1/projects/{project.key}/sprints/", json={"name": "Old name"})
    assert created.status_code == 201, created.text
    sprint_id = created.json()["id"]

    events: list[str] = []
    _test_app.dependency_overrides[get_db] = _recording_get_db(_test_connection, events)
    transport = ASGITransport(app=_recording_app(events))
    async with AsyncClient(transport=transport, base_url="http://test", headers=admin_client.headers) as client:
        resp = await client.patch(
            f"/api/v1/projects/{project.key}/sprints/{sprint_id}/",
            json={"name": "New name"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "New name"
    assert events == ["commit", "response"]


# ---------------------------------------------------------------------------
# Every route depends on the session the same way
# ---------------------------------------------------------------------------


def _effective_routes(routes: list[BaseRoute]) -> Iterator[object]:
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _effective_routes(route.effective_candidates())  # type: ignore[arg-type]
        else:
            yield route


def _uses_session(dependant: Dependant) -> bool:
    return dependant.call is get_db or any(_uses_session(sub) for sub in dependant.dependencies)


def _scope_problems(dependant: Dependant, path: str) -> Iterator[str]:
    for sub in dependant.dependencies:
        name = getattr(sub.call, "__qualname__", repr(sub.call))
        if sub.call is get_db:
            if sub.scope != "function":
                yield f"{path}: get_db with scope={sub.scope!r}"
        elif (inspect.isasyncgenfunction(sub.call) or inspect.isgeneratorfunction(sub.call)) and _uses_session(sub):
            # Its exit code would run after the session it holds has closed.
            if sub.scope != "function":
                yield f"{path}: yield dependency {name} uses the session with scope={sub.scope!r}"
        yield from _scope_problems(sub, path)


def test_every_route_commits_before_its_response() -> None:
    problems: list[str] = []
    checked = 0
    for route in _effective_routes(_test_app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        path = getattr(route, "path", "?")
        if _uses_session(dependant):
            checked += 1
        if inspect.isasyncgenfunction(dependant.call) and _uses_session(dependant):
            problems.append(f"{path}: streaming endpoint reads the session after it has closed")
        problems.extend(_scope_problems(dependant, path))

    assert checked > 100, "route walk found too few routes; has FastAPI's router layout changed?"
    assert not problems, "Use DbSession / DB_SESSION, not Depends(get_db):\n" + "\n".join(problems)
