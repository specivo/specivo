"""Wiki section tools keep code blocks whole; restore and reparenting hold up."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import ValidationError
from specivo.mcp.tools import _read_wiki, _replace_wiki_section, _restore_wiki, _update_wiki_metadata
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

PAGE = "## Install\n```bash\n# install deps\npip install specivo\n```\n## Usage\nRun it.\n"


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> tuple[User, Project]:
    admin = AdminUserFactory.build(login="wsr_admin", status="active")
    project = ProjectFactory.build(key="WSR", identifier="wsr")
    db_session.add_all([admin, project])
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="wiki"))
    await db_session.commit()
    return admin, project


async def test_replacing_a_section_keeps_the_code_block_and_what_follows(db_session: AsyncSession, world) -> None:
    admin, project = world
    await WikiService().create_page(db_session, project.id, title="Setup", text=PAGE, author=admin)
    await db_session.commit()

    await _replace_wiki_section(db_session, admin, "WSR", "setup", "## Install", "Use the installer.")

    text = await _read_wiki(db_session, admin, "WSR", "setup")
    assert "Use the installer." in text
    assert "## Usage\nRun it." in text
    assert "# install deps" not in text and "```" not in text


async def test_a_slug_deleted_twice_restores_its_latest_page(
    client: AsyncClient, db_session: AsyncSession, world
) -> None:
    admin, project = world
    svc = WikiService()
    for text in ("first runbook", "second runbook"):
        page, _ = await svc.create_page(db_session, project.id, title="Runbook", text=text, author=admin)
        await svc.delete_page(db_session, page.id, deleted_by=admin)
        await db_session.commit()

    await _restore_wiki(db_session, admin, "WSR", "runbook")
    assert "second runbook" in await _read_wiki(db_session, admin, "WSR", "runbook")

    await svc.delete_page(db_session, page.id, deleted_by=admin)
    await db_session.commit()
    resp = await client.post(
        "/api/v1/projects/WSR/wiki/runbook/restore/",
        headers={"Authorization": f"Bearer {_make_access_token(admin, get_settings())}"},
    )
    assert resp.status_code == 200, resp.text


async def test_a_page_cannot_be_moved_under_itself_or_a_subpage(
    client: AsyncClient, db_session: AsyncSession, world
) -> None:
    admin, project = world
    svc = WikiService()
    parent, _ = await svc.create_page(db_session, project.id, title="Parent", text="p", author=admin)
    child, _ = await svc.create_page(
        db_session, project.id, title="Child", text="c", author=admin, parent_slug="parent"
    )
    await db_session.commit()

    with pytest.raises(ValidationError):
        await _update_wiki_metadata(db_session, admin, "WSR", "parent", parent_slug="parent")
    with pytest.raises(ValidationError):
        await _update_wiki_metadata(db_session, admin, "WSR", "parent", parent_slug="child")

    await db_session.refresh(parent)
    resp = await client.patch(
        "/api/v1/projects/WSR/wiki/parent/",
        json={"text": "p", "lock_version": parent.lock_version, "parent_slug": "child"},
        headers={"Authorization": f"Bearer {_make_access_token(admin, get_settings())}"},
    )
    assert resp.status_code == 422, resp.text
    await db_session.refresh(parent)
    assert parent.parent_id is None
