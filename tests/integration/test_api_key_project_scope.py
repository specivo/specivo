"""An API key limited to some projects reaches those projects and nothing else, over REST.

The key's owner is a member of projects A and B (and a public project C is
open to every signed-in user); the key is limited to A. Everything in B and C
then behaves as if it did not exist: single reads, lists, search, relations,
history names, wiki, time entries, and every write. Inside A the key does
whatever its owner may. An unlimited key of the same owner is unaffected.

A limit is validated when the key is created and stored as project ids; a
stored limit is always enforced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.models.auth import ApiKey
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.member import Member, MemberRole
from specivo.models.notification import Notification
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.api_key_scope import (
    ProjectScope,
    bound_scope,
    current_project_scope,
    resolve_project_scope,
    scope_project_ids,
)
from specivo.services.api_key_service import ApiKeyService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory
from tests.factories.version import VersionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_MODULES = ("issue_tracking", "wiki", "time_tracking")


@dataclass
class World:
    owner: User
    a: Project
    b: Project
    c: Project
    tracker_id: int
    activity_id: int
    a_issue: dict
    b_issue: dict
    c_issue: dict
    b_entry_id: int
    b_version_name: str
    b_only_user: User
    limited_raw: str
    open_raw: str


async def _project(db: AsyncSession, key: str, *, is_public: bool = False) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=is_public)
    db.add(project)
    await db.flush()
    db.add_all([EnabledModule(project_id=project.id, name=name) for name in _MODULES])
    await db.flush()
    return project


async def _join(db: AsyncSession, user: User, project: Project, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


def _auth(client: AsyncClient, raw_key: str) -> AsyncClient:
    client.headers["Authorization"] = f"Bearer {raw_key}"
    return client


async def _create_issue(client: AsyncClient, project: Project, tracker_id: int, subject: str) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={"project_key": project.key, "subject": subject, "tracker_id": tracker_id},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def build_world(db: AsyncSession, client: AsyncClient, prefix: str, *, admin: bool = False) -> World:
    """Projects <prefix>A, <prefix>B (both with the owner as manager) and public <prefix>C, with content in each."""
    factory = AdminUserFactory if admin else UserFactory
    owner = factory.build(login=f"{prefix.lower()}_owner", display_name=f"{prefix} Owner", status="active")
    b_only = UserFactory.build(login=f"{prefix.lower()}_bonly", display_name=f"{prefix} Bonly", status="active")
    db.add_all([owner, b_only])
    await db.flush()

    status = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal", position=1, is_default=True)
    activity = TimeEntryActivityFactory.build(name=f"{prefix} Dev", is_default=True)
    db.add_all([tracker, priority, activity])
    await db.flush()

    role = Role(name=f"{prefix} Manager", permissions=["*"], builtin=0, issues_visibility="all")
    db.add(role)
    await db.flush()

    a = await _project(db, f"{prefix}A")
    b = await _project(db, f"{prefix}B")
    c = await _project(db, f"{prefix}C", is_public=True)
    await _join(db, owner, a, role)
    await _join(db, owner, b, role)
    await _join(db, b_only, b, role)
    b_version = VersionFactory.build(project_id=b.id, name=f"{prefix} B release")
    db.add(b_version)
    await db.flush()

    service = ApiKeyService()
    limited, limited_raw = await service.create_key(session=db, user_id=owner.id, name=f"{prefix}-limited")
    # Stored directly, by project key: a limit stored before creation resolved it to ids still holds.
    limited.scopes = {"projects": [a.key]}
    _open, open_raw = await service.create_key(session=db, user_id=owner.id, name=f"{prefix}-open")
    await db.commit()

    _auth(client, open_raw)
    a_issue = await _create_issue(client, a, tracker.id, f"{prefix} alpha quokka")
    b_issue = await _create_issue(client, b, tracker.id, f"{prefix} beta quokka")
    # The owner is no member of public C: its issue is written directly.
    c_row = IssueFactory.build(
        project_id=c.id,
        project_key=c.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=b_only.id,
        subject=f"{prefix} gamma quokka",
    )
    db.add(c_row)
    await db.commit()
    c_issue = {"id": c_row.id, "key": c_row.display_key}

    resp = await client.post(
        f"/api/v1/issues/{a_issue['key']}/relations/",
        json={"issue_to_key": b_issue["key"], "relation_type": "relates"},
    )
    assert resp.status_code == 201, resp.text
    for project in (a, b):
        resp = await client.post(
            f"/api/v1/projects/{project.key}/wiki/", json={"title": "Handbook", "text": f"{project.key} handbook"}
        )
        assert resp.status_code == 201, resp.text
    resp = await client.post(
        f"/api/v1/projects/{b.key}/time-entries/",
        json={"activity_id": activity.id, "hours": "1.5", "spent_on": date.today().isoformat()},
    )
    assert resp.status_code == 201, resp.text
    b_entry_id = resp.json()["id"]

    # A field change on A's issue naming B's version.
    journal = Journal(issue_id=a_issue["id"], project_id=a.id, user_id=owner.id, sequence=50)
    db.add(journal)
    await db.flush()
    db.add(
        JournalDetail(
            journal_id=journal.id,
            property="attr",
            prop_key="fixed_version_id",
            old_value=str(b_version.id),
            new_value=None,
        )
    )
    await db.commit()

    return World(
        owner=owner,
        a=a,
        b=b,
        c=c,
        tracker_id=tracker.id,
        activity_id=activity.id,
        a_issue=a_issue,
        b_issue=b_issue,
        c_issue=c_issue,
        b_entry_id=b_entry_id,
        b_version_name=b_version.name,
        b_only_user=b_only,
        limited_raw=limited_raw,
        open_raw=open_raw,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_single_issue_fetch_outside_the_limit_is_not_found(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSR")

    _auth(client, w.limited_raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 404
    assert (await client.get(f"/api/v1/issues/{w.c_issue['key']}/")).status_code == 404
    assert (await client.get(f"/api/v1/issues/{w.b_issue['id']}/")).status_code == 404
    resp = await client.get(f"/api/v1/issues/{w.a_issue['key']}/")
    assert resp.status_code == 200, resp.text

    _auth(client, w.open_raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 200
    assert (await client.get(f"/api/v1/issues/{w.c_issue['key']}/")).status_code == 200


async def test_projects_and_their_lists_outside_the_limit_are_not_found(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSP")

    _auth(client, w.limited_raw)
    listed = (await client.get("/api/v1/projects/")).json()
    assert [p["key"] for p in listed["items"]] == [w.a.key]
    for project in (w.b, w.c):
        assert (await client.get(f"/api/v1/projects/{project.key}/")).status_code == 404
        assert (await client.get(f"/api/v1/projects/{project.key}/issues/")).status_code == 404
        assert (await client.get(f"/api/v1/projects/{project.key}/wiki/handbook/")).status_code == 404
    assert (await client.get(f"/api/v1/projects/{w.b.key}/time-entries/")).status_code == 404
    assert (await client.get(f"/api/v1/time-entries/{w.b_entry_id}/")).status_code == 404

    issues = (await client.get(f"/api/v1/projects/{w.a.key}/issues/")).json()
    assert [i["key"] for i in issues["items"]] == [w.a_issue["key"]]
    assert (await client.get(f"/api/v1/projects/{w.a.key}/wiki/handbook/")).status_code == 200

    _auth(client, w.open_raw)
    listed = (await client.get("/api/v1/projects/")).json()
    assert {p["key"] for p in listed["items"]} >= {w.a.key, w.b.key, w.c.key}
    assert (await client.get(f"/api/v1/projects/{w.b.key}/wiki/handbook/")).status_code == 200
    assert (await client.get(f"/api/v1/time-entries/{w.b_entry_id}/")).status_code == 200


async def test_search_finds_nothing_outside_the_limit(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSS")

    _auth(client, w.open_raw)
    found = (await client.get("/api/v1/search/", params={"q": "quokka", "scope": "issues"})).json()
    assert {i["project_key"] for i in found["items"]} >= {w.a.key, w.b.key, w.c.key}

    _auth(client, w.limited_raw)
    found = (await client.get("/api/v1/search/", params={"q": "quokka", "scope": "issues"})).json()
    assert {i["project_key"] for i in found["items"]} == {w.a.key}
    # Naming a project outside the limit reads as naming none; an issue there by its key finds nothing.
    outside = await client.get("/api/v1/search/", params={"q": "quokka", "project_key": w.b.key})
    missing = await client.get("/api/v1/search/", params={"q": "quokka", "project_key": "KSSNONE"})
    assert outside.status_code == missing.status_code == 422
    assert outside.json()["errors"][0]["code"] == missing.json()["errors"][0]["code"]
    found = (await client.get("/api/v1/search/", params={"q": w.b_issue["key"]})).json()
    assert found["items"] == []
    found = (await client.get("/api/v1/search/", params={"q": "quokka", "project_keys": f"{w.a.key},{w.c.key}"})).json()
    assert {i["project_key"] for i in found["items"]} == {w.a.key}
    # Wiki pages too.
    found = (await client.get("/api/v1/search/", params={"q": "handbook", "scope": "wiki"})).json()
    assert {i["project_key"] for i in found["items"]} <= {w.a.key}


async def test_relations_and_history_name_nothing_outside_the_limit(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSH")

    _auth(client, w.open_raw)
    relations = (await client.get(f"/api/v1/issues/{w.a_issue['key']}/relations/")).json()
    assert len(relations) == 1
    detail = await client.get(f"/api/v1/issues/{w.a_issue['key']}/", params={"include": "journals"})
    assert w.b_version_name in detail.text

    _auth(client, w.limited_raw)
    relations = (await client.get(f"/api/v1/issues/{w.a_issue['key']}/relations/")).json()
    assert relations == []
    detail = await client.get(f"/api/v1/issues/{w.a_issue['key']}/", params={"include": "journals"})
    assert detail.status_code == 200
    assert w.b_version_name not in detail.text
    assert w.b_issue["key"] not in detail.text


async def test_people_and_notifications_outside_the_limit_are_hidden(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSN")
    db_session.add_all(
        [
            Notification(
                user_id=w.owner.id,
                event_type="issue_updated",
                entity_type="Issue",
                entity_id=w.b_issue["id"],
                project_id=w.b.id,
                actor_id=w.b_only_user.id,
                title="KSN beta changed",
            ),
            Notification(
                user_id=w.owner.id,
                event_type="issue_updated",
                entity_type="Issue",
                entity_id=w.a_issue["id"],
                project_id=w.a.id,
                actor_id=w.b_only_user.id,
                title="KSN alpha changed",
            ),
        ]
    )
    await db_session.commit()

    _auth(client, w.open_raw)
    people = (await client.get("/api/v1/users/autocomplete/", params={"q": "KSN"})).json()
    assert w.b_only_user.id in {p["id"] for p in people}
    titles = {n["title"] for n in (await client.get("/api/v1/notifications/")).json()["items"]}
    assert titles == {"KSN beta changed", "KSN alpha changed"}

    _auth(client, w.limited_raw)
    people = (await client.get("/api/v1/users/autocomplete/", params={"q": "KSN"})).json()
    assert w.b_only_user.id not in {p["id"] for p in people}
    titles = {n["title"] for n in (await client.get("/api/v1/notifications/")).json()["items"]}
    assert titles == {"KSN alpha changed"}
    assert (await client.get("/api/v1/notifications/unread-count/")).json()["count"] == 1


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def test_writes_outside_the_limit_are_not_found_and_inside_succeed(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSW")
    b_key = w.b_issue["key"]

    _auth(client, w.limited_raw)
    resp = await client.post(
        f"/api/v1/projects/{w.b.key}/issues/",
        json={"project_key": w.b.key, "subject": "x", "tracker_id": w.tracker_id},
    )
    assert resp.status_code == 404
    resp = await client.patch(f"/api/v1/issues/{b_key}/", json={"subject": "hijacked", "lock_version": 0})
    assert resp.status_code == 404
    assert (await client.post(f"/api/v1/issues/{b_key}/journals/", json={"notes": "hi"})).status_code == 404
    assert (await client.delete(f"/api/v1/issues/{b_key}/")).status_code == 404
    resp = await client.post(
        f"/api/v1/projects/{w.b.key}/wiki/", json={"title": "Injected", "text": "should not be written"}
    )
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/v1/projects/{w.b.key}/time-entries/",
        json={"activity_id": w.activity_id, "hours": "1", "spent_on": date.today().isoformat()},
    )
    assert resp.status_code == 404
    assert (await client.delete(f"/api/v1/time-entries/{w.b_entry_id}/")).status_code == 404
    # A relation from A to B's issue cannot be made: B's issue is not found.
    resp = await client.post(
        f"/api/v1/issues/{w.a_issue['key']}/relations/",
        json={"issue_to_key": b_key, "relation_type": "blocks"},
    )
    assert resp.status_code == 404
    # A parent outside the limit is not found either.
    resp = await client.patch(
        f"/api/v1/issues/{w.a_issue['key']}/",
        json={"parent_id": w.b_issue["id"], "lock_version": w.a_issue["lock_version"]},
    )
    assert resp.status_code == 404
    # Bulk operations report B's issue as missing, without its key.
    resp = await client.post(
        "/api/v1/issues/bulk-update/", json={"issue_ids": [w.b_issue["id"]], "updates": {"subject": "bulk"}}
    )
    assert resp.status_code == 200
    failed = resp.json()["failed"]
    assert failed[0]["error"]["code"] == "not_found"
    assert failed[0]["key"] == f"#{w.b_issue['id']}"

    b_issue = await db_session.scalar(select(Issue).where(Issue.id == w.b_issue["id"]))
    assert b_issue is not None
    await db_session.refresh(b_issue)
    assert b_issue.subject == "KSW beta quokka"

    # Everything the owner may do in A works.
    created = await _create_issue(client, w.a, w.tracker_id, "KSW made with the limited key")
    assert created["project_key"] == w.a.key
    assert (await client.post(f"/api/v1/issues/{created['key']}/journals/", json={"notes": "ok"})).status_code == 201
    current = (await client.get(f"/api/v1/issues/{created['key']}/")).json()
    resp = await client.patch(
        f"/api/v1/issues/{created['key']}/", json={"subject": "renamed", "lock_version": current["lock_version"]}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/projects/{w.a.key}/time-entries/",
        json={"activity_id": w.activity_id, "hours": "1", "spent_on": date.today().isoformat()},
    )
    assert resp.status_code == 201, resp.text


async def test_a_limited_key_cannot_mint_keys_or_manage_the_account(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSK")

    _auth(client, w.limited_raw)
    resp = await client.post("/api/v1/my/api-keys/", json={"name": "escape"})
    assert resp.status_code == 403
    assert (await client.get("/api/v1/my/api-keys/")).status_code == 403
    assert (await client.get("/api/v1/auth/sessions/")).status_code == 403
    assert (await client.post("/api/v1/auth/logout-all/")).status_code == 403
    count = await db_session.scalar(select(ApiKey.id).where(ApiKey.name == "escape"))
    assert count is None

    _auth(client, w.open_raw)
    assert (await client.get("/api/v1/my/api-keys/")).status_code == 200


async def test_an_admin_key_limited_to_a_project_stops_at_it(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSA", admin=True)

    _auth(client, w.limited_raw)
    assert (await client.get(f"/api/v1/issues/{w.a_issue['key']}/")).status_code == 200
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 404
    assert (await client.get(f"/api/v1/issues/{w.c_issue['key']}/")).status_code == 404
    listed = (await client.get("/api/v1/projects/")).json()
    assert [p["key"] for p in listed["items"]] == [w.a.key]
    relations = (await client.get(f"/api/v1/issues/{w.a_issue['key']}/relations/")).json()
    assert relations == []
    found = (await client.get("/api/v1/search/", params={"q": "quokka", "scope": "issues"})).json()
    assert {i["project_key"] for i in found["items"]} == {w.a.key}
    # Instance-wide administration is refused.
    assert (await client.get("/api/v1/admin/users/")).status_code == 403
    resp = await client.post("/api/v1/projects/", json={"key": "KSANEW", "name": "New", "identifier": "ksanew"})
    assert resp.status_code == 403

    _auth(client, w.open_raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 200
    assert (await client.get("/api/v1/admin/users/")).status_code == 200


async def test_the_limit_does_not_outlive_the_request(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSL")

    _auth(client, w.limited_raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 404
    assert current_project_scope() is None
    _auth(client, w.open_raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 200


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def _post_key(client: AsyncClient, raw: str, body: dict) -> object:
    _auth(client, raw)
    return await client.post("/api/v1/my/api-keys/", json={"name": "made", **body})


def _scopes_error(resp) -> dict:
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    assert [e["field"] for e in errors] == ["scopes"], errors
    return errors[0]


async def test_key_created_over_rest_with_a_limit_is_limited(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSG")

    resp = await _post_key(client, w.open_raw, {"scopes": {"projects": [w.a.key.lower()]}})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["project_keys"] == [w.a.key]

    _auth(client, created["raw_key"])
    assert (await client.get(f"/api/v1/issues/{w.a_issue['key']}/")).status_code == 200
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 404
    found = (await client.get("/api/v1/search/", params={"q": "quokka", "scope": "issues"})).json()
    assert {i["project_key"] for i in found["items"]} == {w.a.key}


async def test_key_created_by_admin_for_service_account_is_limited(
    admin_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
):
    w = await build_world(db_session, client, "KSV")
    agent = ServiceAccountFactory.build(login="ksv_agent", status="active")
    db_session.add(agent)
    await db_session.flush()
    role = Role(name="KSV Agent", permissions=["*"], builtin=0, issues_visibility="all")
    db_session.add(role)
    await db_session.flush()
    await _join(db_session, agent, w.a, role)
    await _join(db_session, agent, w.b, role)
    await db_session.commit()

    resp = await admin_client.post(
        f"/api/v1/admin/users/{agent.id}/api-keys/", json={"name": "ksv-agent", "scopes": {"projects": [w.a.id]}}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["project_keys"] == [w.a.key]
    listed = (await admin_client.get(f"/api/v1/admin/users/{agent.id}/api-keys/")).json()
    assert [k["project_keys"] for k in listed] == [[w.a.key]]

    _auth(client, resp.json()["raw_key"])
    assert (await client.get(f"/api/v1/issues/{w.a_issue['key']}/")).status_code == 200
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 404


async def test_admin_route_accepts_a_project_the_service_account_is_not_in_yet(
    admin_client: AsyncClient, client: AsyncClient, db_session: AsyncSession
):
    """Existence only: membership may come later, and the key never exceeds its owner's roles."""
    w = await build_world(db_session, client, "KSW")
    agent = ServiceAccountFactory.build(login="ksw_agent", status="active")
    db_session.add(agent)
    await db_session.commit()

    resp = await admin_client.post(
        f"/api/v1/admin/users/{agent.id}/api-keys/", json={"name": "ksw-agent", "scopes": {"projects": [w.b.key]}}
    )
    assert resp.status_code == 201, resp.text
    _auth(client, resp.json()["raw_key"])
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 404

    resp = await admin_client.post(
        f"/api/v1/admin/users/{agent.id}/api-keys/", json={"name": "ksw-typo", "scopes": {"projects": ["KSWNONE"]}}
    )
    assert _scopes_error(resp)["code"] == "validation_error"


async def test_limit_is_stored_as_ids_and_survives_project_key_rename(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSN")

    resp = await _post_key(client, w.open_raw, {"scopes": {"projects": [w.a.key, str(w.a.id), w.a.id]}})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    row = await db_session.get(ApiKey, created["id"])
    assert row is not None
    assert row.scopes == {"projects": [w.a.id]}
    assert created["scopes"] == {"projects": [w.a.id]}

    w.a.key = "KSNRENAMED"
    await db_session.commit()

    _auth(client, created["raw_key"])
    listed = (await client.get("/api/v1/projects/")).json()
    assert [p["key"] for p in listed["items"]] == ["KSNRENAMED"]

    _auth(client, w.open_raw)
    keys = (await client.get("/api/v1/my/api-keys/")).json()
    assert {k["name"]: k["project_keys"] for k in keys}["made"] == ["KSNRENAMED"]


async def test_empty_projects_list_is_refused(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSE")
    error = _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": []}}))
    assert error["code"] == "validation_error"
    assert await db_session.scalar(select(ApiKey.id).where(ApiKey.name == "made")) is None


async def test_unknown_scope_key_is_refused(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSQ")
    _scopes_error(
        await _post_key(client, w.open_raw, {"scopes": {"projects": [w.a.key], "permissions": ["issues:read"]}})
    )
    _scopes_error(await _post_key(client, w.open_raw, {"scopes": {}}))


async def test_scopes_list_is_refused_by_the_api(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSM")
    _scopes_error(await _post_key(client, w.open_raw, {"scopes": ["mcp"]}))
    _scopes_error(await _post_key(client, w.open_raw, {"scopes": "ACME"}))
    _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": [True]}}))


async def test_entry_no_project_could_have_is_refused(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "IMPID")
    for entry in (2**31, 99999999999999999999, "99999999999999999999", "\u00b2", 0, -1):
        _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": [entry]}}))


async def test_stored_limit_with_an_impossible_id_reaches_nothing(db_session: AsyncSession):
    scope = await resolve_project_scope(db_session, {"projects": [2**31, "\u00b2"]})
    assert scope is not None
    assert scope.project_ids == frozenset()


async def test_create_key_rejects_bare_list_scopes(db_session: AsyncSession):
    owner = UserFactory.build(login="ksbare_owner", status="active")
    db_session.add(owner)
    await db_session.flush()

    with pytest.raises(ValidationError) as caught:
        await ApiKeyService().create_key(db_session, owner.id, "agent", scopes=["mcp"])  # type: ignore[arg-type]
    assert caught.value.field == "scopes"
    with pytest.raises(ValidationError):
        await ApiKeyService().create_key(db_session, owner.id, "agent", scopes={"projects": []})
    assert await ApiKeyService().list_keys(db_session, owner.id) == []


async def test_project_the_owner_cannot_see_is_refused_with_the_same_message_as_a_missing_one(
    client: AsyncClient, db_session: AsyncSession
):
    w = await build_world(db_session, client, "KSH")
    hidden = await _project(db_session, "KSHHIDDEN")
    await db_session.commit()

    missing = _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": ["KSHNONE"]}}))
    private = _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": [hidden.key]}}))
    by_id = _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": [hidden.id]}}))
    assert missing["message"].replace("KSHNONE", "<P>") == private["message"].replace(hidden.key, "<P>")
    assert by_id["message"].replace(str(hidden.id), "<P>") == private["message"].replace(hidden.key, "<P>")
    assert missing["code"] == private["code"] == by_id["code"]
    # A public project the owner is no member of is visible, so it may be named.
    resp = await _post_key(client, w.open_raw, {"scopes": {"projects": [w.c.key]}})
    assert resp.status_code == 201, resp.text


async def test_scope_refusal_is_translatable(client: AsyncClient, db_session: AsyncSession):
    """The refusal goes through gettext; the Thai entry comes with the catalogue pass."""
    w = await build_world(db_session, client, "KST")
    with patch("specivo.services.api_key_service._", side_effect=lambda s: f"<{s}>") as wrapped:
        error = _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": ["KSTNONE"]}}))
    assert wrapped.called
    assert error["message"].startswith("<")
    with patch("specivo.schemas.auth._", side_effect=lambda s: f"<{s}>") as wrapped:
        error = _scopes_error(await _post_key(client, w.open_raw, {"scopes": {"projects": []}}))
    assert wrapped.called
    assert error["message"].startswith("<")


async def test_key_list_shows_project_keys(client: AsyncClient, db_session: AsyncSession):
    w = await build_world(db_session, client, "KSP")
    resp = await _post_key(client, w.open_raw, {"scopes": {"projects": [w.b.key, w.a.key]}})
    assert resp.status_code == 201, resp.text
    assert resp.json()["project_keys"] == sorted([w.a.key, w.b.key])

    _auth(client, w.open_raw)
    keys = {k["name"]: k for k in (await client.get("/api/v1/my/api-keys/")).json()}
    assert keys["made"]["project_keys"] == sorted([w.a.key, w.b.key])
    assert keys[f"{w.a.key[:-1]}-open"]["project_keys"] is None
    # The limit stored by key (as build_world does) is shown too.
    assert keys[f"{w.a.key[:-1]}-limited"]["project_keys"] == [w.a.key]

    # A key whose projects are all gone shows an empty list (it reaches nothing).
    gone = await ApiKeyService().list_keys(db_session, w.owner.id)
    made = next(k for k in gone if k.name == "made")
    made.scopes = {"projects": [999_999_991]}
    await db_session.commit()
    keys = {k["name"]: k for k in (await client.get("/api/v1/my/api-keys/")).json()}
    assert keys["made"]["project_keys"] == []


async def test_key_without_scopes_stays_unlimited(client: AsyncClient, db_session: AsyncSession):
    """No scopes, and a row holding JSON ``null`` rather than SQL NULL, both reach everything."""
    w = await build_world(db_session, client, "KSZ")
    resp = await _post_key(client, w.open_raw, {"scopes": None})
    assert resp.status_code == 201, resp.text
    assert resp.json()["project_keys"] is None
    raw = resp.json()["raw_key"]
    kind = await db_session.scalar(select(func.jsonb_typeof(ApiKey.scopes)).where(ApiKey.id == resp.json()["id"]))
    assert kind in (None, "null")

    _auth(client, raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 200


async def test_bare_capability_list_stored_does_not_limit_nor_break_the_list(
    client: AsyncClient, db_session: AsyncSession
):
    """The read path stays tolerant of a list a plugin stored once: unlimited, and the key list answers."""
    w = await build_world(db_session, client, "KSY")
    key, raw = await ApiKeyService().create_key(db_session, w.owner.id, "ksy-mcp")
    key.scopes = ["mcp"]
    await db_session.commit()

    _auth(client, raw)
    assert (await client.get(f"/api/v1/issues/{w.b_issue['key']}/")).status_code == 200
    _auth(client, w.open_raw)
    resp = await client.get("/api/v1/my/api-keys/")
    assert resp.status_code == 200, resp.text
    assert {k["name"]: k["project_keys"] for k in resp.json()}["ksy-mcp"] is None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def test_scope_resolution(db_session: AsyncSession):
    first = await _project(db_session, "KSRA")
    second = await _project(db_session, "KSRB")

    assert await resolve_project_scope(db_session, None) is None
    assert await resolve_project_scope(db_session, {}) is None
    assert await resolve_project_scope(db_session, {"projects": []}) is None
    assert await resolve_project_scope(db_session, ["mcp"]) is None
    by_key = await resolve_project_scope(db_session, {"projects": ["ksra"]})
    assert by_key == ProjectScope(frozenset({first.id}))
    mixed = await resolve_project_scope(db_session, {"projects": [second.id, str(first.id)]})
    assert mixed == ProjectScope(frozenset({first.id, second.id}))
    # Naming nothing that exists, or something unreadable, reaches nothing.
    assert await resolve_project_scope(db_session, {"projects": ["NOPE"]}) == ProjectScope(frozenset())
    assert await resolve_project_scope(db_session, {"projects": {"x": 1}}) == ProjectScope(frozenset())


async def test_scope_project_ids_narrowing():
    assert scope_project_ids(None, None) == (None, None)
    with bound_scope(ProjectScope(frozenset({1, 2}))):
        assert scope_project_ids(None, None) == (None, [1, 2])
        assert scope_project_ids(1, None) == (1, None)
        assert scope_project_ids(3, None) == (None, [])
        assert scope_project_ids(None, [2, 3]) == (None, [2])
    assert current_project_scope() is None


def _refusal(resp, key: str) -> tuple[int, str]:
    """Status and body of a search refusal, with the echoed key masked."""
    return resp.status_code, json.dumps(resp.json(), sort_keys=True).replace(key, "<KEY>")


async def test_search_refuses_every_unreachable_project_like_a_missing_one(
    client: AsyncClient, db_session: AsyncSession
):
    """Missing, private-without-membership and outside-the-limit read the same; anonymous is refused uniformly."""
    w = await build_world(db_session, client, "KSU")
    _b_only_key, b_only_raw = await ApiKeyService().create_key(
        session=db_session, user_id=w.b_only_user.id, name="ksu-bonly"
    )
    await db_session.commit()
    a_key, b_key = w.a.key, w.b.key

    async def ask(raw: str | None, **params) -> object:
        if raw is None:
            client.headers.pop("Authorization", None)
        else:
            _auth(client, raw)
        return await client.get("/api/v1/search/", params={"q": "quokka", **params})

    # project_key: one 422, one code, one field, one body.
    missing = _refusal(await ask(b_only_raw, project_key="KSUNONE"), "KSUNONE")
    private = _refusal(await ask(b_only_raw, project_key=a_key), a_key)
    outside = _refusal(await ask(w.limited_raw, project_key=b_key), b_key)
    assert missing[0] == 422
    assert missing == private == outside
    assert json.loads(missing[1])["errors"][0]["field"] == "project_key"

    # project_keys: the same when no named project is reachable.
    multi = [
        _refusal(await ask(b_only_raw, project_keys="KSUNONE"), "KSUNONE"),
        _refusal(await ask(b_only_raw, project_keys=a_key), a_key),
        _refusal(await ask(w.limited_raw, project_keys=b_key), b_key),
    ]
    assert multi[0][0] == 422
    assert multi[0] == multi[1] == multi[2]

    # Anonymous: the refusal does not depend on the key at all.
    anon_private = await ask(None, project_key=a_key)
    anon_missing = await ask(None, project_key="KSUNONE")
    assert anon_private.status_code == anon_missing.status_code
    assert anon_private.json() == anon_missing.json()
    assert a_key not in anon_private.text


async def test_search_folds_a_forbidden_refusal_into_not_found(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A 403-style refusal from the access check reads as a missing key, not as a 403."""
    from specivo.api.v1 import search as search_api
    from specivo.core.exceptions import PermissionDeniedError

    w = await build_world(db_session, client, "KSF")
    a_key = w.a.key

    async def forbid(_db, _project, _user):
        raise PermissionDeniedError("You may not search this project")

    monkeypatch.setattr(search_api._project_service, "require_project_access", forbid)
    _auth(client, w.open_raw)
    forbidden = await client.get("/api/v1/search/", params={"q": "quokka", "project_key": a_key})
    missing = await client.get("/api/v1/search/", params={"q": "quokka", "project_key": "KSFNONE"})
    assert _refusal(forbidden, a_key) == _refusal(missing, "KSFNONE")
    assert forbidden.status_code == 422
