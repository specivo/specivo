"""Integration tests for custom metadata preset CRUD and builtin guard.

Covers SPECIVO-91 (custom preset CRUD with builtin guard) and
SPECIVO-92 (API endpoints for preset create/update/delete).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
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
async def builtin_preset(db_session: AsyncSession) -> MetadataPreset:
    """A builtin preset that cannot be deleted."""
    p = MetadataPreset(
        slug="builtin-test",
        name="Builtin Test",
        description="Built-in preset for tests",
        icon="lock",
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


@pytest_asyncio.fixture
async def custom_preset(db_session: AsyncSession) -> MetadataPreset:
    """A custom (non-builtin) preset that can be deleted."""
    p = MetadataPreset(
        slug="custom-test",
        name="Custom Test",
        description="Custom preset for tests",
        icon="star",
        is_builtin=False,
        schema_definition={
            "type": "object",
            "properties": {
                "priority_tag": {"type": "string"},
            },
        },
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Service-level tests — create_custom
# ---------------------------------------------------------------------------


class TestCreateCustomPreset:
    async def test_creates_with_is_builtin_false(self, db_session: AsyncSession):
        """Custom presets are always created with is_builtin=False."""
        svc = MetadataPresetService()
        preset = await svc.create_custom(
            db_session,
            slug="new-custom",
            name="New Custom",
            description="A new custom preset",
            icon="zap",
            schema_definition={
                "type": "object",
                "properties": {"field_a": {"type": "string"}},
            },
        )
        assert preset.is_builtin is False
        assert preset.slug == "new-custom"
        assert preset.name == "New Custom"

    async def test_validates_json_schema(self, db_session: AsyncSession):
        """Invalid JSON Schema definition is rejected."""
        svc = MetadataPresetService()
        with pytest.raises(AppError) as exc_info:
            await svc.create_custom(
                db_session,
                slug="bad-schema",
                name="Bad Schema",
                description=None,
                icon="x",
                schema_definition={"type": "not_a_valid_type"},
            )
        assert exc_info.value.status_code == 422

    async def test_rejects_duplicate_slug(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """Duplicate slug raises 409 conflict."""
        svc = MetadataPresetService()
        with pytest.raises(AppError, match="already exists") as exc_info:
            await svc.create_custom(
                db_session,
                slug="custom-test",  # same as custom_preset
                name="Another name",
                description=None,
                icon="x",
                schema_definition={"type": "object"},
            )
        assert exc_info.value.status_code == 409

    async def test_rejects_case_insensitive_duplicate_slug(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """A slug differing only by case is rejected (case-insensitive uniqueness)."""
        svc = MetadataPresetService()
        with pytest.raises(AppError, match="already exists") as exc_info:
            await svc.create_custom(
                db_session,
                slug="Custom-Test",  # same as custom_preset modulo case
                name="Another name",
                description=None,
                icon="x",
                schema_definition={"type": "object"},
            )
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Service-level tests — update_preset
# ---------------------------------------------------------------------------


class TestUpdatePreset:
    async def test_updates_name_and_description(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """Partial update of name and description works."""
        svc = MetadataPresetService()
        updated = await svc.update_preset(
            db_session,
            custom_preset,
            name="Updated Name",
            description="Updated description",
        )
        assert updated.name == "Updated Name"
        assert updated.description == "Updated description"

    async def test_updates_schema_definition(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """Schema definition can be updated."""
        svc = MetadataPresetService()
        new_schema = {
            "type": "object",
            "properties": {"new_field": {"type": "integer"}},
        }
        updated = await svc.update_preset(
            db_session,
            custom_preset,
            schema_definition=new_schema,
        )
        assert "new_field" in updated.schema_definition["properties"]

    async def test_rejects_invalid_schema_definition(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """Invalid JSON Schema on update is rejected."""
        svc = MetadataPresetService()
        with pytest.raises(AppError) as exc_info:
            await svc.update_preset(
                db_session,
                custom_preset,
                schema_definition={"type": "bogus"},
            )
        assert exc_info.value.status_code == 422

    async def test_cannot_change_slug_on_builtin(
        self,
        db_session: AsyncSession,
        builtin_preset: MetadataPreset,
    ):
        """Builtin presets cannot have their slug changed."""
        svc = MetadataPresetService()
        with pytest.raises(AppError) as exc_info:
            await svc.update_preset(
                db_session,
                builtin_preset,
                slug="new-slug",
            )
        assert exc_info.value.status_code == 403

    async def test_rejects_case_insensitive_duplicate_slug_on_update(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """Updating a custom preset's slug to collide (case-insensitively) is rejected."""
        svc = MetadataPresetService()
        other = await svc.create_custom(
            db_session,
            slug="other-custom",
            name="Other Custom",
            description=None,
            icon="x",
            schema_definition={"type": "object"},
        )
        with pytest.raises(AppError, match="already exists") as exc_info:
            await svc.update_preset(db_session, other, slug="CUSTOM-TEST")
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Service-level tests — delete_preset
# ---------------------------------------------------------------------------


class TestDeletePreset:
    async def test_deletes_custom_preset(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
    ):
        """Custom presets can be deleted."""
        svc = MetadataPresetService()
        preset_id = custom_preset.id
        await svc.delete_preset(db_session, custom_preset)
        await db_session.commit()

        result = await db_session.execute(
            select(MetadataPreset).where(MetadataPreset.id == preset_id)
        )
        assert result.scalar_one_or_none() is None

    async def test_raises_403_for_builtin(
        self,
        db_session: AsyncSession,
        builtin_preset: MetadataPreset,
    ):
        """Builtin presets cannot be deleted — raises 403."""
        svc = MetadataPresetService()
        with pytest.raises(AppError) as exc_info:
            await svc.delete_preset(db_session, builtin_preset)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# API-level tests — preset CRUD endpoints
# ---------------------------------------------------------------------------


class TestPresetCRUDAPI:
    async def test_create_preset(self, admin_client: AsyncClient):
        """POST /admin/metadata-presets/ creates a custom preset."""
        resp = await admin_client.post(
            "/api/v1/admin/metadata-presets/",
            json={
                "slug": "api-created",
                "name": "API Created Preset",
                "description": "Created via API",
                "icon": "api",
                "schema_definition": {
                    "type": "object",
                    "properties": {"api_field": {"type": "string"}},
                },
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == "api-created"
        assert data["is_builtin"] is False

    async def test_create_preset_rejects_invalid_schema(self, admin_client: AsyncClient):
        """POST rejects invalid JSON Schema."""
        resp = await admin_client.post(
            "/api/v1/admin/metadata-presets/",
            json={
                "slug": "bad-api",
                "name": "Bad API Preset",
                "icon": "x",
                "schema_definition": {"type": "invalid_type"},
            },
        )
        assert resp.status_code == 422

    async def test_create_preset_rejects_duplicate_slug(
        self,
        admin_client: AsyncClient,
        custom_preset: MetadataPreset,
    ):
        """POST rejects duplicate slug with 409."""
        resp = await admin_client.post(
            "/api/v1/admin/metadata-presets/",
            json={
                "slug": "custom-test",
                "name": "Duplicate",
                "icon": "x",
                "schema_definition": {"type": "object"},
            },
        )
        assert resp.status_code == 409

    async def test_create_preset_normalizes_slug(self, admin_client: AsyncClient):
        """Slug is normalized to lowercase/dashes on create."""
        resp = await admin_client.post(
            "/api/v1/admin/metadata-presets/",
            json={
                "slug": "My Cool Fields",
                "name": "My Cool Fields",
                "icon": "star",
                "schema_definition": {"type": "object", "properties": {"f": {"type": "string"}}},
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["slug"] == "my-cool-fields"

    async def test_create_preset_rejects_case_insensitive_duplicate_slug(
        self,
        admin_client: AsyncClient,
        custom_preset: MetadataPreset,
    ):
        """POST rejects a slug that duplicates an existing one case-insensitively."""
        resp = await admin_client.post(
            "/api/v1/admin/metadata-presets/",
            json={
                "slug": "CUSTOM-TEST",
                "name": "Duplicate",
                "icon": "x",
                "schema_definition": {"type": "object"},
            },
        )
        assert resp.status_code == 409

    async def test_create_preset_requires_admin(self, client: AsyncClient):
        """Unauthenticated request returns 401."""
        resp = await client.post(
            "/api/v1/admin/metadata-presets/",
            json={
                "slug": "unauth",
                "name": "Unauth",
                "icon": "x",
                "schema_definition": {"type": "object"},
            },
        )
        assert resp.status_code == 401

    async def test_update_preset(
        self,
        admin_client: AsyncClient,
        custom_preset: MetadataPreset,
    ):
        """PATCH /admin/metadata-presets/{slug}/ updates the preset."""
        resp = await admin_client.patch(
            f"/api/v1/admin/metadata-presets/{custom_preset.slug}/",
            json={"name": "Updated via API", "description": "New desc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Updated via API"
        assert data["description"] == "New desc"

    async def test_update_preset_not_found(self, admin_client: AsyncClient):
        """PATCH with unknown slug returns 404."""
        resp = await admin_client.patch(
            "/api/v1/admin/metadata-presets/nonexistent/",
            json={"name": "X"},
        )
        assert resp.status_code == 404

    async def test_delete_custom_preset(
        self,
        admin_client: AsyncClient,
        custom_preset: MetadataPreset,
    ):
        """DELETE /admin/metadata-presets/{slug}/ deletes custom presets."""
        resp = await admin_client.delete(
            f"/api/v1/admin/metadata-presets/{custom_preset.slug}/"
        )
        assert resp.status_code == 204

    async def test_delete_builtin_preset_returns_403(
        self,
        admin_client: AsyncClient,
        builtin_preset: MetadataPreset,
    ):
        """DELETE returns 403 for builtin presets."""
        resp = await admin_client.delete(
            f"/api/v1/admin/metadata-presets/{builtin_preset.slug}/"
        )
        assert resp.status_code == 403

    async def test_delete_preset_not_found(self, admin_client: AsyncClient):
        """DELETE with unknown slug returns 404."""
        resp = await admin_client.delete(
            "/api/v1/admin/metadata-presets/nonexistent/"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete preset blocked when schemas reference it
# ---------------------------------------------------------------------------


class TestDeletePresetWithActiveSchemas:
    """Verify that deleting a preset fails when projects have it enabled."""

    @pytest_asyncio.fixture
    async def project(self, db_session: AsyncSession) -> Project:
        proj = ProjectFactory.build(key="PCRD", name="Preset CRUD Test", is_public=True)
        db_session.add(proj)
        await db_session.commit()
        await db_session.refresh(proj)
        return proj

    async def test_delete_blocked_when_schemas_reference_preset(
        self,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
        project: Project,
    ):
        """Service: delete_preset raises 409 when a schema references it."""
        schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name=custom_preset.name,
            schema_definition=custom_preset.schema_definition,
            preset_slug=custom_preset.slug,
        )
        db_session.add(schema)
        await db_session.flush()

        svc = MetadataPresetService()
        with pytest.raises(AppError, match="Cannot delete") as exc_info:
            await svc.delete_preset(db_session, custom_preset)
        assert exc_info.value.status_code == 409

    async def test_delete_preset_returns_409_when_enabled(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
        project: Project,
    ):
        """API: DELETE preset returns 409 when a project has it enabled."""
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

    async def test_delete_succeeds_after_disabling_preset(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        custom_preset: MetadataPreset,
        project: Project,
    ):
        """API: DELETE preset succeeds after disabling it from the project."""
        # Enable preset on project
        schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name=custom_preset.name,
            schema_definition=custom_preset.schema_definition,
            preset_slug=custom_preset.slug,
        )
        db_session.add(schema)
        await db_session.commit()

        # Disable it first
        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/{custom_preset.slug}/disable/"
        )
        assert resp.status_code == 204

        # Now delete should succeed
        resp = await admin_client.delete(
            f"/api/v1/admin/metadata-presets/{custom_preset.slug}/"
        )
        assert resp.status_code == 204
