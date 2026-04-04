"""Integration tests for activity feed pagination and per-page preference.

Covers:
- PATCH /api/v1/users/me/preferences/activity-per-page/ — save preference
- Validation of allowed per_page values
- Auth requirement on the preference endpoint
- GET /projects/{key}/issues/{ref}/?activity_page=N — pagination param handling
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import EnabledModule, Project
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

# ---------------------------------------------------------------------------
# Module-level service singletons
# ---------------------------------------------------------------------------

_issue_svc = IssueService()
_journal_svc = JournalService()

PREF_URL = "/api/v1/users/me/preferences/activity-per-page/"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _lookups(db_session: AsyncSession) -> dict:
    """Seed minimum lookup rows for issue creation."""
    status = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(status)
    await db_session.flush()

    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(tracker)

    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(priority)

    await db_session.commit()
    await db_session.refresh(status)
    await db_session.refresh(tracker)
    await db_session.refresh(priority)

    return {"status": status, "tracker": tracker, "priority": priority}


@pytest_asyncio.fixture
async def _project(db_session: AsyncSession) -> Project:
    """Project with issue_tracking module enabled."""
    proj = ProjectFactory.build(key="PAG", identifier="pagination-test")
    db_session.add(proj)
    await db_session.flush()

    module = EnabledModule(project_id=proj.id, name="issue_tracking")
    db_session.add(module)

    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def _issue(
    db_session: AsyncSession,
    _project: Project,
    _lookups: dict,
    admin_client: AsyncClient,
) -> object:
    """A persisted test issue ready for detail-page tests."""
    user = admin_client.state.user
    data = IssueCreate(
        project_key=_project.key,
        tracker_id=_lookups["tracker"].id,
        subject="Pagination test issue",
        status_id=_lookups["status"].id,
        priority_id=_lookups["priority"].id,
    )
    issue = await _issue_svc.create(db_session, _project, data, user)
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# Tests: preference save endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_save_activity_per_page_preference(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: dict,
    _project: Project,
) -> None:
    """PATCH with per_page=100 returns 200 with the saved value and persists to DB.

    Verifies both the API response shape and that the user's preferences JSONB
    column is updated in the database after the request.
    """
    token = admin_client.state.token
    user = admin_client.state.user

    resp = await admin_client.patch(
        f"{PREF_URL}?per_page=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["activity_per_page"] == 100

    # Verify persisted in DB — refresh user row to get the updated JSONB value.
    await db_session.refresh(user)
    assert user.preferences.get("activity_per_page") == 100


@pytest.mark.integration
async def test_save_invalid_per_page_rejected(
    admin_client: AsyncClient,
    _project: Project,
) -> None:
    """PATCH with per_page=10 (not in ACTIVITY_PER_PAGE_OPTIONS) returns 422."""
    token = admin_client.state.token

    resp = await admin_client.patch(
        f"{PREF_URL}?per_page=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_save_preference_requires_auth(
    unauth_client: AsyncClient,
) -> None:
    """PATCH without a valid token returns 401."""
    resp = await unauth_client.patch(f"{PREF_URL}?per_page=100")
    assert resp.status_code == 401, resp.text


@pytest.mark.integration
async def test_preference_persists_across_requests(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: dict,
    _project: Project,
) -> None:
    """Saving per_page=100 twice shows the value is stable in DB (idempotent).

    A second PATCH with the same value must still return 200 and the preference
    must remain set to 100 in the DB.  This confirms the merge strategy
    (dict spread) does not clobber the value.
    """
    token = admin_client.state.token
    user = admin_client.state.user

    for _ in range(2):
        resp = await admin_client.patch(
            f"{PREF_URL}?per_page=100",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    await db_session.refresh(user)
    assert user.preferences.get("activity_per_page") == 100


# ---------------------------------------------------------------------------
# Tests: pagination behaviour on the issue detail page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_activity_page_param_works(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: dict,
    _project: Project,
    _issue: object,
) -> None:
    """GET detail with activity_page=1 returns 200 and renders all 3 comments.

    With only 3 journals and a default per-page of 50, all comments must be
    visible on page 1.
    """
    user = admin_client.state.user
    comments = ["Alpha comment", "Beta comment", "Gamma comment"]
    for text in comments:
        await _journal_svc.add_comment(db_session, _issue, user, text)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{_issue.display_key}/?activity_page=1",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    html = resp.text
    for text in comments:
        assert text in html, f"Expected comment {text!r} in HTML"


@pytest.mark.integration
async def test_activity_page_param_accepted_without_error(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: dict,
    _project: Project,
    _issue: object,
) -> None:
    """GET detail with activity_page=2 returns 200 even when only one page exists.

    The handler clamps out-of-range pages to the last available page rather
    than returning an error, so page 2 on a small issue must still succeed.
    """
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{_issue.display_key}/?activity_page=2",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
async def test_activity_page_out_of_range_clamped(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: dict,
    _project: Project,
    _issue: object,
) -> None:
    """GET detail with activity_page=999 returns 200 — clamped to last page.

    The implementation caps the page index at activity_total_pages so an
    absurdly large value must not produce a 4xx or 5xx response.
    """
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{_issue.display_key}/?activity_page=999",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    # Page must still contain the issue subject so we know the template rendered.
    assert "Pagination test issue" in resp.text


@pytest.mark.integration
async def test_default_per_page_used_when_no_preference(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: dict,
    _project: Project,
    _issue: object,
) -> None:
    """Issue detail renders correctly for a user with no activity_per_page preference.

    The admin_client fixture creates a fresh user whose preferences dict is
    empty.  The page must fall back to ACTIVITY_DEFAULT_PER_PAGE (50) and
    return 200.
    """
    user = admin_client.state.user

    # Confirm no preference is set on the fixture user.
    await db_session.refresh(user)
    assert "activity_per_page" not in user.preferences

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{_issue.display_key}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    assert "Pagination test issue" in resp.text
