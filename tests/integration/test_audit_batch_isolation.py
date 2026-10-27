"""The audit events a request buffers are flushed through the app's ``get_db``.

``AuditBatchMiddleware`` writes the buffered events after the response in a
session of their own, so they survive a request whose own transaction rolls
back. It takes that session from the ``get_db`` dependency, honouring an
override. In tests the override hands out sessions on the test's rolled-back
connection, so the rows stay inside the test: other xdist workers never see
them, and another test's teardown cannot delete them before the assertions.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from specivo.main import get_plugin_manager
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.security_audit_service import AuditEvent

pytestmark = pytest.mark.integration


@pytest.fixture
def audit_on() -> Iterator[None]:
    get_plugin_manager().feature_registry.register("security_audit_log", "test")
    yield


async def _count(session_or_conn: AsyncSession, slug: str) -> int:
    stmt = (
        select(func.count())
        .select_from(SecurityAuditLog)
        .where(
            SecurityAuditLog.event_type == AuditEvent.SSO_PROVIDER_CREATED.value,
            SecurityAuditLog.details["provider"].astext == slug,
        )
    )
    return (await session_or_conn.execute(stmt)).scalar_one()


async def test_buffered_audit_rows_stay_in_the_test_transaction(
    admin_client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine, audit_on: None
) -> None:
    slug = "batch-isolation"
    created = await admin_client.post(
        "/api/v1/admin/sso/providers/",
        json={
            "slug": slug,
            "display_name": "Isolation IdP",
            "issuer": "https://idp.example.com/realms/isolation",
            "client_id": "specivo",
            "client_secret": "isolation-secret-0001",
        },
    )
    assert created.status_code == 201, created.text

    # The test sees the row the middleware flushed after the response...
    assert await _count(db_session, slug) == 1
    # ...and nobody outside the test's transaction does.
    async with db_engine.connect() as other:
        assert await _count(other, slug) == 0  # type: ignore[arg-type]
