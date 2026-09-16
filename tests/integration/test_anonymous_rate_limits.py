"""Per-IP budgets for anonymous traffic.

Anonymous reads are the only requests Specivo serves to callers it cannot
name, so the only thing bounding them is the address they arrive from. Two
properties matter:

- reads and searches are metered in separate buckets, neither of which is the
  bucket a signed-in caller spends;
- the address used is the peer Specivo is actually talking to. A caller that
  makes up an ``X-Forwarded-For`` cannot spend somebody else's budget, or
  escape its own, unless the peer is a configured trusted proxy.

The limits are lowered for these tests rather than driving 120 requests
through the stack: what is under test is that the metering is wired to the
anonymous path at all, not Redis's arithmetic.
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


@dataclass(frozen=True)
class Live:
    """An opted-in project with the switch on, plus a signed-in user."""

    project: Project
    member: User


@pytest_asyncio.fixture
async def live(db_session: AsyncSession) -> Live:
    admin = AdminUserFactory.build(login="anonrl_admin", status="active")
    member = UserFactory.build(login="anonrl_member", status="active")
    db_session.add_all([admin, member])
    await db_session.commit()

    project = ProjectFactory.build(key="ANONRL", identifier="anonrl", is_public=True)
    db_session.add(project)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db_session.commit()

    await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
    await db_session.commit()

    return Live(project=project, member=member)


async def test_anonymous_reads_are_capped_per_ip(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("specivo.core.security.ANONYMOUS_READ_RATE_LIMIT", (3, 60))

    codes = [(await client.get("/api/v1/projects/")).status_code for _ in range(4)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


async def test_anonymous_searches_have_their_own_tighter_bucket(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Searching does not spend the read budget, and is capped on its own."""
    monkeypatch.setattr("specivo.core.security.ANONYMOUS_READ_RATE_LIMIT", (100, 60))
    monkeypatch.setattr("specivo.api.v1.search.ANONYMOUS_SEARCH_RATE_LIMIT", (2, 60))

    codes = [(await client.get("/api/v1/search/?q=anything")).status_code for _ in range(3)]

    assert codes[:2] == [200, 200]
    assert codes[2] == 429

    # The read bucket is untouched, so ordinary reads still go through.
    assert (await client.get("/api/v1/projects/")).status_code == 200


async def test_a_spoofed_forwarded_for_buys_nothing(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The peer is not a trusted proxy, so its claim about the client is ignored."""
    assert get_settings().trusted_proxies == [], "this test assumes no proxy is trusted"
    monkeypatch.setattr("specivo.core.security.ANONYMOUS_READ_RATE_LIMIT", (3, 60))

    codes = [
        (await client.get("/api/v1/projects/", headers={"X-Forwarded-For": f"203.0.113.{n}"})).status_code
        for n in range(4)
    ]

    assert codes[3] == 429, "a rotating X-Forwarded-For must not create fresh budgets"


async def test_signed_in_callers_do_not_spend_the_anonymous_budget(
    client: AsyncClient, live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("specivo.core.security.ANONYMOUS_READ_RATE_LIMIT", (2, 60))
    headers = {"Authorization": f"Bearer {_make_access_token(live.member, get_settings())}"}

    for _ in range(5):
        assert (await client.get("/api/v1/projects/", headers=headers)).status_code == 200

    # The anonymous bucket was never touched by those five requests.
    assert (await client.get("/api/v1/projects/")).status_code == 200
