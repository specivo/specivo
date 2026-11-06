"""A key limited to some projects never reads development activity of an issue in another project.

The world of ``test_api_key_project_scope``: the owner manages A and B; the
limited key reaches A only. A repository connected to B links a commit and a
CI run to B's issue.
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.models.project import Project
from specivo.services.repository_hooks import process_delivery
from tests.fixtures import forge_activity as fa
from tests.fixtures.forge_hooks import hook_path, make_integration, push_payload, signed_headers
from tests.integration.test_mcp_api_key_project_scope import _as, _refused, _world, use_test_session  # noqa: F401

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


async def _deliver(client: AsyncClient, db: AsyncSession, integration, event: str, payload: dict, queued: list) -> None:
    body = json.dumps(payload).encode()
    headers = signed_headers("github", body, event=event, delivery=str(uuid.uuid4()))
    resp = await client.post(hook_path(integration), content=body, headers=headers)
    assert resp.status_code == 202, resp.text
    await process_delivery(db, queued[-1])
    await db.commit()


async def test_limited_key_reads_no_activity_of_another_project(
    use_test_session,  # noqa: F811
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    queued: list[int] = []
    monkeypatch.setattr("specivo.hooks.router.queue_processing", lambda ids: queued.extend(ids) or len(queued))
    w = await _world(db_session, client, "KDV")
    b = await db_session.get(Project, w.b.id)
    b_ref, a_ref = w.b_issue["key"], w.a_issue["key"]
    integration = await make_integration(db_session, b)
    message = f"{b_ref} quokka build"
    data = push_payload("github", [{**_commit(message), "id": fa.SHA_A}], branch="feat/x")
    await _deliver(client, db_session, integration, "push", data, queued)
    await _deliver(client, db_session, integration, "workflow_run", fa.workflow_run(branch="feat/x"), queued)

    # The open key sees it; the key limited to A does not, over REST or MCP.
    client.headers["Authorization"] = f"Bearer {w.open_raw}"
    opened = await client.get(f"/api/v1/issues/{b_ref}/development/")
    assert opened.status_code == 200 and len(opened.json()["ci_runs"]) == 1
    client.headers["Authorization"] = f"Bearer {w.limited_raw}"
    assert (await client.get(f"/api/v1/issues/{b_ref}/development/")).status_code == 404
    assert (await client.get(f"/api/v1/issues/{a_ref}/development/")).status_code == 200
    client.headers.pop("Authorization", None)

    _as(w.limited_raw)
    assert "not found" in await _refused(server.specivo_issue_development(issue_ref=b_ref))
    assert json.loads(await server.specivo_issue_development(issue_ref=a_ref))["ci_runs"] == []


def _commit(message: str) -> dict:
    from tests.fixtures.forge_hooks import commit

    return commit("scope", message)
