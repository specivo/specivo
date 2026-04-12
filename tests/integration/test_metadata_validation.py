"""Integration tests for metadata schema discovery and preset deletion guards.

Covers tracker_id filtering on the discovery endpoint and preset
deletion with active schema references.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.metadata_preset import MetadataPreset
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.project import Project
from tests.factories.lookups import StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MVAL", name="Metadata Validation Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession):
    status = StatusFactory.build(name="ValNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    t = TrackerFactory.build(name="ValBug", default_status_id=status.id)
    db_session.add(t)
    await db_session.flush()
    return t


@pytest_asyncio.fixture
async def project_wide_schema(
    db_session: AsyncSession,
    project: Project,
) -> MetadataSchema:
    """Schema with no tracker_id (project-wide)."""
    s = MetadataSchema(
        project_id=project.id,
        tracker_id=None,
        name="project-wide-schema",
        description="Applies to all trackers",
        schema_definition={
            "type": "object",
            "properties": {"environment": {"type": "string"}},
        },
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker_schema(
    db_session: AsyncSession,
    project: Project,
    tracker,
) -> MetadataSchema:
    """Schema scoped to a specific tracker."""
    s = MetadataSchema(
        project_id=project.id,
        tracker_id=tracker.id,
        name="tracker-specific-schema",
        description="Only for ValBug tracker",
        schema_definition={
            "type": "object",
            "properties": {"steps_to_reproduce": {"type": "string"}},
        },
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Schema discovery with tracker_id filter
# ---------------------------------------------------------------------------


class TestSchemaDiscoveryWithTrackerFilter:
    """Verify the tracker_id query parameter on the discovery endpoint."""

    async def test_no_filter_returns_all_schemas(
        self,
        auth_client: AsyncClient,
        project: Project,
        project_wide_schema: MetadataSchema,
        tracker_schema: MetadataSchema,
    ):
        """GET without tracker_id returns all schemas."""
        resp = await auth_client.get(
            f"/api/v1/projects/{project.key}/metadata-schemas/"
        )
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert "project-wide-schema" in names
        assert "tracker-specific-schema" in names

    async def test_filter_by_tracker_returns_matching_and_project_wide(
        self,
        auth_client: AsyncClient,
        project: Project,
        project_wide_schema: MetadataSchema,
        tracker_schema: MetadataSchema,
        tracker,
    ):
        """GET with tracker_id returns both project-wide and tracker-specific."""
        resp = await auth_client.get(
            f"/api/v1/projects/{project.key}/metadata-schemas/",
            params={"tracker_id": tracker.id},
        )
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert "project-wide-schema" in names
        assert "tracker-specific-schema" in names

    async def test_filter_by_other_tracker_excludes_tracker_specific(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        project_wide_schema: MetadataSchema,
        tracker_schema: MetadataSchema,
    ):
        """GET with a different tracker_id excludes tracker-specific schemas."""
        # Create a second tracker
        status = StatusFactory.build(name="ValOpen", position=2, category="backlog")
        db_session.add(status)
        await db_session.flush()
        other_tracker = TrackerFactory.build(name="ValFeature", default_status_id=status.id)
        db_session.add(other_tracker)
        await db_session.flush()

        resp = await auth_client.get(
            f"/api/v1/projects/{project.key}/metadata-schemas/",
            params={"tracker_id": other_tracker.id},
        )
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert "project-wide-schema" in names
        assert "tracker-specific-schema" not in names


# ---------------------------------------------------------------------------
# content_type filtering (service-level)
# ---------------------------------------------------------------------------


class TestSchemaContentTypeFilter:
    """Verify list_for_project filters by content_type and validate_metadata
    only considers schemas of the matching content type."""

    async def test_list_for_project_filters_by_content_type(
        self,
        db_session: AsyncSession,
        project: Project,
        project_wide_schema: MetadataSchema,
    ):
        from specivo.services.metadata_schema_service import MetadataSchemaService

        # Existing schema defaults to content_type='issue'
        svc = MetadataSchemaService()
        issue_schemas = await svc.list_for_project(db_session, project.id, content_type="issue")
        assert any(s.name == "project-wide-schema" for s in issue_schemas)

        wiki_schemas = await svc.list_for_project(db_session, project.id, content_type="wiki")
        assert all(s.name != "project-wide-schema" for s in wiki_schemas)

    async def test_list_for_project_no_filter_returns_all(
        self,
        db_session: AsyncSession,
        project: Project,
        project_wide_schema: MetadataSchema,
    ):
        from specivo.services.metadata_schema_service import MetadataSchemaService

        svc = MetadataSchemaService()
        all_schemas = await svc.list_for_project(db_session, project.id)
        assert any(s.name == "project-wide-schema" for s in all_schemas)

    async def test_schema_defaults_to_issue_content_type(
        self,
        project_wide_schema: MetadataSchema,
    ):
        assert project_wide_schema.content_type == "issue"


# ---------------------------------------------------------------------------
# Delete preset that is enabled on a project
# ---------------------------------------------------------------------------


class TestDeletePresetWithActiveProject:
    """Ensure preset deletion is blocked when a project has it enabled."""

    @pytest_asyncio.fixture
    async def custom_preset(self, db_session: AsyncSession) -> MetadataPreset:
        p = MetadataPreset(
            slug="val-custom",
            name="Validation Custom",
            description="For validation tests",
            icon="check",
            is_builtin=False,
            schema_definition={
                "type": "object",
                "properties": {"val_field": {"type": "string"}},
            },
        )
        db_session.add(p)
        await db_session.commit()
        await db_session.refresh(p)
        return p

    async def test_delete_returns_409_when_enabled(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        custom_preset: MetadataPreset,
    ):
        """DELETE preset returns 409 when a project has it enabled."""
        schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name=custom_preset.name,
            schema_definition=custom_preset.schema_definition,
            preset_slug=custom_preset.slug,
        )
        db_session.add(schema)
        await db_session.commit()

        resp = await admin_client.delete(
            f"/api/v1/admin/metadata-presets/{custom_preset.slug}/"
        )
        assert resp.status_code == 409

    async def test_disable_then_delete_succeeds(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        custom_preset: MetadataPreset,
    ):
        """Disable preset from project first, then DELETE succeeds with 204."""
        schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name=custom_preset.name,
            schema_definition=custom_preset.schema_definition,
            preset_slug=custom_preset.slug,
        )
        db_session.add(schema)
        await db_session.commit()

        # Disable first
        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/{custom_preset.slug}/disable/"
        )
        assert resp.status_code == 204

        # Now delete the preset itself
        resp = await admin_client.delete(
            f"/api/v1/admin/metadata-presets/{custom_preset.slug}/"
        )
        assert resp.status_code == 204
