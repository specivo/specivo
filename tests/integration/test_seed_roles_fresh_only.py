"""The seed creates the default roles and workflow transitions once, on a fresh database, and never changes them.

The container entrypoint re-seeds on every start, so an administrator's role
edits must survive it. Serial: the test seeds rows with fixed global names
(Manager, Developer, Bug, New), which parallel modules might also create.
Everything runs inside the test's rolled-back transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli.seed import (
    is_fresh_database,
    seed_non_member_role,
    seed_roles,
    seed_statuses,
    seed_trackers,
    seed_workflow_transitions,
)
from specivo.models.role import Role, RoleBuiltin
from specivo.models.workflow import WorkflowTransition

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]


async def _role(db: AsyncSession, name: str) -> Role | None:
    return (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none()


async def _transitions(db: AsyncSession, role_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(WorkflowTransition).where(WorkflowTransition.role_id == role_id)
        )
        or 0
    )


async def _start_fresh(db: AsyncSession) -> None:
    """Remove every custom role apart from the system ones, inside the test's transaction."""
    await db.execute(delete(Role).where(Role.builtin == RoleBuiltin.CUSTOM, Role.system_key.is_(None)))
    await db.flush()


async def test_system_roles_do_not_make_a_database_non_fresh(db_session: AsyncSession) -> None:
    await _start_fresh(db_session)
    assert await db_session.scalar(select(func.count()).select_from(Role).where(Role.system_key.is_not(None))) == 1
    assert await is_fresh_database(db_session) is True


async def test_fresh_database_gets_the_defaults(db_session: AsyncSession) -> None:
    await _start_fresh(db_session)
    await seed_statuses(db_session)
    await seed_trackers(db_session)

    assert await seed_roles(db_session) is True
    await seed_workflow_transitions(db_session, fresh=True)

    developer = await _role(db_session, "Developer")
    assert developer is not None
    assert "manage_wiki" in developer.permissions
    for name in ("Manager", "Reporter", "Agent"):
        assert await _role(db_session, name) is not None
    assert await _transitions(db_session, developer.id) == 21


async def test_reseeding_never_changes_roles_or_workflows(db_session: AsyncSession) -> None:
    await _start_fresh(db_session)
    await seed_statuses(db_session)
    await seed_trackers(db_session)
    await seed_workflow_transitions(db_session, fresh=await seed_roles(db_session))

    developer = await _role(db_session, "Developer")
    reporter = await _role(db_session, "Reporter")
    agent = await _role(db_session, "Agent")
    assert developer is not None and reporter is not None and agent is not None
    developer.permissions = ["view_issues"]
    developer.issues_visibility = "own"
    reporter.name = "Reporter (renamed)"
    await db_session.delete(agent)
    removed = (
        await db_session.execute(select(WorkflowTransition).where(WorkflowTransition.role_id == developer.id).limit(1))
    ).scalar_one()
    await db_session.delete(removed)
    await db_session.commit()

    for _ in range(2):
        fresh = await seed_roles(db_session)
        assert fresh is False
        await seed_non_member_role(db_session)
        await seed_workflow_transitions(db_session, fresh=fresh)

    await db_session.refresh(developer)
    assert developer.permissions == ["view_issues"]
    assert developer.issues_visibility == "own"
    assert await _role(db_session, "Reporter") is None
    assert await _role(db_session, "Reporter (renamed)") is not None
    assert await _role(db_session, "Agent") is None
    assert await _transitions(db_session, developer.id) == 20
