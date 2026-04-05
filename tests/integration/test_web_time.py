"""Web time tracking page integration tests.

Verifies time entries list and log-time form pages render correctly
with proper auth checks.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.models.time_entry import TimeEntryActivity
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _project(db_session: AsyncSession) -> Project:
    """Persisted test project for time entry tests."""
    proj = ProjectFactory.build(key="WTE", identifier="web-time-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: time entries list page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_time_entries_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/time-entries with auth returns 200."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/time-entries/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Time" in resp.text


@pytest.mark.integration
async def test_time_entries_requires_auth(unauth_client: AsyncClient):
    """GET /projects/{key}/time-entries without auth redirects to /login."""
    resp = await unauth_client.get(
        "/projects/ANY/time-entries/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests: time entry form page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_time_entry_form(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/time-entries/new with auth returns 200."""
    # Seed an activity for the form dropdown (select-or-insert to avoid seed data collision)
    result = await db_session.execute(
        select(TimeEntryActivity).where(TimeEntryActivity.name == "Development")
    )
    activity = result.scalar_one_or_none()
    if activity is None:
        activity = TimeEntryActivityFactory.build(name="Development")
        db_session.add(activity)
        await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/time-entries/new/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Log Time" in resp.text
