"""The concept extraction card on the project settings page: managers see the switch, administrators the provider."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.user import TEST_PASSWORD
from tests.integration.concept_helpers import _no_queued_extraction, enable
from tests.integration.link_graph_helpers import World, build_world
from tests.integration.link_suggestion_helpers import add_permission

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_extraction  # the autouse fixture, imported to be active here


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "CXW")


async def _page(client: AsyncClient, login: str, key: str) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    page = await client.get(
        f"/projects/{key}/settings/", headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
    )
    assert page.status_code == 200, page.text
    return page.text


async def test_a_manager_sees_the_switch_but_not_the_provider_form(client, db_session, world):
    await add_permission(db_session, world.reader, world.home, "manage_project")
    html = await _page(client, world.reader.login, world.home.key)
    assert 'data-testid="concept-extraction-settings"' in html
    assert 'data-testid="concept-extraction-provider-form"' not in html
    assert "No AI provider is set for this project" in html


async def test_an_administrator_sees_the_provider_without_its_key(client, db_session, world):
    await enable(db_session, world.home, world.admin)
    html = await _page(client, world.admin.login, world.home.key)
    assert 'data-testid="concept-extraction-provider-form"' in html
    assert "small-model" in html and "models.example.test" in html
    assert "api_key_encrypted" not in html
    assert "0 concepts from 0 issues and pages." in html
