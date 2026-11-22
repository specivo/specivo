"""RoleService: create (blank and copy), edit, reorder and delete roles, with every refusal.

Role names are unique database-wide and modules run in parallel inside
uncommitted transactions, so names here carry a module-local prefix.
"""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import ROLE_SYSTEM_KEY_AUTOMATION, Role, RoleBuiltin
from specivo.models.user_group import UserGroup
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition
from specivo.schemas.role import RoleCreate, RoleUpdate
from specivo.services.repository_service import automation_role
from specivo.services.role_service import (
    RoleInUseError,
    RoleNameTakenError,
    RolePermissionNotAllowedError,
    RoleProtectedError,
    RoleService,
)
from tests.factories.lookups import StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = RoleService()
_PREFIX = "rsvc"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


async def _role(db: AsyncSession, permissions: list[str] | None = None, **kwargs: object) -> Role:
    role = Role(name=_name("Role"), permissions=permissions or ["view_issues"], builtin=0, **kwargs)
    db.add(role)
    await db.flush()
    return role


async def _non_member(db: AsyncSession) -> Role:
    return (await db.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()


async def _project(db: AsyncSession) -> Project:
    n = next(_counter)
    project = ProjectFactory.build(key=f"RSVC{n}", identifier=f"rsvc-{n}", name=f"Role service {n}")
    db.add(project)
    await db.flush()
    return project


async def _user_membership(db: AsyncSession, role: Role, project: Project) -> MemberRole:
    user = UserFactory.build(login=_name("user"))
    db.add(user)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    member_role = MemberRole(member_id=member.id, role_id=role.id)
    db.add(member_role)
    await db.flush()
    return member_role


@pytest_asyncio.fixture
async def workflow(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssueStatus]:
    tracker = TrackerFactory.build(name=_name("Tracker"))
    new = StatusFactory.build(name=_name("New"))
    done = StatusFactory.build(name=_name("Done"))
    db_session.add_all([tracker, new, done])
    await db_session.flush()
    return tracker, new, done


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_roles_puts_builtin_last(db_session: AsyncSession) -> None:
    role = await _role(db_session, position=10_000)
    roles = await _svc.list_roles(db_session)
    assert roles[-1].builtin == RoleBuiltin.NON_MEMBER
    assert roles[-2].id == role.id


async def test_get_unknown_raises(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await _svc.get(db_session, 987_654_321)


async def test_usage_and_counts(db_session: AsyncSession) -> None:
    role = await _role(db_session)
    project = await _project(db_session)
    await _user_membership(db_session, role, project)
    await _user_membership(db_session, role, project)

    usage = await _svc.usage(db_session, role.id)
    assert usage.membership_count == 2
    assert usage.projects == [{"id": project.id, "key": project.key, "name": project.name}]
    assert (await _svc.membership_counts(db_session))[role.id] == 2


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_blank(db_session: AsyncSession) -> None:
    name = _name("Blank")
    role, details = await _svc.create(
        db_session, RoleCreate(name=f"  {name} ", permissions=["delete_wiki_pages"], assignable=False)
    )
    assert role.name == name
    assert role.builtin == RoleBuiltin.CUSTOM
    assert role.system_key is None
    assert role.assignable is False
    assert role.permissions == ["view_wiki", "manage_wiki", "delete_wiki_pages"]
    assert role.users_visibility == "all"
    assert role.time_entries_visibility == "all"
    assert role.ai_usage_visibility == "own"
    max_position = await db_session.scalar(select(func.max(Role.position)).where(Role.builtin == 0))
    assert role.position == max_position
    assert details == {
        "role_name": name,
        "permissions": ["view_wiki", "manage_wiki", "delete_wiki_pages"],
        "copied_from": None,
        "workflow_transitions_copied": 0,
        "field_rules_copied": 0,
    }


async def test_create_full_access_is_stored_alone(db_session: AsyncSession) -> None:
    role, _ = await _svc.create(db_session, RoleCreate(name=_name("Full"), permissions=["view_issues", "*"]))
    assert role.permissions == ["*"]


async def test_create_unknown_permission_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(ValidationError) as excinfo:
        await _svc.create(db_session, RoleCreate(name=_name("Bad"), permissions=["fly"]))
    assert excinfo.value.field == "permissions"


async def test_create_copy_takes_permissions_visibility_and_workflow(
    db_session: AsyncSession, workflow: tuple[Tracker, IssueStatus, IssueStatus]
) -> None:
    tracker, new, done = workflow
    source = await _role(
        db_session,
        permissions=["view_issues", "add_issues"],
        assignable=False,
        issues_visibility="own",
        users_visibility="members_of_visible_projects",
        time_entries_visibility="own",
        ai_usage_visibility="all",
        settings={"marker": True},
    )
    db_session.add_all(
        [
            WorkflowTransition(tracker_id=tracker.id, role_id=source.id, old_status_id=new.id, new_status_id=done.id),
            WorkflowTransition(tracker_id=tracker.id, role_id=source.id, old_status_id=done.id, new_status_id=new.id),
            WorkflowFieldRule(
                tracker_id=tracker.id, role_id=source.id, status_id=new.id, field_name="due_date", rule="required"
            ),
        ]
    )
    await db_session.flush()

    role, details = await _svc.create(db_session, RoleCreate(name=_name("Copy"), copy_from_role_id=source.id))

    assert role.permissions == ["view_issues", "add_issues"]
    assert role.assignable is False
    assert (role.issues_visibility, role.users_visibility) == ("own", "members_of_visible_projects")
    assert (role.time_entries_visibility, role.ai_usage_visibility) == ("own", "all")
    assert role.settings == {}
    assert details["copied_from"] == {"id": source.id, "name": source.name}
    assert details["workflow_transitions_copied"] == 2
    assert details["field_rules_copied"] == 1
    assert await _svc.workflow_counts(db_session, role.id) == (2, 1)
    assert await _svc.workflow_counts(db_session, source.id) == (2, 1)


async def test_copy_of_automation_does_not_take_the_system_key(db_session: AsyncSession) -> None:
    automation = await automation_role(db_session)
    assert automation is not None
    role, _ = await _svc.create(db_session, RoleCreate(name=_name("AutoCopy"), copy_from_role_id=automation.id))
    assert role.system_key is None
    assert (await automation_role(db_session)).id == automation.id  # type: ignore[union-attr]


async def test_copy_of_non_member_is_custom_and_assignable(db_session: AsyncSession) -> None:
    non_member = await _non_member(db_session)
    role, _ = await _svc.create(
        db_session, RoleCreate(name=_name("NMCopy"), copy_from_role_id=non_member.id, assignable=False)
    )
    assert role.builtin == RoleBuiltin.CUSTOM
    assert role.assignable is True
    assert role.permissions == list(non_member.permissions)


async def test_copy_from_unknown_role_is_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await _svc.create(db_session, RoleCreate(name=_name("Orphan"), copy_from_role_id=987_654_321))


@pytest.mark.parametrize("variant", ["exact", "upper", "anonymous"])
async def test_create_name_clash_is_refused(db_session: AsyncSession, variant: str) -> None:
    existing = await _role(db_session)
    name = {"exact": existing.name, "upper": existing.name.upper(), "anonymous": "ANONYMOUS"}[variant]
    with pytest.raises(RoleNameTakenError) as excinfo:
        await _svc.create(db_session, RoleCreate(name=name))
    assert excinfo.value.status_code == 409
    assert excinfo.value.field == "name"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_rename_and_diff(db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["view_issues", "add_issues"])
    old_name, version = role.name, role.lock_version
    new_name = _name("Renamed")
    updated, changes = await _svc.update(
        db_session,
        role.id,
        RoleUpdate(
            lock_version=version,
            name=new_name,
            assignable=False,
            permissions=["view_issues", "delete_wiki_pages"],
            users_visibility="members_of_visible_projects",
            time_entries_visibility="all",
        ),
    )
    assert updated.name == new_name
    assert updated.lock_version == version + 1
    assert changes == {
        "name": {"old": old_name, "new": new_name},
        "assignable": {"old": True, "new": False},
        "permissions": {"added": ["view_wiki", "manage_wiki", "delete_wiki_pages"], "removed": ["add_issues"]},
        "users_visibility": {"old": "all", "new": "members_of_visible_projects"},
    }


async def test_noop_update_changes_nothing(db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["add_issues", "view_issues"])
    version = role.lock_version
    _, changes = await _svc.update(
        db_session,
        role.id,
        RoleUpdate(lock_version=version, name=role.name, permissions=["view_issues", "add_issues"]),
    )
    assert changes == {}
    assert role.lock_version == version


async def test_full_access_toggle_is_in_the_diff(db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["view_issues"])
    updated, changes = await _svc.update(
        db_session, role.id, RoleUpdate(lock_version=role.lock_version, permissions=["*"])
    )
    assert updated.permissions == ["*"]
    assert changes == {
        "full_access": {"old": False, "new": True},
        "permissions": {"added": [], "removed": ["view_issues"]},
    }


async def test_stale_lock_version_is_a_conflict(db_session: AsyncSession) -> None:
    role = await _role(db_session)
    with pytest.raises(ConflictError):
        await _svc.update(db_session, role.id, RoleUpdate(lock_version=role.lock_version + 1, name=_name("Late")))


async def test_rename_onto_a_taken_name_is_refused(db_session: AsyncSession) -> None:
    role = await _role(db_session)
    other = await _role(db_session)
    with pytest.raises(RoleNameTakenError):
        await _svc.update(db_session, role.id, RoleUpdate(lock_version=role.lock_version, name=other.name.upper()))


async def test_builtin_rename_and_assignable_are_refused(db_session: AsyncSession) -> None:
    non_member = await _non_member(db_session)
    with pytest.raises(ValidationError) as excinfo:
        await _svc.update(db_session, non_member.id, RoleUpdate(lock_version=non_member.lock_version, name=_name("NM")))
    assert excinfo.value.field == "name"
    with pytest.raises(ValidationError) as excinfo:
        await _svc.update(db_session, non_member.id, RoleUpdate(lock_version=non_member.lock_version, assignable=True))
    assert excinfo.value.field == "assignable"


async def test_non_member_ceiling_is_refused_not_stripped(db_session: AsyncSession) -> None:
    non_member = await _non_member(db_session)
    before = list(non_member.permissions)
    with pytest.raises(RolePermissionNotAllowedError) as excinfo:
        await _svc.update(
            db_session,
            non_member.id,
            RoleUpdate(lock_version=non_member.lock_version, permissions=["view_issues", "delete_issues"]),
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.details == {"not_allowed": ["delete_issues"]}
    assert non_member.permissions == before

    with pytest.raises(RolePermissionNotAllowedError):
        await _svc.update(
            db_session, non_member.id, RoleUpdate(lock_version=non_member.lock_version, permissions=["*"])
        )


async def test_non_member_within_ceiling_is_saved(db_session: AsyncSession) -> None:
    non_member = await _non_member(db_session)
    updated, changes = await _svc.update(
        db_session,
        non_member.id,
        RoleUpdate(lock_version=non_member.lock_version, permissions=["manage_wiki", "view_issues"]),
    )
    assert set(updated.permissions) == {"view_issues", "view_wiki", "manage_wiki"}
    assert changes["permissions"]["added"] == ["view_wiki", "manage_wiki"]


async def test_automation_flag_survives_a_rename(db_session: AsyncSession) -> None:
    automation = await automation_role(db_session)
    assert automation is not None and automation.system_key == ROLE_SYSTEM_KEY_AUTOMATION
    new_name = _name("Merge bot")
    await _svc.update(db_session, automation.id, RoleUpdate(lock_version=automation.lock_version, name=new_name))
    found = await automation_role(db_session)
    assert found is not None
    assert (found.id, found.name) == (automation.id, new_name)


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------


async def test_move_renumbers_densely_and_swaps(db_session: AsyncSession) -> None:
    a = await _role(db_session, position=50_000)
    b = await _role(db_session, position=50_000)
    c = await _role(db_session, position=50_000)
    versions = {r.id: r.lock_version for r in (a, b, c)}

    roles = await _svc.move(db_session, c.id, "up")
    ids = [r.id for r in roles]
    assert [r.position for r in roles] == list(range(1, len(roles) + 1))
    assert ids[-3:] == [a.id, c.id, b.id]
    assert all(r.builtin == RoleBuiltin.CUSTOM for r in roles)

    roles = await _svc.move(db_session, a.id, "down")
    assert [r.id for r in roles][-3:] == [c.id, a.id, b.id]
    # Reordering never bumps lock_version, so an open editor is not broken.
    assert {r.id: r.lock_version for r in roles if r.id in versions} == versions


async def test_move_past_the_end_is_a_noop(db_session: AsyncSession) -> None:
    last = await _role(db_session, position=60_000)
    roles = await _svc.move(db_session, last.id, "down")
    assert roles[-1].id == last.id
    first_id = roles[0].id
    roles = await _svc.move(db_session, first_id, "up")
    assert roles[0].id == first_id


async def test_move_builtin_is_a_noop(db_session: AsyncSession) -> None:
    non_member = await _non_member(db_session)
    roles = await _svc.move(db_session, non_member.id, "up")
    assert non_member.id not in [r.id for r in roles]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_cascades_workflow_rows(
    db_session: AsyncSession, workflow: tuple[Tracker, IssueStatus, IssueStatus]
) -> None:
    tracker, new, done = workflow
    role = await _role(db_session, permissions=["view_issues"])
    db_session.add(
        WorkflowTransition(tracker_id=tracker.id, role_id=role.id, old_status_id=new.id, new_status_id=done.id)
    )
    db_session.add(
        WorkflowFieldRule(
            tracker_id=tracker.id, role_id=role.id, status_id=new.id, field_name="due_date", rule="readonly"
        )
    )
    await db_session.flush()
    role_id, name = role.id, role.name

    facts = await _svc.delete(db_session, role_id)

    assert facts == {
        "role_name": name,
        "permissions": ["view_issues"],
        "workflow_transitions_removed": 1,
        "field_rules_removed": 1,
    }
    assert await db_session.scalar(select(func.count()).select_from(Role).where(Role.id == role_id)) == 0
    assert await _svc.workflow_counts(db_session, role_id) == (0, 0)


async def test_delete_builtin_is_protected(db_session: AsyncSession) -> None:
    non_member = await _non_member(db_session)
    with pytest.raises(RoleProtectedError) as excinfo:
        await _svc.delete(db_session, non_member.id)
    assert excinfo.value.details == {"reason": "builtin"}


async def test_delete_automation_is_protected(db_session: AsyncSession) -> None:
    automation = await automation_role(db_session)
    assert automation is not None
    with pytest.raises(RoleProtectedError) as excinfo:
        await _svc.delete(db_session, automation.id)
    assert excinfo.value.details == {"reason": "automation"}


@pytest.mark.parametrize("holder", ["user", "group", "inherited"])
async def test_delete_in_use_is_refused(db_session: AsyncSession, holder: str) -> None:
    role = await _role(db_session)
    project = await _project(db_session)
    if holder == "group":
        group = UserGroup(name=_name("Group"))
        db_session.add(group)
        await db_session.flush()
        member = Member(group_id=group.id, project_id=project.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    else:
        member_role = await _user_membership(db_session, role, project)
        if holder == "inherited":
            member_role.inherited_from = member_role.id
    await db_session.flush()

    with pytest.raises(RoleInUseError) as excinfo:
        await _svc.delete(db_session, role.id)
    error: AppError = excinfo.value
    assert error.status_code == 409
    assert error.details == {
        "membership_count": 1,
        "projects": [{"id": project.id, "key": project.key, "name": project.name}],
    }
