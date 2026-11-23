"""GET /api/v1/issues/{ref}/agent-sessions/: an issue's agent sessions with counters and cost, paginated."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.integration.agent_timeline_world import TimelineWorld, build_timeline_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    clear_role_cache(db_session)
    yield await build_timeline_world(db_session, "AIA")
    clear_role_cache(db_session)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _url(ref: str) -> str:
    return f"/api/v1/issues/{ref}/agent-sessions/"


async def test_list_for_a_manager(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(_url(w.pub.display_key), headers=_auth(w.manager))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] == 1
    [item] = body["items"]
    assert item["id"] == w.old.id
    assert item["session_ids"] == [w.old.id]
    assert item["user_login"] == w.agent.login
    assert item["service_account"] is True
    assert item["state"] == "completed"
    assert item["tool_calls"] == 3
    assert item["approvals_requested"] == 1
    assert item["approvals_resolved"] == {"deny": 1}
    assert sorted(item["children"]) == sorted([w.live.id, t.sub.id])
    assert item["cost"]["entries"] == 2
    assert item["cost"]["currency"] == "USD"
    assert Decimal(item["cost"]["spend"]["amount"]) == Decimal("1.75")
    assert item["cost"]["spend"]["currency"] == "USD"
    assert item["cost"]["profiles"] == ["night"]
    assert item["cost"]["unattended"] is True


async def test_sub_issues_and_pagination(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(_url(w.pub.display_key), params={"sub": "true", "limit": 1}, headers=_auth(w.manager))
    body = resp.json()
    assert body["total_count"] == 2
    assert [i["id"] for i in body["items"]] == [w.old.id]
    resp = await client.get(
        _url(w.pub.display_key), params={"sub": "true", "limit": 1, "offset": 1}, headers=_auth(w.manager)
    )
    assert [i["id"] for i in resp.json()["items"]] == [t.child.id]


async def test_own_visibility_gets_no_cost(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(_url(w.pub.display_key), params={"sub": "true"}, headers=_auth(t.dev))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The private sub-issue's session is left out for the lead.
    assert [i["id"] for i in body["items"]] == [w.old.id]
    assert body["items"][0]["cost"] is None


async def test_permissions(client: AsyncClient, world: TimelineWorld) -> None:
    w = world.w
    resp = await client.get(_url(w.pub.display_key), headers=_auth(w.member))
    assert resp.status_code == 403
    # An issue the caller cannot see is 404, whatever the permission.
    resp = await client.get(_url(w.priv.display_key), headers=_auth(w.member))
    assert resp.status_code == 404
    resp = await client.get(_url(w.pub.display_key), headers=_auth(w.outsider))
    assert resp.status_code == 404
