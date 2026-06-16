"""Integration tests for the metadata schema management MCP tools.

Covers create, update, delete, list round-trip plus permission enforcement
and security-audit event emission.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.models.lookups import Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from tests.factories.lookups import StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession):
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MSCH", name="Metadata Schema MCP", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="msch_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def viewer(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="msch_viewer", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def role_view_only(db_session: AsyncSession) -> Role:
    result = await db_session.execute(select(Role).where(Role.name == "MSCHViewOnly"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="MSCHViewOnly",
        position=21,
        permissions=["view_issues"],
        issues_visibility="default",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


async def _add_member(db, project, user, role):
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


def _basic_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["low", "med", "high"]},
        },
    }


async def _audit_count(db, event_type: str, project_id: int) -> int:
    result = await db.execute(
        select(SecurityAuditLog).where(
            SecurityAuditLog.event_type == event_type,
            SecurityAuditLog.project_id == project_id,
        )
    )
    return len(list(result.scalars().all()))


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestMetadataSchemaRoundTrip:
    async def test_create_list_update_delete(self, db_session, admin, project, tracker):
        from specivo.mcp.tools import (
            _create_metadata_schema,
            _delete_metadata_schema,
            _list_metadata_schemas,
            _update_metadata_schema,
        )

        # Create
        out = await _create_metadata_schema(
            db_session,
            admin,
            project.key,
            name="Bug Fields",
            schema=_basic_schema(),
            content_type="issue",
            tracker_id=tracker.id,
            description="schema for bugs",
        )
        assert "Created metadata schema" in out
        assert "Bug Fields" in out

        result = await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project.id))
        rows = list(result.scalars().all())
        assert len(rows) == 1
        schema_id = rows[0].id
        assert rows[0].description == "schema for bugs"

        # List shows the new schema with id
        listing = await _list_metadata_schemas(db_session, admin, project.key)
        assert f"id={schema_id}" in listing
        assert "Bug Fields" in listing

        # Update
        new_def = {
            "type": "object",
            "properties": {
                "severity": {"type": "string"},
                "owner": {"type": "string"},
            },
        }
        out = await _update_metadata_schema(
            db_session,
            admin,
            project.key,
            schema_id,
            name="Bug Fields v2",
            schema=new_def,
            description="updated",
        )
        assert "Updated" in out
        assert "name" in out and "schema_definition" in out and "description" in out

        listing = await _list_metadata_schemas(db_session, admin, project.key)
        assert "Bug Fields v2" in listing
        assert "owner" in listing

        # Delete
        out = await _delete_metadata_schema(db_session, admin, project.key, schema_id)
        assert "Deleted" in out

        listing = await _list_metadata_schemas(db_session, admin, project.key)
        assert "Bug Fields" not in listing


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestMetadataSchemaPermissions:
    async def test_create_denied_for_non_manager(self, db_session, viewer, project, role_view_only):
        from specivo.mcp.tools import _create_metadata_schema

        await _add_member(db_session, project, viewer, role_view_only)

        with pytest.raises(PermissionDeniedError):
            await _create_metadata_schema(
                db_session,
                viewer,
                project.key,
                name="Nope",
                schema=_basic_schema(),
            )

    async def test_update_denied_for_non_manager(self, db_session, admin, viewer, project, role_view_only):
        from specivo.mcp.tools import _create_metadata_schema, _update_metadata_schema

        await _create_metadata_schema(db_session, admin, project.key, name="X", schema=_basic_schema())
        result = await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project.id))
        sid = result.scalar_one().id

        await _add_member(db_session, project, viewer, role_view_only)

        with pytest.raises(PermissionDeniedError):
            await _update_metadata_schema(db_session, viewer, project.key, sid, name="hacked")

    async def test_delete_denied_for_non_manager(self, db_session, admin, viewer, project, role_view_only):
        from specivo.mcp.tools import _create_metadata_schema, _delete_metadata_schema

        await _create_metadata_schema(db_session, admin, project.key, name="Y", schema=_basic_schema())
        result = await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project.id))
        sid = result.scalar_one().id

        await _add_member(db_session, project, viewer, role_view_only)

        with pytest.raises(PermissionDeniedError):
            await _delete_metadata_schema(db_session, viewer, project.key, sid)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestMetadataSchemaAudit:
    async def test_create_emits_audit_event(self, db_session, admin, project):
        from specivo.mcp.tools import _create_metadata_schema

        before = await _audit_count(db_session, "metadata_schema_created", project.id)
        await _create_metadata_schema(db_session, admin, project.key, name="Audited", schema=_basic_schema())
        after = await _audit_count(db_session, "metadata_schema_created", project.id)
        assert after == before + 1

        # Audit row carries source=mcp and the schema name
        result = await db_session.execute(
            select(SecurityAuditLog)
            .where(SecurityAuditLog.event_type == "metadata_schema_created")
            .order_by(SecurityAuditLog.id.desc())
        )
        row = result.scalars().first()
        assert row is not None
        assert row.details.get("source") == "mcp"
        assert row.details.get("name") == "Audited"
        assert row.project_id == project.id

    async def test_update_emits_audit_event(self, db_session, admin, project):
        from specivo.mcp.tools import _create_metadata_schema, _update_metadata_schema

        await _create_metadata_schema(db_session, admin, project.key, name="U1", schema=_basic_schema())
        result = await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project.id))
        sid = result.scalar_one().id

        before = await _audit_count(db_session, "metadata_schema_updated", project.id)
        await _update_metadata_schema(db_session, admin, project.key, sid, name="U1-renamed")
        after = await _audit_count(db_session, "metadata_schema_updated", project.id)
        assert after == before + 1

    async def test_delete_emits_audit_event(self, db_session, admin, project):
        from specivo.mcp.tools import _create_metadata_schema, _delete_metadata_schema

        await _create_metadata_schema(db_session, admin, project.key, name="D1", schema=_basic_schema())
        result = await db_session.execute(select(MetadataSchema).where(MetadataSchema.project_id == project.id))
        sid = result.scalar_one().id

        before = await _audit_count(db_session, "metadata_schema_deleted", project.id)
        await _delete_metadata_schema(db_session, admin, project.key, sid)
        after = await _audit_count(db_session, "metadata_schema_deleted", project.id)
        assert after == before + 1
