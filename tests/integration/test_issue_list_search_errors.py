"""Issue list and search inputs that used to end in a 500 or in unstable pages."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.issue import Issue
from specivo.models.project import EnabledModule, Project
from specivo.models.saved_filter import SavedFilter
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.auth_service import _make_access_token
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> tuple[User, Project]:
    db = db_session
    status = StatusFactory.build(name="ILS New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="ILS Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="ILS Normal", position=1, is_default=True)
    project = ProjectFactory.build(key="ILS", identifier="ils")
    admin = AdminUserFactory.build(login="ils_admin", status="active")
    db.add_all([tracker, priority, project, admin])
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    for n in range(1, 8):
        await IssueService().create(
            db, project, IssueCreate(project_key="ILS", tracker_id=tracker.id, subject=f"ILS {n}"), admin
        )
    # Two days of history: issues 1-3 on March 1, 4-7 on March 2.
    for n in range(1, 8):
        day = 1 if n <= 3 else 2
        await db.execute(
            update(Issue)
            .where(Issue.project_id == project.id, Issue.sequence_number == n)
            .values(created_at=datetime(2026, 3, day, 12, tzinfo=UTC))
        )
    await db.commit()
    return admin, project


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _keys(client: AsyncClient, user: User, **params) -> list[str]:
    resp = await client.get("/api/v1/projects/ILS/issues/", params={"status": "all", **params}, headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return [i["key"] for i in resp.json()["items"]]


async def test_date_filters_take_a_date_or_a_timestamp(client: AsyncClient, world) -> None:
    admin, _project = world
    assert len(await _keys(client, admin, created_after="2026-03-02")) == 4
    # A bare "before" date covers that whole day.
    assert len(await _keys(client, admin, created_before="2026-03-01")) == 3
    assert len(await _keys(client, admin, created_before="2026-03-01T12:00:00Z")) == 3
    resp = await client.get("/api/v1/projects/ILS/issues/", params={"created_after": "soon"}, headers=_auth(admin))
    assert resp.status_code == 422


async def test_saved_filters_are_typed(client: AsyncClient, db_session: AsyncSession, world) -> None:
    admin, project = world
    resp = await client.post(
        "/api/v1/projects/ILS/saved-filters/",
        json={"name": "Broken", "filter_definition": {"tracker_id": "x"}},
        headers=_auth(admin),
    )
    assert resp.status_code == 422

    # One saved before definitions were checked answers 422 too, not 500.
    stored = SavedFilter(
        name="Old", user_id=admin.id, project_id=project.id, filter_definition={"tracker_id": "x"}, is_public=False
    )
    db_session.add(stored)
    await db_session.commit()
    resp = await client.get("/api/v1/projects/ILS/issues/", params={"saved_filter_id": stored.id}, headers=_auth(admin))
    assert resp.status_code == 422, resp.text


async def test_pages_of_a_tied_sort_neither_skip_nor_repeat(client: AsyncClient, world) -> None:
    admin, _project = world
    # Every issue has the same priority: the sort alone does not order them.
    seen = []
    for offset in range(7):
        seen += await _keys(client, admin, sort="priority_id:asc", limit=1, offset=offset)
    assert sorted(seen) == sorted(f"ILS-{n}" for n in range(1, 8))


async def test_an_out_of_range_display_key_is_not_found(client: AsyncClient, world) -> None:
    admin, _project = world
    for ref in ("ILS-99999999999", "ILS-0"):
        assert (await client.get(f"/api/v1/issues/{ref}/", headers=_auth(admin))).status_code == 404


async def test_search_reads_the_project_key_in_any_case(client: AsyncClient, world) -> None:
    admin, _project = world
    resp = await client.get(
        "/api/v1/search/", params={"q": "ILS", "project_key": "ils", "mode": "keyword"}, headers=_auth(admin)
    )
    assert resp.status_code == 200, resp.text
