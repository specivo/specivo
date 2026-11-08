"""Per-IP search budgets, and how a refusal reads.

Every search may run the embedding model, so both the search page and the
REST search endpoint are metered per client address in every mode: visitors
without an account in their own bucket, signed-in callers in a larger one.
The address is the one ``specivo.core.client_ip`` resolves, so a client can
only name itself through ``X-Forwarded-For`` when the peer is a trusted proxy.

Over the limit, the JSON API keeps its JSON error. A web page answers with a
page in the app's layout (or, for an htmx request, a short fragment), still
with ``429`` and ``Retry-After``.

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

from specivo.core.config import get_settings
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import Permission
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
ANON_LIMIT = 2
SIGNED_IN_LIMIT = 3


@dataclass(frozen=True)
class Live:
    project: Project
    member: User


@pytest_asyncio.fixture
async def live(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Live:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "search_rate_limit_anonymous_per_minute", ANON_LIMIT)
    monkeypatch.setattr(settings, "search_rate_limit_per_minute", SIGNED_IN_LIMIT)
    monkeypatch.setattr("specivo.core.security.ANONYMOUS_READ_RATE_LIMIT", (100, 60))

    admin = AdminUserFactory.build(login="searchrl_admin", status="active")
    member = UserFactory.build(login="searchrl_member", status="active")
    db_session.add_all([admin, member])
    await db_session.commit()

    project = ProjectFactory.build(key="SEARCHRL", identifier="searchrl", is_public=True)
    db_session.add(project)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db_session.commit()

    await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
    await db_session.commit()
    return Live(project=project, member=member)


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _codes(client: AsyncClient, url: str, n: int, headers: dict[str, str] | None = None) -> list[int]:
    return [(await client.get(url, headers=headers)).status_code for _ in range(n)]


# ---------------------------------------------------------------------------
# Who is metered, in which bucket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
async def test_anonymous_api_search_is_capped_per_ip_in_every_mode(client: AsyncClient, live: Live, mode: str) -> None:
    codes = await _codes(client, f"/api/v1/search/?q=anything&mode={mode}", ANON_LIMIT + 1)

    assert codes == [200] * ANON_LIMIT + [429]


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
async def test_signed_in_api_search_is_capped_per_ip_too(client: AsyncClient, live: Live, mode: str) -> None:
    codes = await _codes(client, f"/api/v1/search/?q=anything&mode={mode}", SIGNED_IN_LIMIT + 1, _bearer(live.member))

    assert codes == [200] * SIGNED_IN_LIMIT + [429]


async def test_signed_in_and_anonymous_budgets_are_separate(client: AsyncClient, live: Live) -> None:
    signed_in = await _codes(client, "/api/v1/search/?q=anything", SIGNED_IN_LIMIT, _bearer(live.member))
    assert signed_in == [200] * SIGNED_IN_LIMIT

    # The signed-in bucket is spent; the anonymous one from the same address is not.
    assert (await client.get("/api/v1/search/?q=anything")).status_code == 200


async def test_the_web_search_page_is_metered_for_anonymous_visitors(client: AsyncClient, live: Live) -> None:
    codes = await _codes(client, "/search/?q=anything", ANON_LIMIT + 1, HTML)

    assert codes == [200] * ANON_LIMIT + [429]


async def test_the_web_search_page_is_metered_for_signed_in_users(client: AsyncClient, live: Live) -> None:
    headers = {**HTML, **_bearer(live.member)}
    codes = await _codes(client, "/search/?q=anything", SIGNED_IN_LIMIT + 1, headers)

    assert codes == [200] * SIGNED_IN_LIMIT + [429]


async def test_the_page_and_the_api_share_one_budget(client: AsyncClient, live: Live) -> None:
    """Switching from the page to the API does not buy a second allowance."""
    assert (await client.get("/search/?q=anything", headers=HTML)).status_code == 200
    assert (await client.get("/api/v1/search/?q=anything")).status_code == 200
    assert (await client.get("/api/v1/search/?q=anything")).status_code == 429


async def test_a_redis_outage_lets_searches_through(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rate limiter fails open, as every other limit does."""

    async def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr("specivo.core.redis.get_redis", _down)

    codes = await _codes(client, "/api/v1/search/?q=anything&mode=hybrid", ANON_LIMIT + 3)

    assert codes == [200] * (ANON_LIMIT + 3)


# ---------------------------------------------------------------------------
# Client addresses behind a proxy
# ---------------------------------------------------------------------------


async def test_behind_a_trusted_proxy_each_client_has_its_own_budget(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["127.0.0.1/32"])
    first = {"X-Forwarded-For": "203.0.113.10"}
    second = {"X-Forwarded-For": "203.0.113.20"}

    assert await _codes(client, "/api/v1/search/?q=anything", ANON_LIMIT + 1, first) == [200] * ANON_LIMIT + [429]
    # Another client behind the same proxy is untouched by the first one's searches.
    assert await _codes(client, "/api/v1/search/?q=anything", ANON_LIMIT, second) == [200] * ANON_LIMIT


async def test_a_forwarded_for_from_an_untrusted_peer_buys_nothing(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["192.0.2.1/32"])

    codes = [
        (await client.get("/api/v1/search/?q=anything", headers={"X-Forwarded-For": f"203.0.113.{n}"})).status_code
        for n in range(ANON_LIMIT + 1)
    ]

    assert codes[-1] == 429, "a rotating X-Forwarded-For must not create fresh budgets"


# ---------------------------------------------------------------------------
# What a refusal looks like
# ---------------------------------------------------------------------------


async def _exhaust(client: AsyncClient, url: str, headers: dict[str, str] | None = None) -> None:
    for _ in range(ANON_LIMIT):
        assert (await client.get(url, headers=headers)).status_code == 200


async def test_the_api_answers_json_with_retry_after(client: AsyncClient, live: Live) -> None:
    await _exhaust(client, "/api/v1/search/?q=anything")

    # Even a client that prefers HTML gets the API's JSON error.
    resp = await client.get("/api/v1/search/?q=anything", headers=HTML)

    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/json")
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.headers["X-RateLimit-Limit"] == str(ANON_LIMIT)
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    error = resp.json()["errors"][0]
    assert error["code"] == "rate_limit_exceeded"
    assert error["details"]["retry_after"] == int(resp.headers["Retry-After"])


async def test_the_web_page_renders_a_friendly_page(client: AsyncClient, live: Live) -> None:
    await _exhaust(client, "/search/?q=anything", HTML)

    resp = await client.get("/search/?q=anything", headers=HTML)

    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("text/html")
    retry_after = int(resp.headers["Retry-After"])
    assert retry_after >= 1
    assert resp.headers["X-RateLimit-Limit"] == str(ANON_LIMIT)
    assert resp.headers["Cache-Control"] == "no-store"
    text = resp.text
    assert "<html" in text
    assert "sp-rate-limit" in text
    assert "Searching is paused" in text
    assert f"{retry_after} second" in text
    assert "rate_limit_exceeded" not in text
    # A way back, and the visitor's own query to retry.
    assert 'href="/search/?q=anything"' in text
    # No inline script: the page relies on the bundles only.
    assert "<script>" not in text


async def test_an_htmx_request_gets_a_fragment(client: AsyncClient, live: Live) -> None:
    await _exhaust(client, "/search/?q=anything", HTML)

    resp = await client.get("/search/?q=anything", headers={"HX-Request": "true", "Accept": "*/*"})

    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("text/html")
    assert "Retry-After" in resp.headers
    assert "<html" not in resp.text
    assert "sp-rate-limit-inline" in resp.text
    assert "Searching is paused" in resp.text


async def test_the_page_is_translated(client: AsyncClient, live: Live) -> None:
    await _exhaust(client, "/search/?q=anything", HTML)
    client.cookies.set("specivo_lang", "th")
    try:
        resp = await client.get("/search/?q=anything", headers=HTML)
    finally:
        client.cookies.delete("specivo_lang")

    assert resp.status_code == 429
    assert "Searching is paused" not in resp.text
    assert "การค้นหาหยุดชั่วคราว" in resp.text


async def test_the_signed_in_page_is_friendly_too(client: AsyncClient, live: Live) -> None:
    headers = {**HTML, **_bearer(live.member)}
    for _ in range(SIGNED_IN_LIMIT):
        assert (await client.get("/search/?q=anything", headers=headers)).status_code == 200

    resp = await client.get("/search/?q=anything", headers=headers)

    assert resp.status_code == 429
    assert "Searching is paused" in resp.text
    assert "Retry-After" in resp.headers


async def test_another_rate_limited_web_page_gets_the_generic_page(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only search: any web page over a limit reads as a page."""
    monkeypatch.setattr("specivo.core.security.ANONYMOUS_READ_RATE_LIMIT", (1, 60))
    assert (await client.get("/projects/", headers=HTML)).status_code == 200

    resp = await client.get("/projects/", headers=HTML)

    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("text/html")
    assert "Retry-After" in resp.headers
    assert "Too many requests" in resp.text
    assert "Searching is paused" not in resp.text
