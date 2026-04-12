"""Integration tests for metadata lifecycle — safe deletion and usage counting.

Covers SPECIVO-90 (safe deletion with usage counting) and
SPECIVO-92 (usage endpoint + safe delete API).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.models.issue import Issue
from specivo.models.metadata_preset import MetadataPreset
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.project import Project
from specivo.services.metadata_schema_service import MetadataSchemaService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession):
    """Create minimal lookup records needed for issues."""
    status = StatusFactory.build(name="LifecycleNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()

    tracker = TrackerFactory.build(name="LifecycleTask", default_status_id=status.id)
    db_session.add(tracker)
    await db_session.flush()

    priority = PriorityFactory.build(name="LifecycleNormal", position=1, is_default=True)
    db_session.add(priority)
    await db_session.flush()

    return {"status": status, "tracker": tracker, "priority": priority}


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MLIF", name="Metadata Lifecycle Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def author(db_session: AsyncSession):
    user = UserFactory.build(login="lifecycle_author", email="lifecycle@test.local")
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def schema(db_session: AsyncSession, project: Project) -> MetadataSchema:
    """A metadata schema with 'component' and 'severity' properties."""
    s = MetadataSchema(
        project_id=project.id,
        tracker_id=None,
        name="lifecycle-test-schema",
        description="Schema for lifecycle tests",
        schema_definition={
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
            },
        },
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def issue_with_metadata(
    db_session: AsyncSession,
    project: Project,
    lookups: dict,
    author,
) -> Issue:
    """An issue that has metadata matching the schema's property keys."""
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        tracker_id=lookups["tracker"].id,
        status_id=lookups["status"].id,
        priority_id=lookups["priority"].id,
        author_id=author.id,
        subject="Issue with metadata",
    )
    issue.issue_metadata = {"component": "backend", "severity": "major"}
    db_session.add(issue)
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


@pytest_asyncio.fixture
async def issue_without_metadata(
    db_session: AsyncSession,
    project: Project,
    lookups: dict,
    author,
) -> Issue:
    """An issue with empty metadata."""
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=99,
        tracker_id=lookups["tracker"].id,
        status_id=lookups["status"].id,
        priority_id=lookups["priority"].id,
        author_id=author.id,
        subject="Issue without metadata",
    )
    issue.issue_metadata = {}
    db_session.add(issue)
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# Service-level tests — count_usages
# ---------------------------------------------------------------------------


class TestCountUsages:
    async def test_returns_zero_when_no_issues_have_metadata(
        self,
        db_session: AsyncSession,
        schema: MetadataSchema,
        issue_without_metadata: Issue,
    ):
        """Schema with no matching issue data returns count 0."""
        svc = MetadataSchemaService()
        count = await svc.count_usages(db_session, schema)
        assert count == 0

    async def test_returns_count_when_issues_have_matching_keys(
        self,
        db_session: AsyncSession,
        schema: MetadataSchema,
        issue_with_metadata: Issue,
    ):
        """Schema with matching issue metadata returns correct count."""
        svc = MetadataSchemaService()
        count = await svc.count_usages(db_session, schema)
        assert count == 1

    async def test_counts_only_issues_in_same_project(
        self,
        db_session: AsyncSession,
        schema: MetadataSchema,
        issue_with_metadata: Issue,
        lookups: dict,
        author,
    ):
        """Issues in other projects are not counted."""
        other_proj = ProjectFactory.build(key="OTHR", name="Other Project", is_public=True)
        db_session.add(other_proj)
        await db_session.flush()

        other_issue = IssueFactory.build(
            project_id=other_proj.id,
            project_key=other_proj.key,
            tracker_id=lookups["tracker"].id,
            status_id=lookups["status"].id,
            priority_id=lookups["priority"].id,
            author_id=author.id,
            subject="Other project issue",
        )
        other_issue.issue_metadata = {"component": "frontend"}
        db_session.add(other_issue)
        await db_session.flush()

        svc = MetadataSchemaService()
        count = await svc.count_usages(db_session, schema)
        # Only the issue in our project should be counted
        assert count == 1

    async def test_returns_zero_for_schema_with_no_properties(
        self,
        db_session: AsyncSession,
        project: Project,
        issue_with_metadata: Issue,
    ):
        """Schema with empty properties dict returns 0."""
        empty_schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name="empty-schema",
            schema_definition={"type": "object", "properties": {}},
        )
        db_session.add(empty_schema)
        await db_session.flush()

        svc = MetadataSchemaService()
        count = await svc.count_usages(db_session, empty_schema)
        assert count == 0

    async def test_partial_key_match_counts(
        self,
        db_session: AsyncSession,
        project: Project,
        issue_with_metadata: Issue,
    ):
        """Issue with only one matching key still counts as usage."""
        partial_schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name="partial-schema",
            schema_definition={
                "type": "object",
                "properties": {
                    "component": {"type": "string"},
                    "nonexistent_field": {"type": "string"},
                },
            },
        )
        db_session.add(partial_schema)
        await db_session.flush()

        svc = MetadataSchemaService()
        count = await svc.count_usages(db_session, partial_schema)
        assert count == 1


# ---------------------------------------------------------------------------
# Service-level tests — delete_safe
# ---------------------------------------------------------------------------


class TestDeleteSafe:
    async def test_deletes_when_no_usage(
        self,
        db_session: AsyncSession,
        schema: MetadataSchema,
        issue_without_metadata: Issue,
    ):
        """Schema with no usage can be deleted."""
        svc = MetadataSchemaService()
        schema_id = schema.id
        await svc.delete_safe(db_session, schema)
        await db_session.commit()

        # Verify schema is gone
        from sqlalchemy import select

        result = await db_session.execute(
            select(MetadataSchema).where(MetadataSchema.id == schema_id)
        )
        assert result.scalar_one_or_none() is None

    async def test_raises_conflict_when_in_use(
        self,
        db_session: AsyncSession,
        schema: MetadataSchema,
        issue_with_metadata: Issue,
    ):
        """Schema with in-use data cannot be deleted — raises 409."""
        svc = MetadataSchemaService()
        with pytest.raises(AppError, match="Cannot delete") as exc_info:
            await svc.delete_safe(db_session, schema)
        assert exc_info.value.status_code == 409

    async def test_conflict_message_includes_count(
        self,
        db_session: AsyncSession,
        schema: MetadataSchema,
        issue_with_metadata: Issue,
    ):
        """The conflict error message includes the usage count."""
        svc = MetadataSchemaService()
        with pytest.raises(AppError) as exc_info:
            await svc.delete_safe(db_session, schema)
        assert "1" in exc_info.value.message


# ---------------------------------------------------------------------------
# API-level tests — usage endpoint
# ---------------------------------------------------------------------------


class TestUsageEndpoint:
    async def test_returns_usage_count(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema: MetadataSchema,
        issue_with_metadata: Issue,
    ):
        """GET .../usage/ returns the correct count."""
        resp = await admin_client.get(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema.id}/usage/"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema_id"] == schema.id
        assert data["name"] == schema.name
        assert data["usage_count"] == 1

    async def test_returns_zero_for_unused_schema(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema: MetadataSchema,
    ):
        """GET .../usage/ returns 0 when no issues use the schema."""
        resp = await admin_client.get(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema.id}/usage/"
        )
        assert resp.status_code == 200
        assert resp.json()["usage_count"] == 0

    async def test_requires_admin(
        self,
        client: AsyncClient,
        project: Project,
        schema: MetadataSchema,
    ):
        """Unauthenticated request returns 401."""
        resp = await client.get(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema.id}/usage/"
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API-level tests — safe delete
# ---------------------------------------------------------------------------


class TestSafeDeleteEndpoint:
    async def test_delete_succeeds_when_no_usage(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema: MetadataSchema,
    ):
        """DELETE returns 204 when schema has no usage."""
        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema.id}/"
        )
        assert resp.status_code == 204

    async def test_delete_returns_409_when_in_use(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema: MetadataSchema,
        issue_with_metadata: Issue,
    ):
        """DELETE returns 409 when issues use the schema's metadata keys."""
        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema.id}/"
        )
        assert resp.status_code == 409
        data = resp.json()
        assert any("Cannot delete" in e["message"] for e in data["errors"])


# ---------------------------------------------------------------------------
# API-level tests — safe disable preset
# ---------------------------------------------------------------------------


class TestSafeDisableEndpoint:
    async def test_disable_returns_409_when_in_use(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        issue_with_metadata: Issue,
    ):
        """Disabling a preset fails if its schemas have in-use data."""
        # Create a preset and enable it
        preset = MetadataPreset(
            slug="lifecycle-preset",
            name="Lifecycle Preset",
            description="For disable test",
            icon="test",
            is_builtin=False,
            schema_definition={
                "type": "object",
                "properties": {
                    "component": {"type": "string"},
                },
            },
        )
        db_session.add(preset)
        await db_session.flush()

        # Enable it on the project
        schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name="Lifecycle Preset",
            description="For disable test",
            schema_definition=preset.schema_definition,
            preset_slug="lifecycle-preset",
        )
        db_session.add(schema)
        await db_session.commit()

        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/lifecycle-preset/disable/"
        )
        assert resp.status_code == 409

    async def test_disable_succeeds_when_no_usage(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
    ):
        """Disabling a preset succeeds when no issues use its schemas."""
        preset = MetadataPreset(
            slug="unused-preset",
            name="Unused Preset",
            description="No issues use this",
            icon="test",
            is_builtin=False,
            schema_definition={
                "type": "object",
                "properties": {"unused_field": {"type": "string"}},
            },
        )
        db_session.add(preset)
        await db_session.flush()

        schema = MetadataSchema(
            project_id=project.id,
            tracker_id=None,
            name="Unused Preset",
            schema_definition=preset.schema_definition,
            preset_slug="unused-preset",
        )
        db_session.add(schema)
        await db_session.commit()

        resp = await admin_client.delete(
            f"/api/v1/admin/projects/{project.key}/metadata-presets/unused-preset/disable/"
        )
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# API-level tests — PATCH clears nullable fields to None
# ---------------------------------------------------------------------------


class TestUpdateClearsNullableFields:
    """Verify that PATCH with explicit null values clears fields correctly."""

    @pytest_asyncio.fixture
    async def schema_with_tracker(
        self,
        db_session: AsyncSession,
        project: Project,
        lookups: dict,
    ) -> MetadataSchema:
        """A schema scoped to a specific tracker."""
        s = MetadataSchema(
            project_id=project.id,
            tracker_id=lookups["tracker"].id,
            name="tracker-scoped-schema",
            description="Has a description",
            schema_definition={
                "type": "object",
                "properties": {"field_a": {"type": "string"}},
            },
        )
        db_session.add(s)
        await db_session.commit()
        await db_session.refresh(s)
        return s

    async def test_patch_tracker_id_null_clears_to_none(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema_with_tracker: MetadataSchema,
        lookups: dict,
    ):
        """PATCH with tracker_id=null makes the schema project-wide."""
        assert schema_with_tracker.tracker_id == lookups["tracker"].id
        resp = await admin_client.patch(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema_with_tracker.id}/",
            json={"tracker_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["tracker_id"] is None

    async def test_patch_description_null_clears_to_none(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema_with_tracker: MetadataSchema,
    ):
        """PATCH with description=null clears the description."""
        assert schema_with_tracker.description == "Has a description"
        resp = await admin_client.patch(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema_with_tracker.id}/",
            json={"description": None},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] is None

    async def test_patch_name_only_does_not_touch_other_fields(
        self,
        admin_client: AsyncClient,
        project: Project,
        schema_with_tracker: MetadataSchema,
        lookups: dict,
    ):
        """PATCH with only name should leave tracker_id and description unchanged."""
        resp = await admin_client.patch(
            f"/api/v1/admin/projects/{project.key}/metadata-schemas/{schema_with_tracker.id}/",
            json={"name": "renamed-schema"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "renamed-schema"
        assert data["tracker_id"] == lookups["tracker"].id
        assert data["description"] == "Has a description"
