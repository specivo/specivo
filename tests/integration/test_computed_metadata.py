"""Integration tests for project-derived (computed) issue metadata.

A metadata field whose value is a strict function of the project (configured in
``project.settings["computed_metadata"]``) must:
- auto-appear on read for issues in that project, across all creation paths;
- never be stored on the issue (cannot drift);
- be ignored when a client tries to set it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.computed_metadata_service import COMPUTED_METADATA_SETTINGS_KEY
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=open_status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(
        key="FIN",
        identifier="fin-app",
        settings={COMPUTED_METADATA_SETTINGS_KEY: {"Area": "Finance"}},
    )
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="cm_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


async def _create_issue(client: AsyncClient, token: str, project: Project, tracker, status, prio, metadata=None):
    body = {
        "project_key": project.key,
        "tracker_id": tracker.id,
        "subject": "Issue",
        "status_id": status.id,
        "priority_id": prio.id,
    }
    if metadata is not None:
        body["metadata"] = metadata
    return await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_created_issue_shows_project_computed_field(
    client, db_session, admin_token, project, tracker, open_status, priority
):
    """Create without the field → it appears on read = the project's value."""
    resp = await _create_issue(client, admin_token, project, tracker, open_status, priority)
    assert resp.status_code == 201, resp.text
    assert resp.json()["metadata"].get("Area") == "Finance"

    key = resp.json()["key"]
    get_resp = await client.get(f"/api/v1/issues/{key}/", headers={"Authorization": f"Bearer {admin_token}"})
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["metadata"].get("Area") == "Finance"


@pytest.mark.asyncio
async def test_computed_field_not_persisted(
    client, db_session, admin_token, project, tracker, open_status, priority
):
    """The derived value must never be written to issue_metadata (cannot drift)."""
    resp = await _create_issue(client, admin_token, project, tracker, open_status, priority)
    issue_id = resp.json()["id"]

    from specivo.models.issue import Issue

    db_issue = await db_session.get(Issue, issue_id)
    await db_session.refresh(db_issue)
    assert "Area" not in (db_issue.issue_metadata or {})


@pytest.mark.asyncio
async def test_client_cannot_set_computed_field(
    client, db_session, admin_token, project, tracker, open_status, priority
):
    """A client value for the computed key is stripped, not stored; read shows project value."""
    resp = await _create_issue(
        client, admin_token, project, tracker, open_status, priority,
        metadata={"Area": "Forged", "keep": "yes"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["metadata"].get("Area") == "Finance"  # project value, not the client's
    assert data["metadata"].get("keep") == "yes"  # non-computed metadata preserved

    from specivo.models.issue import Issue

    db_issue = await db_session.get(Issue, data["id"])
    await db_session.refresh(db_issue)
    assert "Area" not in (db_issue.issue_metadata or {})
    assert db_issue.issue_metadata.get("keep") == "yes"


@pytest.mark.asyncio
async def test_update_strips_computed_field(
    client, db_session, admin_token, project, tracker, open_status, priority
):
    """PATCH that includes the computed key must not persist it."""
    resp = await _create_issue(client, admin_token, project, tracker, open_status, priority)
    data = resp.json()

    patch = await client.patch(
        f"/api/v1/issues/{data['key']}/",
        json={"metadata": {"Area": "Forged", "note": "n"}, "lock_version": data["lock_version"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["metadata"].get("Area") == "Finance"
    assert patch.json()["metadata"].get("note") == "n"

    from specivo.models.issue import Issue

    db_issue = await db_session.get(Issue, data["id"])
    await db_session.refresh(db_issue)
    assert "Area" not in (db_issue.issue_metadata or {})


@pytest.mark.asyncio
async def test_configure_computed_metadata_via_project_update(
    client, db_session, admin_token, tracker, open_status, priority
):
    """Setting computed_metadata via ProjectService.update makes new issues derive it."""
    from specivo.schemas.project import ProjectUpdate
    from specivo.services.project_service import ProjectService

    plain = ProjectFactory.build(key="OPS", identifier="ops-app")
    db_session.add(plain)
    await db_session.commit()
    await db_session.refresh(plain)

    await ProjectService().update(
        db_session, plain, ProjectUpdate(computed_metadata={"Area": "Operations"})
    )
    await db_session.commit()

    resp = await _create_issue(client, admin_token, plain, tracker, open_status, priority)
    assert resp.status_code == 201, resp.text
    assert resp.json()["metadata"].get("Area") == "Operations"


@pytest.mark.asyncio
async def test_issue_in_project_without_config_has_no_computed_field(
    client, db_session, admin_token, tracker, open_status, priority
):
    """A project without computed_metadata config behaves exactly as before."""
    plain = ProjectFactory.build(key="PLAIN", identifier="plain-app")
    db_session.add(plain)
    await db_session.commit()
    await db_session.refresh(plain)

    resp = await _create_issue(client, admin_token, plain, tracker, open_status, priority)
    assert resp.status_code == 201, resp.text
    assert "Area" not in resp.json()["metadata"]
