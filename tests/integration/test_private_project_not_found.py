"""A private project a caller cannot reach answers exactly like a key that names no project.

ADR-0004: 404, never 403, so project keys cannot be enumerated. MCP tools and
the project-admin REST routes used to check the permission first and answer
"permission denied" for an existing private project but "not found" for a
missing key. A public project under a private parent also spelled out the
parent's key and path.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.mcp.tools import _list_issues, _list_versions
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class World:
    outsider: User
    reporter: User
    secret: Project
    child: Project


async def _join(db: AsyncSession, project: Project, user: User, permissions: list[str]) -> None:
    role = Role(name=f"PNF {project.key}", permissions=permissions, builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    secret = ProjectFactory.build(key="PNFS", identifier="pnf-secret", is_public=False)
    db.add(secret)
    await db.flush()
    # A public project under the private one.
    child = ProjectFactory.build(key="PNFC", identifier="pnf-child", is_public=True, parent_id=secret.id)
    db.add(child)
    await db.flush()
    db.add_all([EnabledModule(project_id=p.id, name="issue_tracking") for p in (secret, child)])
    outsider = UserFactory.build(login="pnf_outsider", status="active")
    reporter = UserFactory.build(login="pnf_reporter", status="active")
    db.add_all([outsider, reporter])
    await db.flush()
    # A member who may read but not manage.
    await _join(db, secret, reporter, ["view_issues"])
    await db.commit()
    return World(outsider, reporter, secret, child)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _error(coro) -> str:
    with pytest.raises((NotFoundError, PermissionDeniedError)) as exc:
        await coro
    return f"{type(exc.value).__name__}: {exc.value.message}"


async def test_mcp_private_project_reads_as_a_missing_key(db_session: AsyncSession, world: World) -> None:
    for tool in (_list_issues, _list_versions):
        hidden = await _error(tool(db_session, world.outsider, "PNFS"))
        missing = await _error(tool(db_session, world.outsider, "PNFX"))
        assert hidden == "NotFoundError: Project 'PNFS' not found"
        assert missing == "NotFoundError: Project 'PNFX' not found"
    # The key is echoed as written, so lowercase input does not reveal the stored key.
    assert await _error(_list_versions(db_session, world.outsider, "pnfs")) == "NotFoundError: Project 'pnfs' not found"


async def test_mcp_member_still_gets_a_permission_error(db_session: AsyncSession, world: World) -> None:
    # A project the caller can reach keeps answering "permission denied" for a missing permission.
    from specivo.mcp.tools import _create_version

    assert (await _error(_create_version(db_session, world.reporter, "PNFS", "1.0"))).startswith(
        "PermissionDeniedError"
    )


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("patch", "/api/v1/projects/{key}/", {"name": "x"}),
        ("get", "/api/v1/projects/{key}/members/", None),
        ("get", "/api/v1/admin/projects/{key}/metadata-schemas/", None),
        ("get", "/api/v1/projects/{key}/search/fts/", None),
    ],
)
async def test_rest_private_project_reads_as_a_missing_key(
    client: AsyncClient, db_session: AsyncSession, world: World, method: str, url: str, body: dict | None
) -> None:
    kwargs = {"headers": _auth(world.outsider)} | ({"json": body} if body is not None else {})
    hidden = await getattr(client, method)(url.format(key="PNFS"), **kwargs)
    missing = await getattr(client, method)(url.format(key="PNFX"), **kwargs)
    assert (hidden.status_code, missing.status_code) == (404, 404), (hidden.text, missing.text)
    assert hidden.json()["errors"][0]["message"] == "Project 'PNFS' not found"
    # No "permission denied" audit row for probing a project the caller cannot reach.
    count = await db_session.scalar(
        select(func.count()).select_from(SecurityAuditLog).where(SecurityAuditLog.project_id == world.secret.id)
    )
    assert count == 0


async def test_rest_member_without_manage_still_gets_403(client: AsyncClient, world: World) -> None:
    resp = await client.patch("/api/v1/projects/PNFS/", json={"name": "x"}, headers=_auth(world.reporter))
    assert resp.status_code == 403, resp.text


async def test_public_child_hides_its_private_parent(client: AsyncClient, world: World) -> None:
    resp = await client.get("/api/v1/projects/PNFC/", headers=_auth(world.outsider))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["parent_id"], body["parent_key"], body["path"]) == (None, None, None)

    listed = await client.get("/api/v1/projects/", headers=_auth(world.outsider), params={"limit": 200})
    row = next(p for p in listed.json()["items"] if p["key"] == "PNFC")
    assert (row["parent_key"], row["path"]) == (None, None)

    # A member of the parent sees the lineage.
    body = (await client.get("/api/v1/projects/PNFC/", headers=_auth(world.reporter))).json()
    assert body["parent_key"] == "PNFS" and body["parent_id"] == world.secret.id and body["path"]
