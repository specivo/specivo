"""Search depth is bounded at every entry point and inside the service.

Keyword search ranks every row up to the end of the requested page in each
part of its union, so an unbounded offset (or limit) would let one cheap
request make the database rank and sort arbitrarily many rows.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_SEARCH_OFFSET, MCP_SEARCH_MAX_LIMIT
from specivo.models.user import User
from specivo.services import search_service
from specivo.services.search_service import SearchService

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SEARCH_URL = "/api/v1/search/"


class TestRestApi:
    async def test_offset_over_cap_is_rejected(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get(SEARCH_URL, params={"q": "budget", "offset": MAX_SEARCH_OFFSET + 1})
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["field"] == "offset"

    @pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
    async def test_offset_at_cap_still_works(self, auth_client: AsyncClient, mode: str) -> None:
        resp = await auth_client.get(SEARCH_URL, params={"q": "budget", "mode": mode, "offset": MAX_SEARCH_OFFSET})
        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []


class TestWebPage:
    async def test_offset_over_cap_renders(self, auth_client: AsyncClient) -> None:
        resp = await auth_client.get(
            "/search/",
            params={"q": "budget", "mode": "keyword", "offset": 10_000_000},
            cookies={"access_token": auth_client.state.token},
        )
        assert resp.status_code == 200


class TestMcpTool:
    async def test_limit_is_clamped(
        self, db_session: AsyncSession, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from specivo.mcp import tools

        seen: dict[str, Any] = {}

        async def _spy(session: AsyncSession, query: str, **kwargs: Any) -> tuple[list, int, dict]:
            seen.update(kwargs)
            return [], 0, {}

        monkeypatch.setattr(tools._search_svc, "search", _spy)
        user: User = admin_client.state.user
        await tools._search(db_session, user, "budget", limit=10_000_000, mode="keyword")
        assert seen["limit"] == MCP_SEARCH_MAX_LIMIT


class TestService:
    async def test_part_limit_is_capped_for_any_caller(
        self, db_session: AsyncSession, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict[str, Any]] = []
        real_execute = db_session.execute

        async def _capture(statement: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
            if isinstance(params, dict) and "part_limit" in params:
                captured.append(dict(params))
            return await real_execute(statement, params, *args, **kwargs)

        monkeypatch.setattr(db_session, "execute", _capture)
        user: User = admin_client.state.user
        results, _, _ = await SearchService().search(db_session, "budget", user=user, offset=10_000_000, limit=25)

        assert results == []
        assert captured
        assert max(p["part_limit"] for p in captured) <= search_service._MAX_PART_ROWS
