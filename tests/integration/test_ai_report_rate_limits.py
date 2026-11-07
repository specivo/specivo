"""Per-user budgets for AI usage charts and spend reports (specivo.core.ai_report_limits).

My AI usage (its charts), the project and instance report pages, the report
API (JSON and CSV) and the MCP report tool spend one ``ai_report`` budget per
user; administrators get a higher one. Over it, a page answers with the
rate-limit page and the API with its JSON error, both ``429`` with
``Retry-After``.

The limits are lowered for these tests; what is under test is the wiring.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_report_limits import ai_report_allowed
from specivo.core.config import get_settings
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")


def _redis_available() -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(_REDIS_URL)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 6379), timeout=1):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.serial,
    pytest.mark.skipif(not _redis_available(), reason=f"Redis not available at {_REDIS_URL}"),
    pytest.mark.skipif(
        os.environ.get("PYTEST_XDIST_WORKER") is not None,
        reason="rate limit counters are shared state; run in the serial pass",
    ),
]

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
USER_LIMIT = 3
ADMIN_LIMIT = 5


@dataclass(frozen=True)
class Live:
    member: User
    other: User
    admin: User
    project_key: str


@pytest_asyncio.fixture
async def live(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Live:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "ai_report_rate_limit_per_minute", USER_LIMIT)
    monkeypatch.setattr(settings, "ai_report_rate_limit_admin_per_minute", ADMIN_LIMIT)
    admin = AdminUserFactory.build(login="airl_admin", status="active")
    member = UserFactory.build(login="airl_member", status="active")
    other = UserFactory.build(login="airl_other", status="active")
    db_session.add_all([admin, member, other])
    project = ProjectFactory.build(key="AIRL", identifier="airl", is_public=True)
    db_session.add(project)
    await db_session.commit()
    return Live(member=member, other=other, admin=admin, project_key=project.key)


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _codes(client: AsyncClient, user: User, url: str, n: int) -> list[int]:
    return [(await client.get(url, headers=HTML, cookies=_cookies(user))).status_code for _ in range(n)]


async def test_my_ai_usage_is_metered_per_user(client: AsyncClient, live: Live) -> None:
    assert await _codes(client, live.member, "/my/ai-usage/", USER_LIMIT + 1) == [200] * USER_LIMIT + [429]
    refused = await client.get("/my/ai-usage/", headers=HTML, cookies=_cookies(live.member))
    assert refused.status_code == 429 and refused.headers["Retry-After"]
    assert "text/html" in refused.headers["content-type"]
    # Another user has a budget of their own.
    assert (await client.get("/my/ai-usage/", headers=HTML, cookies=_cookies(live.other))).status_code == 200


async def test_the_page_and_the_api_share_one_budget(client: AsyncClient, live: Live) -> None:
    url = f"/api/v1/projects/{live.project_key}/ai-usage/report/"
    assert await _codes(client, live.member, "/my/ai-usage/", USER_LIMIT - 1) == [200] * (USER_LIMIT - 1)
    assert (await client.get(url, headers=_bearer(live.member))).status_code == 200
    refused = await client.get(url + "?format=csv", headers=_bearer(live.member))
    assert refused.status_code == 429
    assert refused.json()["errors"][0]["code"] == "rate_limit_exceeded"


async def test_report_pages_are_metered(client: AsyncClient, live: Live) -> None:
    url = f"/projects/{live.project_key}/ai-usage/report/"
    assert await _codes(client, live.member, url, USER_LIMIT + 1) == [200] * USER_LIMIT + [429]


async def test_the_project_ai_usage_tab_is_metered(client: AsyncClient, live: Live) -> None:
    """The tab draws charts of the project's usage, so it spends the same budget as the report."""
    url = f"/projects/{live.project_key}/ai-usage/"
    assert await _codes(client, live.member, url, USER_LIMIT + 1) == [200] * USER_LIMIT + [429]


async def test_administrators_have_a_higher_budget(client: AsyncClient, live: Live) -> None:
    codes = await _codes(client, live.admin, "/admin/ai-spend/", ADMIN_LIMIT + 1)
    assert codes == [200] * ADMIN_LIMIT + [429]
    api = await client.get("/api/v1/ai-usage/report/", headers=_bearer(live.admin))
    assert api.status_code == 429


async def test_the_mcp_report_tool_spends_the_same_budget(client: AsyncClient, live: Live) -> None:
    for _ in range(USER_LIMIT):
        assert (await ai_report_allowed(live.member))[0]
    allowed, retry_after = await ai_report_allowed(live.member)
    assert not allowed and retry_after >= 1
    assert (await client.get("/my/ai-usage/", headers=HTML, cookies=_cookies(live.member))).status_code == 429
