"""Web issue page integration tests.

Verifies issue list, detail, create form, edit form, journal display,
and htmx partial rendering with proper auth checks and content.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_issue_svc = IssueService()
_journal_svc = JournalService()


@pytest_asyncio.fixture
async def _lookups(db_session: AsyncSession) -> tuple[IssueStatus, Tracker, IssuePriority]:
    """Seed the minimum lookup rows needed to create issues."""
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
    return status, tracker, priority


@pytest_asyncio.fixture
async def _project(db_session: AsyncSession) -> Project:
    """Persisted test project."""
    proj = ProjectFactory.build(key="WIS", identifier="web-issue-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


async def _create_issue(
    db_session: AsyncSession,
    project: Project,
    user: User,
    tracker: Tracker,
    *,
    subject: str = "Test issue subject",
) -> object:
    """Create an issue via the service layer and commit."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject=subject,
    )
    issue = await _issue_svc.create(db_session, project, data, user)
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# Tests: issue list page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issues_list_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: tuple,
    _project: Project,
):
    """GET /projects/{key}/issues with auth returns 200 and contains 'Issues'."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Issues" in resp.text


@pytest.mark.integration
async def test_issues_list_requires_auth(unauth_client: AsyncClient):
    """GET /projects/{key}/issues without auth redirects to /login."""
    resp = await unauth_client.get(
        "/projects/ANY/issues/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests: issue detail page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_detail_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: tuple,
    _project: Project,
):
    """GET /projects/{key}/issues/{ref} returns 200 and contains the subject."""
    _, tracker, _ = _lookups
    user = admin_client.state.user
    issue = await _create_issue(db_session, _project, user, tracker, subject="Detail page bug")
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{issue.display_key}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Detail page bug" in resp.text


# ---------------------------------------------------------------------------
# Tests: issue create form
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_create_form(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: tuple,
    _project: Project,
):
    """GET /projects/{key}/issues/new returns 200 and contains form elements."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/new/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "subject" in resp.text.lower()


# ---------------------------------------------------------------------------
# Tests: issue edit form
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_edit_form(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: tuple,
    _project: Project,
):
    """GET /projects/{key}/issues/{ref}/edit returns 200 and contains the subject."""
    _, tracker, _ = _lookups
    user = admin_client.state.user
    issue = await _create_issue(db_session, _project, user, tracker, subject="Edit form bug")
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{issue.display_key}/edit/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Edit form bug" in resp.text


# ---------------------------------------------------------------------------
# Tests: issue detail shows journals
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_detail_shows_journals(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: tuple,
    _project: Project,
):
    """Issue detail page includes journal comment text."""
    _, tracker, _ = _lookups
    user = admin_client.state.user
    issue = await _create_issue(db_session, _project, user, tracker, subject="Journal test bug")
    await _journal_svc.add_comment(db_session, issue, user, "This is a test comment")
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/{issue.display_key}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "This is a test comment" in resp.text


# ---------------------------------------------------------------------------
# Tests: htmx partial — table rows
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_partial_table(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _lookups: tuple,
    _project: Project,
):
    """GET /partials/issues/table returns partial HTML without full page wrapper."""
    _, tracker, _ = _lookups
    user = admin_client.state.user
    await _create_issue(db_session, _project, user, tracker)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/partials/issues/table/?project_key={_project.key}",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    # Partial should NOT contain a full HTML page wrapper
    assert "<html" not in resp.text.lower()


# ---------------------------------------------------------------------------
# Tests: filter with empty string params (HTML form sends value="")
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_list_empty_filter_params(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project,
):
    """GET /projects/{key}/issues/?tracker_id=&assigned_to_id=&priority_id= returns 200.

    HTML <select> elements send empty strings for unselected options.
    The handler must treat these as None, not fail with a validation error.
    """
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/issues/?status=all&tracker_id=&assigned_to_id=&priority_id=",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}: {resp.text[:200]}"
    assert "Issues" in resp.text
