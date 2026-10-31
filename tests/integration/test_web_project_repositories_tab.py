"""The project settings page renders the Repositories tab with the connected repositories, never a secret."""

from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.services.auth_service import _make_access_token
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory
from tests.fixtures.forge_hooks import SECRET, make_integration

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


class _XData(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        x_data = dict(attrs).get("x-data") or ""
        if x_data.startswith("projectRepositories("):
            self.found.append(x_data)


async def test_tab_lists_repositories_without_secret(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = AdminUserFactory.build(login="rweb_admin", status="active")
    project = ProjectFactory.build(key="RWEB", identifier="rweb", name="Repositories page")
    db_session.add_all([admin, project])
    await db_session.commit()
    integration = await make_integration(db_session, project, provider="forgejo")

    cookies = {"access_token": _make_access_token(admin, get_settings())}
    page = await client.get(f"/projects/{project.key}/settings/", cookies=cookies)
    assert page.status_code == 200
    html = page.text
    assert "tab === 'repositories'" in html
    assert SECRET not in html

    parser = _XData()
    parser.feed(html)
    (x_data,) = parser.found
    assert str(integration.public_id) in x_data
    assert '"forgejo"' in x_data
    data = json.loads(x_data[x_data.index("repositories: ") + len("repositories: ") : x_data.index(", providers:")])
    assert [r["repo_full_name"] for r in data] == ["example-org/widgets"]
    assert data[0]["hook_url"].endswith(f"/hooks/forgejo/{integration.public_id}/")


async def test_tab_offers_issue_changes_with_the_email_warning(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = AdminUserFactory.build(login="rweb_admin2", status="active")
    project = ProjectFactory.build(key="RWEC", identifier="rwec", name="Repository options")
    db_session.add_all([admin, project])
    await db_session.commit()

    cookies = {"access_token": _make_access_token(admin, get_settings())}
    html = (await client.get(f"/projects/{project.key}/settings/", cookies=cookies)).text
    assert 'data-testid="repository-options"' in html
    assert "Commit commands trust the commit author email" in html
    assert 'data-testid="repository-merge-automation"' in html
    assert 'data-testid="branch-name-settings"' in html

    parser = _XData()
    parser.feed(html)
    (x_data,) = parser.found
    # The Automation role is offered for $git even though it is not assignable.
    assert '"Automation"' in x_data
    assert "canPickRole: true" in x_data
