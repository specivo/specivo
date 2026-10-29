"""Custom fields read by their names, and the people and versions they hold by theirs.

A metadata schema's property may give the field a ``title`` (an imported field
gets the name it had in the source) and say its values are ids of people or
versions (``x-specivo-ref``). The issue page's metadata panel and history, the
search filter, the REST API and MCP show the title, or the key humanised
without one; the ids are named for a reader who may see what they name, by
the rules history follows, and shown as stored otherwise.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools import _list_comments, _list_metadata_schemas, _show_issue
from specivo.models.journal import CF_USER, CF_VERSION, Journal, JournalDetail
from specivo.models.metadata_schema import MetadataSchema
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.metadata_fields import field_label, humanise
from specivo.services.permission_service import clear_role_cache
from tests.factories.user import UserFactory
from tests.factories.version import VersionFactory
from tests.integration.test_journal_detail_names import _issue, _join, _project

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

THAI = "ระดับความเสี่ยง"


async def _world(db: AsyncSession, reader: User, prefix: str) -> dict[str, Any]:
    """An issue of <prefix>A with a Thai-named field, a user field and a version field.

    The user field holds a colleague the reader shares project <prefix>C with
    and a stranger of project <prefix>B only; the version field a version of
    <prefix>A and one of <prefix>B, which the reader may not open.
    """
    from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory

    status = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()

    mine = await _project(db, f"{prefix}A")
    theirs = await _project(db, f"{prefix}B")
    shared = await _project(db, f"{prefix}C")
    role = Role(
        name=f"{prefix} Member",
        permissions=["view_issues", "edit_issues", "add_issues"],
        builtin=0,
        users_visibility="members_of_visible_projects",
    )
    db.add(role)
    await db.flush()
    await _join(db, reader, mine, role)
    await _join(db, reader, shared, role)
    colleague = UserFactory.build(login=f"{prefix.lower()}_colleague", display_name=f"{prefix} Colleague")
    stranger = UserFactory.build(login=f"{prefix.lower()}_stranger", display_name=f"{prefix} Stranger")
    db.add_all([colleague, stranger])
    await db.flush()
    await _join(db, colleague, shared, role)
    await _join(db, stranger, theirs, role)

    own_version = VersionFactory.build(project_id=mine.id, name=f"{prefix} own release")
    their_version = VersionFactory.build(project_id=theirs.id, name=f"{prefix} secret release")
    db.add_all([own_version, their_version])
    await db.flush()

    db.add(
        MetadataSchema(
            project_id=mine.id,
            content_type="issue",
            name="Imported fields",
            schema_definition={
                "type": "object",
                "properties": {
                    "customfield_10044": {"type": "string", "title": THAI},
                    "reviewers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "title": "Reviewers",
                        "x-specivo-ref": "user",
                    },
                    "found_in": {"type": "integer", "title": "Found in version", "x-specivo-ref": "version"},
                    "target": {"type": "integer", "title": "Target", "x-specivo-ref": "version"},
                    "story_points": {"type": "number"},
                },
            },
        )
    )
    issue = await _issue(db, mine, (tracker, status, priority), reader, 1)
    issue.issue_metadata = {
        "customfield_10044": "ต่ำ",
        "reviewers": [colleague.id, stranger.id],
        "found_in": their_version.id,
        "target": own_version.id,
        "story_points": 3,
    }
    journal = Journal(issue_id=issue.id, project_id=mine.id, user_id=reader.id, sequence=2)
    db.add(journal)
    await db.flush()
    db.add_all(
        [
            JournalDetail(journal_id=journal.id, property="cf", prop_key="customfield_10044", new_value="ต่ำ"),
            JournalDetail(journal_id=journal.id, property=CF_USER, prop_key="reviewers", new_value=str(colleague.id)),
            JournalDetail(
                journal_id=journal.id, property=CF_VERSION, prop_key="found_in", new_value=str(their_version.id)
            ),
            JournalDetail(journal_id=journal.id, property="cf", prop_key="story_points", new_value="3"),
        ]
    )
    await db.commit()
    clear_role_cache(db)
    return {
        "issue": issue,
        "colleague": colleague,
        "stranger": stranger,
        "own_version": own_version,
        "their_version": their_version,
    }


def _panel(html: str) -> dict[str, Any]:
    """The metadata panel's initial data: schemas, metadata and value names."""
    start = html.index("issueMetadataPanel(") + len("issueMetadataPanel(")
    raw = html[start : html.index(", issueRef:", start)] + "}"
    return json.loads(
        raw.replace("schemas:", '"schemas":', 1)
        .replace(", metadata:", ', "metadata":', 1)
        .replace(", names:", ', "names":', 1)
    )


class TestLabels:
    def test_a_title_is_the_name_and_a_key_reads_as_one_without(self):
        assert field_label("customfield_10044", {"title": THAI}) == THAI
        assert field_label("qa_owner", {"type": "integer"}) == "Qa Owner"
        assert field_label("qa_owner", {"title": "  "}) == "Qa Owner"
        assert humanise("customfield_10044") == "Customfield 10044"

    async def test_history_names_the_field(self, auth_client: AsyncClient, db_session: AsyncSession):
        world = await _world(db_session, auth_client.state.user, "CFN")
        resp = await auth_client.get(f"/issue/{world['issue'].display_key}/")
        assert resp.status_code == 200
        text = " ".join(resp.text.split())
        assert f'<strong class="sp-change-label">{THAI}</strong>' in text
        assert '<strong class="sp-change-label">Reviewers</strong>' in text
        # No title: the key humanised, as before.
        assert '<strong class="sp-change-label">Story Points</strong>' in text
        assert "Customfield 10044" not in text

    async def test_the_panel_gets_the_titles(self, auth_client: AsyncClient, db_session: AsyncSession):
        world = await _world(db_session, auth_client.state.user, "CFP")
        resp = await auth_client.get(f"/issue/{world['issue'].display_key}/")
        panel = _panel(resp.text)
        props = panel["schemas"][0]["schema_definition"]["properties"]
        assert props["customfield_10044"]["title"] == THAI


class TestValues:
    async def test_people_and_versions_are_named_for_who_may_see_them(
        self, auth_client: AsyncClient, db_session: AsyncSession
    ):
        world = await _world(db_session, auth_client.state.user, "CFV")
        resp = await auth_client.get(f"/issue/{world['issue'].display_key}/")
        names = _panel(resp.text)["names"]
        # The colleague shares a project with the reader; the stranger does not.
        assert names["reviewers"] == {str(world["colleague"].id): world["colleague"].login}
        # A version of a project the reader may not open is shown as stored.
        assert names["target"] == {str(world["own_version"].id): world["own_version"].name}
        assert "found_in" not in names
        assert world["stranger"].login not in resp.text
        assert world["their_version"].name not in resp.text

    async def test_the_api_names_fields_and_values(self, auth_client: AsyncClient, db_session: AsyncSession):
        world = await _world(db_session, auth_client.state.user, "CFA")
        resp = await auth_client.get(f"/api/v1/issues/{world['issue'].display_key}/?include=journals")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        fields = {f["key"]: f for f in body["metadata_fields"]}
        assert fields["customfield_10044"]["name"] == THAI
        assert fields["story_points"]["name"] == "Story Points"
        assert fields["reviewers"]["value_names"] == {str(world["colleague"].id): world["colleague"].display_name}
        assert fields["found_in"]["value_names"] == {}
        assert fields["found_in"]["value"] == world["their_version"].id
        details = {d["prop_key"]: d for j in body["journals"] for d in j["details"]}
        assert details["customfield_10044"]["field_name"] == THAI
        assert details["found_in"]["field_name"] == "Found in version"
        assert details["found_in"]["new_name"] is None
        assert world["their_version"].name not in resp.text
        assert world["stranger"].display_name not in resp.text

    async def test_mcp_names_fields_and_values(self, auth_client: AsyncClient, db_session: AsyncSession):
        reader = auth_client.state.user
        world = await _world(db_session, reader, "CFM")
        shown = await _show_issue(db_session, reader, world["issue"].display_key)
        assert f"  {THAI} (customfield_10044): ต่ำ" in shown
        assert (
            f"  Reviewers (reviewers): {world['colleague'].display_name} (id {world['colleague'].id}), "
            f"{world['stranger'].id}" in shown
        )
        assert f"  Found in version (found_in): {world['their_version'].id}" in shown
        assert world["their_version"].name not in shown
        history = await _list_comments(db_session, reader, world["issue"].display_key, include_changes=True)
        assert f"{THAI} (customfield_10044, custom field): (none) -> ต่ำ" in history
        schemas = await _list_metadata_schemas(db_session, reader, "CFMA")
        assert f'customfield_10044 "{THAI}": string' in schemas

    async def test_a_reader_who_cannot_see_the_user_gets_the_stored_value(
        self, auth_client: AsyncClient, db_session: AsyncSession
    ):
        """The same issue, read by a member of its project only: the colleague is a stranger to them."""
        world = await _world(db_session, auth_client.state.user, "CFS")
        outsider = UserFactory.build(login="cfs_outsider", display_name="CFS Outsider")
        db_session.add(outsider)
        await db_session.flush()
        role = Role(name="CFS Outsider role", permissions=["view_issues"], builtin=0,
                    users_visibility="members_of_visible_projects")  # fmt: skip
        db_session.add(role)
        await db_session.flush()
        from sqlalchemy import select

        from specivo.models.project import Project

        project = (await db_session.execute(select(Project).where(Project.key == "CFSA"))).scalar_one()
        await _join(db_session, outsider, project, role)
        await db_session.commit()
        clear_role_cache(db_session)
        shown = await _show_issue(db_session, outsider, world["issue"].display_key)
        assert f"  Reviewers (reviewers): {world['colleague'].id}, {world['stranger'].id}" in shown
        assert world["colleague"].display_name not in shown


class TestSearchFilter:
    async def test_the_filter_reads_by_the_fields_name(self, auth_client: AsyncClient, db_session: AsyncSession):
        world = await _world(db_session, auth_client.state.user, "CFF")
        resp = await auth_client.get(
            f"/search/?scope=issues&mf=reviewers&mv={world['colleague'].id}&ma=1&project_key=CFFA"
        )
        assert resp.status_code == 200
        assert f"Reviewers = {world['colleague'].login}" in resp.text

    async def test_a_thai_name_in_the_filter(self, auth_client: AsyncClient, db_session: AsyncSession):
        await _world(db_session, auth_client.state.user, "CFT")
        resp = await auth_client.get("/search/?scope=issues&mf=customfield_10044&mv=x&project_key=CFTA")
        assert f"{THAI} = x" in resp.text
