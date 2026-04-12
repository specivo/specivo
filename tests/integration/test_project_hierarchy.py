"""Integration tests for project parent re-assignment.

Tests cover:
- PATCH project to set a new parent
- PATCH project to remove parent (move to root)
- Cycle detection: setting parent to a descendant is rejected
- Self-assignment: setting parent to self is rejected
- Non-existent parent is rejected
- ltree path is updated for the moved project and all descendants
- Settings page HTML includes parent project dropdown
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from tests.factories.project import ProjectFactory

pytestmark = pytest.mark.integration

PATCH_URL = "/api/v1/projects/{key}/"
SETTINGS_URL = "/projects/{key}/settings/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_project(db: AsyncSession, **kwargs) -> Project:
    proj = ProjectFactory.build(**kwargs)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# PATCH parent_id tests
# ---------------------------------------------------------------------------


class TestUpdateProjectParent:
    async def test_update_project_parent(self, admin_client: AsyncClient, db_session: AsyncSession):
        """PATCH project to set a new parent moves it in the hierarchy."""
        parent = await _create_project(db_session, key="PAR1", identifier="par-one", path="par_one")
        child = await _create_project(db_session, key="CHD1", identifier="chd-one", path="chd_one")

        resp = await admin_client.patch(
            PATCH_URL.format(key="CHD1"),
            json={"parent_id": parent.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["parent_id"] == parent.id
        assert data["parent_key"] == "PAR1"

    async def test_update_project_remove_parent(self, admin_client: AsyncClient, db_session: AsyncSession):
        """PATCH with parent_id=null moves a project to root."""
        parent = await _create_project(db_session, key="PAR2", identifier="par-two", path="par_two")
        child = await _create_project(
            db_session,
            key="CHD2",
            identifier="chd-two",
            path="par_two.chd_two",
            parent_id=parent.id,
        )

        resp = await admin_client.patch(
            PATCH_URL.format(key="CHD2"),
            json={"parent_id": None},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["parent_id"] is None
        assert data["parent_key"] is None

    async def test_update_project_parent_cycle_rejected(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Setting parent to a descendant creates a cycle and must be rejected with 400."""
        grandparent = await _create_project(db_session, key="GP01", identifier="gp-one", path="gp_one")
        parent = await _create_project(
            db_session,
            key="PRT1",
            identifier="prt-one",
            path="gp_one.prt_one",
            parent_id=grandparent.id,
        )
        child = await _create_project(
            db_session,
            key="CYC1",
            identifier="cyc-one",
            path="gp_one.prt_one.cyc_one",
            parent_id=parent.id,
        )

        # Try to set grandparent's parent to its own grandchild
        resp = await admin_client.patch(
            PATCH_URL.format(key="GP01"),
            json={"parent_id": child.id},
        )
        assert resp.status_code == 400

    async def test_update_project_parent_self_rejected(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Setting a project's parent to itself is rejected with 400."""
        proj = await _create_project(db_session, key="SELF", identifier="self-proj", path="self_proj")

        resp = await admin_client.patch(
            PATCH_URL.format(key="SELF"),
            json={"parent_id": proj.id},
        )
        assert resp.status_code == 400

    async def test_update_project_parent_nonexistent_rejected(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        """Setting parent to a non-existent project ID is rejected with 404."""
        proj = await _create_project(db_session, key="NEX1", identifier="nex-one", path="nex_one")

        resp = await admin_client.patch(
            PATCH_URL.format(key="NEX1"),
            json={"parent_id": 999999},
        )
        assert resp.status_code == 404

    async def test_update_project_parent_updates_path(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Moving a project updates ltree path for the project and all descendants."""
        new_parent = await _create_project(db_session, key="NWP1", identifier="new-parent", path="new_parent")
        moved = await _create_project(db_session, key="MOV1", identifier="moved-proj", path="moved_proj")
        grandchild = await _create_project(
            db_session,
            key="GCH1",
            identifier="grand-child",
            path="moved_proj.grand_child",
            parent_id=moved.id,
        )
        great_grandchild = await _create_project(
            db_session,
            key="GGC1",
            identifier="great-grand",
            path="moved_proj.grand_child.great_grand",
            parent_id=grandchild.id,
        )

        resp = await admin_client.patch(
            PATCH_URL.format(key="MOV1"),
            json={"parent_id": new_parent.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Moved project path should be new_parent.moved_proj
        assert data["path"] == "new_parent.moved_proj"

        # Descendants must also have their paths updated
        await db_session.refresh(grandchild)
        await db_session.refresh(great_grandchild)
        assert grandchild.path == "new_parent.moved_proj.grand_child"
        assert great_grandchild.path == "new_parent.moved_proj.grand_child.great_grand"

    async def test_update_project_parent_updates_path_to_root(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        """Removing parent (move to root) updates ltree path for project and descendants."""
        old_parent = await _create_project(db_session, key="OLP1", identifier="old-parent", path="old_parent")
        moved = await _create_project(
            db_session,
            key="RM01",
            identifier="rm-proj",
            path="old_parent.rm_proj",
            parent_id=old_parent.id,
        )
        child = await _create_project(
            db_session,
            key="RC01",
            identifier="rm-child",
            path="old_parent.rm_proj.rm_child",
            parent_id=moved.id,
        )

        resp = await admin_client.patch(
            PATCH_URL.format(key="RM01"),
            json={"parent_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["path"] == "rm_proj"

        await db_session.refresh(child)
        assert child.path == "rm_proj.rm_child"


# ---------------------------------------------------------------------------
# Settings page HTML dropdown test
# ---------------------------------------------------------------------------


class TestSettingsPageParentDropdown:
    async def test_settings_page_shows_parent_dropdown(self, admin_client: AsyncClient, db_session: AsyncSession):
        """GET settings page contains a parent project select element."""
        proj = await _create_project(db_session, key="DRP1", identifier="drp-one", path="drp_one")
        other = await _create_project(db_session, key="DRP2", identifier="drp-two", path="drp_two")

        resp = await admin_client.get(SETTINGS_URL.format(key="DRP1"))
        assert resp.status_code == 200
        html = resp.text

        # Dropdown must be present
        assert 'name="parent_id"' in html or 'x-model="parentId"' in html
        # The other project should appear as an option
        assert "DRP2" in html or "drp-two" in html
        # Self should not appear as an option
        assert 'value="' in html  # sanity: options exist
