"""MCP wiki tools respect the wiki module, and list paging is bounded."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.mcp import server
from specivo.mcp.tools import _create_wiki, _list_wiki_pages, _read_wiki
from specivo.models.project import EnabledModule
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def test_wiki_tools_refuse_a_project_without_the_wiki_module(db_session: AsyncSession) -> None:
    admin = AdminUserFactory.build(login="mwm_admin", status="active")
    project = ProjectFactory.build(key="MWM", identifier="mwm")
    db_session.add_all([admin, project])
    await db_session.flush()
    await WikiService().create_page(db_session, project.id, title="Left over", text="x", author=admin)
    await db_session.commit()

    with pytest.raises(PermissionDeniedError, match="Wiki module is not enabled"):
        await _read_wiki(db_session, admin, "MWM", "left-over")
    with pytest.raises(PermissionDeniedError, match="Wiki module is not enabled"):
        await _list_wiki_pages(db_session, admin, "MWM")
    with pytest.raises(PermissionDeniedError, match="Wiki module is not enabled"):
        await _create_wiki(db_session, admin, "MWM", "New page", "text")

    db_session.add(EnabledModule(project_id=project.id, name="wiki"))
    await db_session.commit()
    assert "Left over" in await _read_wiki(db_session, admin, "MWM", "left-over")


@pytest.mark.parametrize("tool", ["specivo_list_projects", "specivo_list_issues"])
async def test_list_paging_is_bounded_in_the_tool_schema(tool: str) -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}
    props = tools[tool].input_schema["properties"]
    assert (props["limit"]["minimum"], props["limit"]["maximum"]) == (1, 100)
    assert props["offset"]["minimum"] == 0
