"""Integration tests for metadata presets — list, enable, disable, discover."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.metadata_preset import MetadataPreset
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.project import Project
from specivo.services.metadata_preset_service import MetadataPresetService
from tests.factories.project import ProjectFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MPRE", name="Metadata Preset Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def preset(db_session: AsyncSession) -> MetadataPreset:
    """Seed a single built-in preset for testing."""
    p = MetadataPreset(
        slug="test-dev",
        name="Test Development",
        description="Test preset",
        icon="code",
        is_builtin=True,
        schema_definition={
            "type": "object",
            "properties": {
                "component": {"type": "string", "description": "Module"},
                "commits": {"type": "array", "items": {"type": "string"}},
            },
        },
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def preset2(db_session: AsyncSession) -> MetadataPreset:
    """Second preset for multi-preset tests."""
    p = MetadataPreset(
        slug="test-bug",
        name="Test Bug Triage",
        description="Bug preset",
        icon="bug",
        is_builtin=True,
        schema_definition={
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
            },
        },
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestMetadataPresetService:
    async def test_list_presets(self, db_session: AsyncSession, preset: MetadataPreset):
        svc = MetadataPresetService()
        presets = await svc.list_presets(db_session)
        slugs = [p.slug for p in presets]
        assert "test-dev" in slugs

    async def test_enable_creates_schema(
        self,
        db_session: AsyncSession,
        preset: MetadataPreset,
        project: Project,
    ):
        svc = MetadataPresetService()
        schema = await svc.enable(db_session, project.id, "test-dev")
        assert schema.project_id == project.id
        assert schema.preset_slug == "test-dev"
        assert schema.name == "Test Development"
        assert "component" in schema.schema_definition["properties"]

    async def test_enable_duplicate_raises_conflict(
        self,
        db_session: AsyncSession,
        preset: MetadataPreset,
        project: Project,
    ):
        from specivo.core.exceptions import AppError

        svc = MetadataPresetService()
        await svc.enable(db_session, project.id, "test-dev")
        with pytest.raises(AppError, match="already enabled"):
            await svc.enable(db_session, project.id, "test-dev")

    async def test_enable_with_tracker_id(
        self,
        db_session: AsyncSession,
        preset: MetadataPreset,
        project: Project,
    ):
        from tests.factories.lookups import StatusFactory, TrackerFactory

        status = StatusFactory.build(name="PresetTestNew", position=1, category="backlog")
        db_session.add(status)
        await db_session.flush()
        tracker = TrackerFactory.build(name="PresetTestBug", default_status_id=status.id)
        db_session.add(tracker)
        await db_session.flush()

        svc = MetadataPresetService()
        schema = await svc.enable(db_session, project.id, "test-dev", tracker_id=tracker.id)
        assert schema.tracker_id == tracker.id

    async def test_disable_removes_schema(
        self,
        db_session: AsyncSession,
        preset: MetadataPreset,
        project: Project,
    ):
        svc = MetadataPresetService()
        await svc.enable(db_session, project.id, "test-dev")
        await svc.disable(db_session, project.id, "test-dev")

        result = await db_session.execute(
            select(MetadataSchema).where(
                MetadataSchema.project_id == project.id,
                MetadataSchema.preset_slug == "test-dev",
            )
        )
        assert result.scalar_one_or_none() is None

    async def test_disable_not_enabled_raises(
        self,
        db_session: AsyncSession,
        preset: MetadataPreset,
        project: Project,
    ):
        from specivo.core.exceptions import NotFoundError

        svc = MetadataPresetService()
        with pytest.raises(NotFoundError, match="not enabled"):
            await svc.disable(db_session, project.id, "test-dev")

    async def test_list_enabled(
        self,
        db_session: AsyncSession,
        preset: MetadataPreset,
        preset2: MetadataPreset,
        project: Project,
    ):
        svc = MetadataPresetService()
        await svc.enable(db_session, project.id, "test-dev")
        await svc.enable(db_session, project.id, "test-bug")
        enabled = await svc.list_enabled(db_session, project.id)
        assert set(enabled) == {"test-dev", "test-bug"}


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------


class TestMetadataPresetAPI:
    async def test_list_presets_requires_admin(
        self,
        client: AsyncClient,
        preset: MetadataPreset,
    ):
        resp = await client.get("/api/v1/admin/metadata-presets/")
        assert resp.status_code == 401

    async def test_list_presets_as_admin(
        self,
        admin_client: AsyncClient,
        preset: MetadataPreset,
    ):
        resp = await admin_client.get("/api/v1/admin/metadata-presets/")
        assert resp.status_code == 200
        slugs = [p["slug"] for p in resp.json()]
        assert "test-dev" in slugs

    async def test_get_preset(
        self,
        admin_client: AsyncClient,
        preset: MetadataPreset,
    ):
        resp = await admin_client.get("/api/v1/admin/metadata-presets/test-dev/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "test-dev"
        assert "component" in data["schema_definition"]["properties"]

    async def test_enable_preset(
        self,
        admin_client: AsyncClient,
        preset: MetadataPreset,
        project: Project,
    ):
        resp = await admin_client.post(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/test-dev/enable/",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["preset_slug"] == "test-dev"
        assert data["project_id"] == project.id

    async def test_disable_preset(
        self,
        admin_client: AsyncClient,
        preset: MetadataPreset,
        project: Project,
    ):
        # Enable first
        await admin_client.post(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/test-dev/enable/",
        )
        # Disable
        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/test-dev/disable/",
        )
        assert resp.status_code == 204

    async def test_list_enabled_presets(
        self,
        admin_client: AsyncClient,
        preset: MetadataPreset,
        project: Project,
    ):
        await admin_client.post(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/test-dev/enable/",
        )
        resp = await admin_client.get(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/",
        )
        assert resp.status_code == 200
        assert "test-dev" in resp.json()


# ---------------------------------------------------------------------------
# Schema discovery tests
# ---------------------------------------------------------------------------


class TestSchemaDiscovery:
    async def test_discover_returns_enabled_schemas(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        preset: MetadataPreset,
        project: Project,
    ):
        """Authenticated user can discover schemas for a project."""
        svc = MetadataPresetService()
        await svc.enable(db_session, project.id, "test-dev")

        resp = await auth_client.get(f"/api/v1/projects/{project.key}/metadata-schemas/")
        assert resp.status_code == 200
        schemas = resp.json()
        assert len(schemas) >= 1
        assert schemas[0]["preset_slug"] == "test-dev"

    async def test_discover_empty_project(
        self,
        auth_client: AsyncClient,
        project: Project,
    ):
        """No schemas returns empty list."""
        resp = await auth_client.get(f"/api/v1/projects/{project.key}/metadata-schemas/")
        assert resp.status_code == 200
        assert resp.json() == []
