"""Concept extraction over REST: the provider (administrators), the switch (managers), runs, and the links API.

- ``PUT/DELETE /projects/{key}/ai-provider/`` are for instance administrators; the key is never returned
  or audited.
- ``PATCH /projects/{key}/`` switches the extraction on only with a provider and ``manage_project``.
- ``GET /projects/{key}/concept-extraction/`` and ``POST .../run/`` need ``manage_project``.
- ``/links/`` returns ``mentions`` edges with ``kinds=mentions``; low-confidence ones with
  ``include_suggestions=true`` only.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.ai_provider import ProjectAiProvider
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.concept_extraction import ENABLED_BY_SETTING, ENABLED_SETTING, extract_project
from tests.factories.user import TEST_PASSWORD
from tests.integration.concept_helpers import FakeModel, _no_queued_extraction, concept, enable
from tests.integration.link_graph_helpers import World, build_world, grant, issue
from tests.integration.link_suggestion_helpers import add_permission

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_extraction  # the autouse fixture, imported to be active here

PROVIDER = {"base_url": "http://models.example.test/v1", "model": "small-model", "api_key": "sk-very-secret"}


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "CXR")


async def _headers(client: AsyncClient, login: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_only_an_administrator_sets_the_provider_and_the_key_never_comes_back(client, db_session, world):
    await add_permission(db_session, world.reader, world.home, "manage_project")
    manager = await _headers(client, world.reader.login)
    admin = await _headers(client, world.admin.login)
    url = f"/api/v1/projects/{world.home.key}/ai-provider/"

    assert (await client.put(url, json=PROVIDER, headers=manager)).status_code == 403

    resp = await client.put(url, json=PROVIDER, headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_api_key"] is True and body["model"] == "small-model"
    assert "sk-very-secret" not in resp.text
    row = await db_session.scalar(select(ProjectAiProvider).where(ProjectAiProvider.project_id == world.home.id))
    assert row is not None and row.api_key_encrypted and "sk-very-secret" not in row.api_key_encrypted

    audit = (
        await db_session.execute(
            select(SecurityAuditLog).where(SecurityAuditLog.event_type == "project_ai_provider_changed")
        )
    ).scalar_one()
    assert audit.details["api_key"] == "set" and audit.details["host"] == "models.example.test"
    assert "sk-very-secret" not in str(audit.details)

    status = await client.get(f"/api/v1/projects/{world.home.key}/concept-extraction/", headers=manager)
    assert status.status_code == 200, status.text
    assert status.json()["provider"]["has_api_key"] is True
    assert "sk-very-secret" not in status.text

    # Omitting the key keeps it; an empty string removes it.
    kept = await client.put(url, json={**PROVIDER, "api_key": None, "model": "other"}, headers=admin)
    assert kept.json()["has_api_key"] is True
    removed = await client.put(url, json={**PROVIDER, "api_key": ""}, headers=admin)
    assert removed.json()["has_api_key"] is False


@pytest.mark.parametrize("base_url", ["file:///etc/passwd", "https://u:p@models.example.test/v1", "models"])
async def test_a_provider_url_that_is_not_plain_http_is_refused(client, world, base_url):
    admin = await _headers(client, world.admin.login)
    resp = await client.put(
        f"/api/v1/projects/{world.home.key}/ai-provider/", json={**PROVIDER, "base_url": base_url}, headers=admin
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "base_url"


async def test_switching_on_needs_a_provider_and_manage_project(client, db_session, world):
    url = f"/api/v1/projects/{world.home.key}/"
    reader = await _headers(client, world.reader.login)
    assert (await client.patch(url, json={"concept_extraction": True}, headers=reader)).status_code == 403

    await add_permission(db_session, world.reader, world.home, "manage_project")
    resp = await client.patch(url, json={"concept_extraction": True}, headers=reader)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "concept_extraction"

    admin = await _headers(client, world.admin.login)
    await client.put(f"/api/v1/projects/{world.home.key}/ai-provider/", json=PROVIDER, headers=admin)
    resp = await client.patch(
        url,
        json={"concept_extraction": True, "concept_confidence_threshold": 0.7, "concept_extraction_run_limit": 20},
        headers=reader,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["concept_extraction"], body["concept_confidence_threshold"], body["concept_extraction_run_limit"]) == (
        True,
        0.7,
        20,
    )
    project = await db_session.get(Project, world.home.id)
    await db_session.refresh(project)
    assert project.settings[ENABLED_BY_SETTING] == world.reader.id

    bad = await client.patch(url, json={"concept_confidence_threshold": 2}, headers=reader)
    assert bad.status_code == 422


async def test_removing_the_provider_switches_the_extraction_off(client, db_session, world):
    await enable(db_session, world.home, world.admin)
    admin = await _headers(client, world.admin.login)
    url = f"/api/v1/projects/{world.home.key}/ai-provider/"

    assert (await client.delete(url, headers=admin)).status_code == 204
    project = await db_session.get(Project, world.home.id)
    await db_session.refresh(project)
    assert project.settings[ENABLED_SETTING] is False
    assert (await client.delete(url, headers=admin)).status_code == 404


async def test_a_run_is_queued_for_managers_of_an_enabled_project(client, db_session, world, _no_queued_extraction):
    url = f"/api/v1/projects/{world.home.key}/concept-extraction/run/"
    reader = await _headers(client, world.reader.login)
    assert (await client.post(url, headers=reader)).status_code == 403

    await add_permission(db_session, world.reader, world.home, "manage_project")
    assert (await client.post(url, headers=reader)).status_code == 422

    await enable(db_session, world.home, world.admin)
    resp = await client.post(url, headers=reader)
    assert resp.status_code == 202 and resp.json() == {"queued": True}
    assert _no_queued_extraction == [world.home.id]

    hidden = await client.post(f"/api/v1/projects/{world.hidden.key}/concept-extraction/run/", headers=reader)
    assert hidden.status_code == 404
    status = await client.get(f"/api/v1/projects/{world.hidden.key}/concept-extraction/", headers=reader)
    assert status.status_code == 404


async def test_the_links_api_returns_mentions_and_their_suggestions(client, db_session, world):
    await enable(db_session, world.home, world.admin)
    item = await issue(db_session, world, world.home, "Payments", "The Ledger, perhaps the Scheduler.")
    await extract_project(
        db_session,
        world.home.id,
        client=FakeModel(
            answers={
                "Payments": [
                    concept("Ledger", "The Ledger", "feature", 0.9),
                    concept("Scheduler", "the Scheduler", "feature", 0.3),
                ]
            }
        ),
    )
    reader = await _headers(client, world.reader.login)
    url = f"/api/v1/issues/{item.display_key}/links/"

    plain = (await client.get(url, headers=reader)).json()
    assert all(e["kind"] != "mentions" for e in plain["edges"])

    data = (await client.get(url + "?kinds=mentions", headers=reader)).json()
    assert [(e["target"], e["label"], e.get("suggested", False)) for e in data["edges"]] == [
        (f"concept:{world.home.key}/ledger", "0.90", False)
    ]
    assert data["edges"][0]["passage"] == "The Ledger"
    concept_node = next(n for n in data["nodes"] if n["type"] == "concept")
    assert (concept_node["title"], concept_node["status"], concept_node["degree"]) == ("Ledger", "feature", 1)

    asked = (await client.get(url + "?kinds=mentions&include_suggestions=true", headers=reader)).json()
    suggested = [e for e in asked["edges"] if e.get("suggested")]
    assert [(e["kind"], e["label"]) for e in suggested] == [("mentions", "0.30")]


async def test_a_non_member_gets_no_status_of_a_project_they_cannot_reach(client, db_session, world):
    outsider = (await build_world(db_session, "CXQ")).reader
    await grant(db_session, outsider, world.hidden, [])
    headers = await _headers(client, outsider.login)
    resp = await client.get(f"/api/v1/projects/{world.home.key}/concept-extraction/", headers=headers)
    assert resp.status_code == 404
