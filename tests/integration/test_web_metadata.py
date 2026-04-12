"""Web admin metadata presets page integration tests.

Verifies the metadata presets admin page renders correctly
and enforces proper access control, and the project settings
metadata tab is present.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.schemas.project import ProjectCreate
from specivo.services.project_service import ProjectService

BROWSER_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

_svc = ProjectService()


async def _create_project(
    db_session: AsyncSession,
    user,
    *,
    name: str = "Metadata Test Project",
    identifier: str = "meta-test-proj",
    key: str = "MTP",
    is_public: bool = False,
) -> object:
    """Create a project via the service layer and commit."""
    data = ProjectCreate(name=name, identifier=identifier, key=key, is_public=is_public)
    project = await _svc.create(db_session, data, user)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.integration
async def test_admin_metadata_presets_page(admin_client: AsyncClient):
    """GET /admin/metadata-presets/ with admin returns 200 with expected content."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/admin/metadata-presets/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Metadata Presets" in resp.text


@pytest.mark.integration
async def test_admin_metadata_presets_requires_admin(auth_client: AsyncClient):
    """GET /admin/metadata-presets/ as regular user returns 403."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/admin/metadata-presets/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_project_settings_metadata_tab(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /projects/{key}/settings/ contains the Metadata tab and preset grid."""
    user = admin_client.state.user
    project = await _create_project(db_session, user)
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.text
    # Tab button present
    assert "tab === &#39;metadata&#39;" in body or "tab === 'metadata'" in body
    # Preset section rendered
    assert "Available Presets" in body
    # Custom schemas section rendered
    assert "Custom Schemas" in body
    # Alpine component wired up
    assert "projectMetadataSettings" in body


# ---------------------------------------------------------------------------
# Issue form metadata (SPECIVO-104)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_form_includes_metadata_schemas(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET issue create form for project with schemas includes metadataFieldRenderer."""
    from tests.factories.lookups import StatusFactory, TrackerFactory
    from tests.factories.metadata_schema import MetadataSchemaFactory

    user = admin_client.state.user
    project = await _create_project(db_session, user, key="MF1", identifier="meta-form-1")

    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(tracker)
    await db_session.flush()

    schema = MetadataSchemaFactory.build(
        project_id=project.id,
        name="Bug Fields",
        schema_definition={
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["low", "high"]}},
        },
    )
    db_session.add(schema)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{project.key}/issues/new/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "metadataFieldRenderer" in body
    assert "Bug Fields" in body


@pytest.mark.integration
async def test_create_form_no_schemas(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET issue create form for project without schemas has no metadata section."""
    from tests.factories.lookups import StatusFactory, TrackerFactory

    user = admin_client.state.user
    project = await _create_project(db_session, user, key="MF2", identifier="meta-form-2")

    status = StatusFactory.build(name="Open", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add(tracker)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{project.key}/issues/new/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    assert "metadataFieldRenderer" not in resp.text


# ---------------------------------------------------------------------------
# Issue detail metadata panel (SPECIVO-105)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_detail_includes_metadata_panel(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET issue detail for issue with metadata shows issueMetadataPanel."""
    from specivo.schemas.issue import IssueCreate
    from specivo.services.issue_service import IssueService
    from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
    from tests.factories.metadata_schema import MetadataSchemaFactory

    user = admin_client.state.user
    project = await _create_project(db_session, user, key="MD1", identifier="meta-detail-1")

    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(priority)
    await db_session.flush()

    schema = MetadataSchemaFactory.build(
        project_id=project.id,
        name="Bug Triage",
        schema_definition={
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["low", "high"]}},
        },
    )
    db_session.add(schema)
    await db_session.flush()

    svc = IssueService()
    issue_data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Test metadata detail",
        metadata={"severity": "high"},
    )
    issue = await svc.create(db_session, project, issue_data, user)
    await db_session.commit()
    await db_session.refresh(issue)

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/issue/{issue.display_key}/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "issueMetadataPanel" in body
    assert "sp-metadata-panel" in body
    assert "Bug Triage" in body


@pytest.mark.integration
async def test_detail_no_metadata_panel_without_schemas(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET issue detail for project without schemas has no metadata panel."""
    from specivo.schemas.issue import IssueCreate
    from specivo.services.issue_service import IssueService
    from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory

    user = admin_client.state.user
    project = await _create_project(db_session, user, key="MD2", identifier="meta-detail-2")

    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Task", default_status_id=status.id)
    db_session.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(priority)
    await db_session.flush()

    svc = IssueService()
    issue_data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="No metadata issue",
    )
    issue = await svc.create(db_session, project, issue_data, user)
    await db_session.commit()
    await db_session.refresh(issue)

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/issue/{issue.display_key}/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    assert "issueMetadataPanel" not in resp.text


@pytest.mark.integration
async def test_edit_form_includes_metadata_with_existing_values(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET issue edit form includes metadata schemas and existing issue metadata."""
    from specivo.schemas.issue import IssueCreate
    from specivo.services.issue_service import IssueService
    from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
    from tests.factories.metadata_schema import MetadataSchemaFactory

    user = admin_client.state.user
    project = await _create_project(db_session, user, key="MF3", identifier="meta-form-3")

    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Feature", default_status_id=status.id)
    db_session.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(priority)
    await db_session.flush()

    schema = MetadataSchemaFactory.build(
        project_id=project.id,
        name="Sprint Planning",
        schema_definition={
            "type": "object",
            "properties": {"story_points": {"type": "integer", "minimum": 1, "maximum": 100}},
        },
    )
    db_session.add(schema)
    await db_session.flush()

    svc = IssueService()
    issue_data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Metadata edit test",
        metadata={"story_points": 5},
    )
    issue = await svc.create(db_session, project, issue_data, user)
    await db_session.commit()
    await db_session.refresh(issue)

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/issue/{issue.display_key}/edit/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "metadataFieldRenderer" in body
    assert "Sprint Planning" in body
    # Existing metadata values should be in the template
    assert "story_points" in body
