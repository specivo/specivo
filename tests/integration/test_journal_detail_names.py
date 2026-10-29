"""The REST API names what an issue's history refers to, by the issue page's rules.

A journal detail keeps ids; ``old_name`` / ``new_name`` name them, but only
for records the caller may see: a version of a project the caller cannot open,
or a person outside the projects they can reach, keeps a null name, and a
parent change to an issue they cannot see is not returned at all. An API key
limited to some projects is shown names from those projects only, and a
visitor without an account follows the anonymous rules.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import CF_USER, CF_VERSION, Journal, JournalDetail
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import Permission, clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.factories.version import VersionFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def _project(db: AsyncSession, key: str, *, is_public: bool = False) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}", is_public=is_public)
    db.add(project)
    await db.flush()
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db.flush()
    return project


async def _issue(db: AsyncSession, project: Project, lookups: tuple, author: User, number: int) -> Issue:
    tracker, status, priority = lookups
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=number,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        subject=f"{project.key} issue {number}",
    )
    db.add(issue)
    await db.flush()
    return issue


async def _join(db: AsyncSession, user: User, project: Project, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def build_history(db: AsyncSession, reader: User, prefix: str = "JDN") -> dict[str, Any]:
    """An issue of project <prefix>A, whose reader may open only that project, with history naming <prefix>B too."""
    first = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    second = StatusFactory.build(name=f"{prefix} Doing", position=2, category="active")
    db.add_all([first, second])
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Bug", default_status_id=first.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()
    lookups = (tracker, first, priority)

    mine, theirs = await _project(db, f"{prefix}A"), await _project(db, f"{prefix}B")
    role = Role(
        name=f"{prefix} Reader",
        permissions=["view_issues"],
        builtin=0,
        users_visibility="members_of_visible_projects",
    )
    db.add(role)
    await db.flush()
    await _join(db, reader, mine, role)
    stranger = UserFactory.build(login=f"{prefix.lower()}_stranger", display_name=f"{prefix} Stranger")
    db.add(stranger)
    await db.flush()
    await _join(db, stranger, theirs, role)

    own_version = VersionFactory.build(project_id=mine.id, name=f"{prefix} own release")
    their_version = VersionFactory.build(project_id=theirs.id, name=f"{prefix} secret release", sharing="system")
    db.add_all([own_version, their_version])
    await db.flush()

    issue = await _issue(db, mine, lookups, reader, 1)
    own_parent = await _issue(db, mine, lookups, reader, 2)
    their_parent = await _issue(db, theirs, lookups, reader, 1)
    journal = Journal(issue_id=issue.id, project_id=mine.id, user_id=reader.id, sequence=2)
    db.add(journal)
    await db.flush()
    changes = [
        ("attr", "status_id", first.id, second.id),
        ("attr", "fixed_version_id", their_version.id, own_version.id),
        ("attr", "assigned_to_id", stranger.id, reader.id),
        ("attr", "parent_id", own_parent.display_key, None),
        ("attr", "parent_id", None, their_parent.display_key),
        (CF_USER, "reviewer", stranger.id, reader.id),
        (CF_VERSION, "found_in", their_version.id, own_version.id),
        ("attr", "subject", "Old", "New"),
    ]
    db.add_all(
        JournalDetail(
            journal_id=journal.id,
            property=prop,
            prop_key=key,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
        )
        for prop, key, old, new in changes
    )
    await db.commit()
    clear_role_cache(db)
    return {
        "issue": issue,
        "mine": mine,
        "theirs": theirs,
        "role": role,
        "stranger": stranger,
        "own_parent": own_parent,
        "their_parent": their_parent,
        "own_version": own_version,
        "their_version": their_version,
        "statuses": (first, second),
    }


async def _details(client: AsyncClient, issue: Issue) -> tuple[dict[tuple[str, str], list[dict]], str]:
    resp = await client.get(f"/api/v1/issues/{issue.display_key}/?include=journals")
    assert resp.status_code == 200, resp.text
    by_field: dict[tuple[str, str], list[dict]] = {}
    for journal in resp.json()["journals"]:
        for detail in journal["details"]:
            by_field.setdefault((detail["property"], detail["prop_key"]), []).append(detail)
    return by_field, resp.text


def _names(detail: dict) -> tuple[str | None, str | None]:
    return detail["old_name"], detail["new_name"]


def _assert_named_for_a_member_of_one_project(details: dict, text: str, world: dict, reader: User) -> None:
    first, second = world["statuses"]
    assert [_names(d) for d in details[("attr", "status_id")]] == [(first.name, second.name)]
    # The other project's version keeps its id and no name; the reader's own is named.
    [version] = details[("attr", "fixed_version_id")]
    assert (version["old_value"], version["old_name"]) == (str(world["their_version"].id), None)
    assert version["new_name"] == world["own_version"].name
    # People by display name, and only those the reader may see.
    assert [_names(d) for d in details[("attr", "assigned_to_id")]] == [(None, reader.display_name)]
    assert [_names(d) for d in details[(CF_USER, "reviewer")]] == [(None, reader.display_name)]
    assert [_names(d) for d in details[(CF_VERSION, "found_in")]] == [(None, world["own_version"].name)]
    # A plain value has no name.
    assert [_names(d) for d in details[("attr", "subject")]] == [(None, None)]
    # A parent the reader may not see is left out, as on the issue page.
    assert [(d["old_value"], d["new_value"]) for d in details[("attr", "parent_id")]] == [
        (world["own_parent"].display_key, None)
    ]
    for secret in (world["their_version"].name, world["stranger"].display_name, world["stranger"].login):
        assert secret not in text
    assert world["their_parent"].display_key not in text


async def test_a_member_is_shown_names_of_what_they_may_see(auth_client: AsyncClient, db_session: AsyncSession):
    reader = auth_client.state.user
    world = await build_history(db_session, reader)
    details, text = await _details(auth_client, world["issue"])
    _assert_named_for_a_member_of_one_project(details, text, world, reader)


async def test_an_api_key_is_shown_what_its_account_may_see(agent_client: AsyncClient, db_session: AsyncSession):
    reader = agent_client.state.user
    world = await build_history(db_session, reader, prefix="JDK")
    details, text = await _details(agent_client, world["issue"])
    _assert_named_for_a_member_of_one_project(details, text, world, reader)


async def test_a_key_limited_to_some_projects_is_named_nothing_from_the_others(
    client: AsyncClient, db_session: AsyncSession
):
    """The account may open both projects; the key it reads with is limited to one."""
    reader = UserFactory.build(login="jds_reader", display_name="JDS Reader", status="active")
    db_session.add(reader)
    await db_session.flush()
    world = await build_history(db_session, reader, prefix="JDS")
    await _join(db_session, reader, world["theirs"], world["role"])
    service = ApiKeyService()
    scoped, scoped_raw = await service.create_key(session=db_session, user_id=reader.id, name="jds-scoped")
    scoped.scopes = {"projects": [world["mine"].key]}
    _open, open_raw = await service.create_key(session=db_session, user_id=reader.id, name="jds-open")
    await db_session.commit()
    clear_role_cache(db_session)

    client.headers["Authorization"] = f"Bearer {open_raw}"
    details, _ = await _details(client, world["issue"])
    assert details[("attr", "fixed_version_id")][0]["old_name"] == world["their_version"].name
    assert len(details[("attr", "parent_id")]) == 2

    client.headers["Authorization"] = f"Bearer {scoped_raw}"
    details, text = await _details(client, world["issue"])
    assert details[("attr", "fixed_version_id")][0]["old_name"] is None
    assert details[(CF_VERSION, "found_in")][0]["old_name"] is None
    assert [(d["old_value"], d["new_value"]) for d in details[("attr", "parent_id")]] == [
        (world["own_parent"].display_key, None)
    ]
    assert world["their_version"].name not in text
    assert world["their_parent"].display_key not in text


async def test_a_visitor_without_an_account_follows_the_anonymous_rules(client: AsyncClient, db_session: AsyncSession):
    """No relation or parent change; people named by display name, only members of projects it may open."""
    admin = AdminUserFactory.build(login="jdp_admin", status="active")
    visible = UserFactory.build(login="jdp_visible", display_name="JDP Visible", status="active")
    outsider = UserFactory.build(login="jdp_outsider", display_name="JDP Outsider", status="active")
    db_session.add_all([admin, visible, outsider])
    await db_session.flush()
    status = StatusFactory.build(name="JDP New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="JDP Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="JDP Normal", position=1)
    db_session.add_all([tracker, priority])
    await db_session.flush()
    public, private = await _project(db_session, "JDPPUB", is_public=True), await _project(db_session, "JDPPRIV")
    db_session.add_all(
        [Member(user_id=visible.id, project_id=public.id), Member(user_id=outsider.id, project_id=private.id)]
    )
    issue = await _issue(db_session, public, (tracker, status, priority), admin, 1)
    sibling = await _issue(db_session, public, (tracker, status, priority), admin, 2)
    journal = Journal(issue_id=issue.id, project_id=public.id, user_id=admin.id, sequence=2)
    db_session.add(journal)
    await db_session.flush()
    db_session.add_all(
        [
            JournalDetail(
                journal_id=journal.id,
                property="attr",
                prop_key="assigned_to_id",
                old_value=str(outsider.id),
                new_value=str(visible.id),
            ),
            JournalDetail(
                journal_id=journal.id,
                property=CF_USER,
                prop_key="reviewer",
                old_value=str(outsider.id),
                new_value=str(visible.id),
            ),
            JournalDetail(journal_id=journal.id, property="attr", prop_key="parent_id", new_value=sibling.display_key),
            JournalDetail(journal_id=journal.id, property="relation", prop_key="relates", new_value="JDPPRIV-1"),
        ]
    )
    await db_session.commit()
    await set_anonymous_permissions(db_session, public, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[public.key])
    await db_session.commit()
    clear_role_cache(db_session)

    details, text = await _details(client, issue)
    assert set(details) == {("attr", "assigned_to_id"), (CF_USER, "reviewer")}
    assert [_names(d) for d in details[("attr", "assigned_to_id")]] == [(None, "JDP Visible")]
    assert [_names(d) for d in details[(CF_USER, "reviewer")]] == [(None, "JDP Visible")]
    assert "JDP Outsider" not in text and "jdp_outsider" not in text
    assert "JDPPRIV-1" not in text
