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
        resp = await auth_client.get(f"/api/v1/projects/{project.key}/metadata-schemas/")
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

        resp = await admin_client.delete(f"/api/v1/admin/metadata-presets/{custom_preset.slug}/")
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
        resp = await admin_client.delete(f"/api/v1/admin/metadata-presets/{custom_preset.slug}/")
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Validation judges the change
# ---------------------------------------------------------------------------
#
# Schemas reject new invalid values on every write path (REST, web, MCP),
# but only for the keys a request changes: an issue that already holds an
# invalid value stays editable, and tightening a schema never traps it.

# The shape of a real schema with a pattern, an integer and no extra keys.
RELEASE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "commits": {"type": "array", "items": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"}},
        "game_year": {"type": "integer"},
        "notes": {"type": "string"},
    },
}

# What such an issue can hold from before validation was on.
OLD_INVALID = {"commits": ["abc1234", "not-a-hash", 42], "game_year": "1999", "legacy_key": "kept"}


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession):
    from tests.factories.lookups import PriorityFactory

    p = PriorityFactory.build(name="ValNormal", is_default=True, position=2)
    db_session.add(p)
    await db_session.flush()
    return p


@pytest_asyncio.fixture
async def other_status(db_session: AsyncSession):
    s = StatusFactory.build(name="ValDoing", position=2, category="backlog")
    db_session.add(s)
    await db_session.flush()
    return s


@pytest_asyncio.fixture
async def writer(db_session: AsyncSession, auth_client: AsyncClient, project: Project):
    """Make the ``auth_client`` user a non-admin member who can add and edit issues."""
    from specivo.models.member import Member, MemberRole
    from specivo.models.role import Role

    role = Role(
        name="MValWriter",
        position=40,
        builtin=0,
        permissions=["view_issues", "add_issues", "edit_issues", "add_issue_notes"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.flush()
    user = auth_client.state.user
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    return user


async def _schema(
    db_session: AsyncSession, project: Project, definition: dict, name: str = "release"
) -> MetadataSchema:
    s = MetadataSchema(project_id=project.id, tracker_id=None, name=name, schema_definition=definition)
    db_session.add(s)
    await db_session.commit()
    return s


async def _post_issue(client: AsyncClient, project: Project, tracker, metadata: dict | None = None, **headers):
    body: dict = {"project_key": project.key, "tracker_id": tracker.id, "subject": "Validated"}
    if metadata is not None:
        body["metadata"] = metadata
    return await client.post(f"/api/v1/projects/{project.key}/issues/", json=body, headers=headers)


async def _stored_issue(db_session: AsyncSession, project: Project, tracker, author, metadata: dict):
    """An issue whose metadata is written past validation, as old data was."""
    from specivo.schemas.issue import IssueCreate
    from specivo.services.issue_service import IssueService

    issue = await IssueService().create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Old issue", metadata=metadata),
        author,
        validate_metadata=False,
    )
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


async def _patch(client: AsyncClient, issue, **fields):
    resp = await client.get(f"/api/v1/issues/{issue.display_key}/")
    assert resp.status_code == 200, resp.text
    return await client.patch(
        f"/api/v1/issues/{issue.display_key}/",
        json={**fields, "lock_version": resp.json()["lock_version"]},
    )


class TestNewValuesAreRefused:
    async def test_create_with_invalid_value_is_refused(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        await _schema(db_session, project, RELEASE_SCHEMA)
        resp = await _post_issue(auth_client, project, tracker, {"game_year": "nineteen"})
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["code"] == "validation_error"

    async def test_create_missing_required_field_is_refused(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        await _schema(db_session, project, {**RELEASE_SCHEMA, "required": ["game_year"]})
        resp = await _post_issue(auth_client, project, tracker, {"notes": "no year"})
        assert resp.status_code == 422, resp.text
        error = resp.json()["errors"][0]
        assert error["code"] == "validation_error"
        assert error["field"] == "metadata.game_year"
        # Without any metadata at all, a new issue still needs the field.
        resp = await _post_issue(auth_client, project, tracker)
        assert resp.status_code == 422, resp.text

    async def test_update_with_invalid_value_is_refused(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        await _schema(db_session, project, RELEASE_SCHEMA)
        resp = await _post_issue(auth_client, project, tracker, {"game_year": 2001})
        assert resp.status_code == 201, resp.text
        key = resp.json()["key"]
        resp = await auth_client.patch(
            f"/api/v1/issues/{key}/",
            json={"metadata": {"game_year": 2001, "commits": ["xyz"]}, "lock_version": resp.json()["lock_version"]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == "metadata.commits"

    async def test_mcp_metadata_set_with_invalid_value_returns_error(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        from specivo.mcp.tools import _metadata

        await _schema(db_session, project, RELEASE_SCHEMA)
        issue = await _stored_issue(db_session, project, tracker, writer, {})
        result = await _metadata(db_session, writer, issue.display_key, "game_year", "set", "soon")
        assert result.startswith("Error:"), result
        assert "game_year" in result
        await db_session.refresh(issue)
        assert issue.issue_metadata == {}

    async def test_invalid_value_error_names_the_key(self, db_session, auth_client, project, tracker, priority, writer):
        await _schema(db_session, project, RELEASE_SCHEMA, name="release-fields")
        resp = await _post_issue(auth_client, project, tracker, {"commits": ["abc1234", "nope"]})
        assert resp.status_code == 422, resp.text
        error = resp.json()["errors"][0]
        assert error["field"] == "metadata.commits"
        assert "commits" in error["message"]
        assert "release-fields" in error["message"]
        assert error["details"]["schema_name"] == "release-fields"
        assert error["details"]["json_path"] == ["commits", 1]

    async def test_unknown_key_is_refused_when_extra_keys_are_not_allowed(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        await _schema(db_session, project, RELEASE_SCHEMA)
        resp = await _post_issue(auth_client, project, tracker, {"game_yaer": 2001})
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == "metadata.game_yaer"

    async def test_error_prefix_is_translated_in_thai(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        """The message prefix goes through gettext.

        The Thai catalogue gets the string in the release's catalogue pass, so a
        stand-in catalogue proves the routing here. The jsonschema reason stays English.
        """
        import gettext as gettext_module
        from unittest.mock import patch

        class _Thai(gettext_module.NullTranslations):
            def gettext(self, message: str) -> str:
                if message.startswith("Metadata field '{key}' does not match schema"):
                    return "ฟิลด์ '{key}' ไม่ตรงกับสคีมา '{schema}': {reason}"
                return message

        await _schema(db_session, project, RELEASE_SCHEMA)
        with patch("specivo.core.i18n._translations_cache", {"th": _Thai()}):
            resp = await _post_issue(auth_client, project, tracker, {"game_year": "x"}, **{"Accept-Language": "th"})
        assert resp.status_code == 422, resp.text
        message = resp.json()["errors"][0]["message"]
        assert message.startswith("ฟิลด์ 'game_year' ไม่ตรงกับสคีมา 'release'"), message
        assert "is not of type 'integer'" in message


class TestTheChangeIsJudged:
    async def test_old_invalid_issue_can_change_status_and_other_keys(
        self, db_session, auth_client, project, tracker, priority, other_status, writer
    ):
        await _schema(db_session, project, RELEASE_SCHEMA)
        issue = await _stored_issue(db_session, project, tracker, writer, dict(OLD_INVALID))

        # A status change that resends the same metadata, as the edit form does.
        resp = await _patch(auth_client, issue, status_id=other_status.id, metadata=dict(OLD_INVALID))
        assert resp.status_code == 200, resp.text
        # A different key changes; the old bad values and the unknown key stay.
        resp = await _patch(auth_client, issue, metadata={**OLD_INVALID, "notes": "release notes"})
        assert resp.status_code == 200, resp.text
        # Writing to the bad key itself is judged, even when the new item is good.
        resp = await _patch(
            auth_client, issue, metadata={**OLD_INVALID, "notes": "release notes",
                                          "commits": [*OLD_INVALID["commits"], "def5678"]}
        )  # fmt: skip
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == "metadata.commits"
        # Fixing the key is accepted.
        resp = await _patch(
            auth_client, issue, metadata={**OLD_INVALID, "notes": "release notes", "commits": ["abc1234", "def5678"]}
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(issue)
        assert issue.issue_metadata["commits"] == ["abc1234", "def5678"]
        assert issue.issue_metadata["legacy_key"] == "kept"
        assert issue.status_id == other_status.id

    async def test_a_boolean_replacing_a_stored_one_is_a_change(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        """Python says ``True == 1``; in JSON they are different values, and only one is an integer."""
        schema = {"type": "object", "properties": {"retries": {"type": "integer"}}}
        await _schema(db_session, project, schema)
        issue = await _stored_issue(db_session, project, tracker, writer, {"retries": 1})

        resp = await _patch(auth_client, issue, metadata={"retries": True})

        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == "metadata.retries"

    async def test_root_refusal_does_not_quote_stored_values(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        schema = {"type": "object", "maxProperties": 1}
        await _schema(db_session, project, schema)
        issue = await _stored_issue(db_session, project, tracker, writer, {"private_note": "stored-secret"})

        resp = await _patch(auth_client, issue, metadata={"private_note": "stored-secret", "other": 1})

        assert resp.status_code == 422, resp.text
        assert "stored-secret" not in resp.text

    async def test_old_invalid_issue_can_drop_a_key(self, db_session, auth_client, project, tracker, priority, writer):
        await _schema(db_session, project, {**RELEASE_SCHEMA, "required": ["notes"]})
        issue = await _stored_issue(db_session, project, tracker, writer, {"notes": "n", "game_year": "1999"})
        resp = await _patch(auth_client, issue, metadata={"notes": "n"})
        assert resp.status_code == 200, resp.text
        # Removing a required key is a change to it, and is refused.
        resp = await _patch(auth_client, issue, metadata={})
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["field"] == "metadata.notes"

    async def test_form_resending_unchanged_metadata_is_not_validated(
        self, db_session, project, tracker, priority, writer
    ):
        """Unchanged metadata costs no schema query and cannot fail."""
        from sqlalchemy import event

        from specivo.services.metadata_schema_service import MetadataSchemaService

        await _schema(db_session, project, RELEASE_SCHEMA)
        statements: list[str] = []

        def _record(conn, cursor, statement, *args) -> None:  # noqa: ANN001
            statements.append(statement)

        engine = db_session.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", _record)
        try:
            await MetadataSchemaService().validate_metadata(
                db_session, project.id, tracker.id, dict(OLD_INVALID), previous=dict(OLD_INVALID)
            )
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        assert statements == []

    async def test_tightened_required_does_not_block_unrelated_edit(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        schema = await _schema(db_session, project, RELEASE_SCHEMA)
        resp = await _post_issue(auth_client, project, tracker, {"notes": "before"})
        assert resp.status_code == 201, resp.text
        key = resp.json()["key"]
        schema.schema_definition = {**RELEASE_SCHEMA, "required": ["game_year"]}
        await db_session.commit()

        resp = await auth_client.get(f"/api/v1/issues/{key}/")
        resp = await auth_client.patch(
            f"/api/v1/issues/{key}/",
            json={"subject": "Renamed", "metadata": {"notes": "after"}, "lock_version": resp.json()["lock_version"]},
        )
        assert resp.status_code == 200, resp.text

    async def test_move_and_tracker_change_do_not_revalidate(
        self, db_session, auth_client, project, tracker, priority, writer
    ):
        other_tracker = TrackerFactory.build(name="ValTask", default_status_id=tracker.default_status_id)
        db_session.add(other_tracker)
        await db_session.flush()
        target = ProjectFactory.build(key="MVALT", name="Metadata Validation Target", is_public=True)
        db_session.add(target)
        await db_session.commit()
        for proj in (project, target):
            await _schema(db_session, proj, {**RELEASE_SCHEMA, "required": ["game_year"]})
        issue = await _stored_issue(db_session, project, tracker, writer, dict(OLD_INVALID))

        resp = await _patch(auth_client, issue, tracker_id=other_tracker.id)
        assert resp.status_code == 200, resp.text
        resp = await _patch(auth_client, issue, tracker_id=tracker.id, metadata=dict(OLD_INVALID))
        assert resp.status_code == 200, resp.text

        from specivo.services.issue_service import IssueService

        await db_session.refresh(issue)
        moved = await IssueService().move(db_session, issue, target, writer)
        assert moved.project_id == target.id
        assert moved.issue_metadata["game_year"] == "1999"

    async def test_schemas_absent_accepts_anything(self, db_session, auth_client, project, tracker, priority, writer):
        resp = await _post_issue(auth_client, project, tracker, {"anything": {"nested": [1, "two"]}})
        assert resp.status_code == 201, resp.text
        resp = await auth_client.patch(
            f"/api/v1/issues/{resp.json()['key']}/",
            json={"metadata": {"anything": None}, "lock_version": resp.json()["lock_version"]},
        )
        assert resp.status_code == 200, resp.text


class TestMetadataCheck:
    async def test_metadata_check_prints_counts_and_keys_but_no_values(
        self, db_session, project, tracker, priority, capsys
    ):
        from specivo.cli.admin import build_parser, print_metadata_check
        from tests.factories.user import UserFactory

        author = UserFactory.build(login="mval_check_author", status="active")
        db_session.add(author)
        await db_session.flush()
        await _schema(db_session, project, {**RELEASE_SCHEMA, "required": ["notes"]})
        bad = await _stored_issue(db_session, project, tracker, author, dict(OLD_INVALID))
        worse = await _stored_issue(db_session, project, tracker, author, {"game_year": "secret-2042"})
        await _stored_issue(db_session, project, tracker, author, {"notes": "fine", "game_year": 2001})

        assert build_parser().parse_args(["metadata-check", "--project", "MVAL"]).project == "MVAL"
        assert await print_metadata_check(db_session, "mval") == 0
        out = capsys.readouterr().out

        assert "MVAL" in out and "release" in out
        assert "2 issue(s)" in out
        assert bad.display_key in out and worse.display_key in out
        for key, count in (("game_year", 2), ("notes", 2), ("commits", 1), ("legacy_key", 1)):
            assert f"{key} ({count})" in out
        for value in ("not-a-hash", "1999", "kept", "secret-2042"):
            assert value not in out

    async def test_metadata_check_with_nothing_wrong(self, db_session, project, capsys):
        from specivo.cli.admin import print_metadata_check

        assert await print_metadata_check(db_session, None) == 0
        assert "No issue" in capsys.readouterr().out


class TestMetadataPanel:
    async def test_panel_has_a_place_for_the_refusal(self, db_session, auth_client, project, tracker, priority, writer):
        """The panel shows the server's message on a refused save (the e2e test drives it in a browser)."""
        await _schema(db_session, project, RELEASE_SCHEMA)
        issue = await _stored_issue(db_session, project, tracker, writer, {"notes": "n"})
        page = await auth_client.get(f"/issue/{issue.display_key}/")
        assert page.status_code == 200, page.text
        assert 'class="sp-metadata-error" x-show="error" x-text="error" role="alert"' in page.text
        assert "The metadata could not be saved." in page.text
