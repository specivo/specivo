"""Retention of the outgoing webhook delivery log."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import WEBHOOK_DELIVERY_RETENTION_DAYS
from specivo.core.utils import utcnow
from specivo.models.webhook import WebhookDelivery
from specivo.services.webhook_delivery import purge_batch_statement, purge_old_deliveries
from specivo.services.webhook_service import WebhookService
from tests.factories.project import ProjectFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


@pytest_asyncio.fixture
async def webhook(db_session: AsyncSession):
    project = ProjectFactory.build(key="WHPRG", name="Webhook Purge", identifier="whprg")
    db_session.add(project)
    await db_session.commit()
    hook, _ = await WebhookService().register(
        db_session, project.id, "https://hooks.example.com/in", events=["issue.created"], secret="purge-secret-0123"
    )
    await db_session.commit()
    return hook


def _row(webhook, event: str, status: str, age_days: float) -> WebhookDelivery:
    created = utcnow() - timedelta(days=age_days)
    return WebhookDelivery(
        webhook_id=webhook.id,
        event=event,
        payload={"event": event},
        status=status,
        attempts=1,
        created_at=created,
        updated_at=created,
    )


async def _events(db_session: AsyncSession) -> list[str]:
    rows = await db_session.execute(select(WebhookDelivery.event).order_by(WebhookDelivery.event))
    return list(rows.scalars().all())


async def test_old_settled_rows_go_and_recent_and_pending_rows_stay(db_session, webhook):
    old = WEBHOOK_DELIVERY_RETENTION_DAYS + 1
    recent = WEBHOOK_DELIVERY_RETENTION_DAYS - 1
    db_session.add_all(
        [
            _row(webhook, "old.succeeded", "succeeded", old),
            _row(webhook, "old.failed", "failed", old),
            _row(webhook, "old.pending", "pending", old),
            _row(webhook, "recent.succeeded", "succeeded", recent),
            _row(webhook, "recent.failed", "failed", recent),
            _row(webhook, "recent.pending", "pending", recent),
        ]
    )
    await db_session.commit()

    assert await purge_old_deliveries(db_session) == 2

    assert await _events(db_session) == [
        "old.pending",
        "recent.failed",
        "recent.pending",
        "recent.succeeded",
    ]


async def test_nothing_to_delete(db_session, webhook):
    db_session.add(_row(webhook, "recent", "succeeded", 1))
    await db_session.commit()

    assert await purge_old_deliveries(db_session) == 0
    assert await _events(db_session) == ["recent"]


async def test_large_backlog_is_deleted_in_batches(db_session, webhook):
    old = WEBHOOK_DELIVERY_RETENTION_DAYS + 5
    db_session.add_all([_row(webhook, f"old.{i:02d}", "succeeded", old) for i in range(7)])
    db_session.add(_row(webhook, "recent", "failed", 1))
    await db_session.commit()

    with patch.object(db_session, "commit", wraps=db_session.commit) as commit:
        assert await purge_old_deliveries(db_session, batch_size=3) == 7

    # 3 + 3 + 1: each batch is its own transaction, so no run holds its locks for long.
    assert commit.await_count == 3
    assert await _events(db_session) == ["recent"]


def _plan_nodes(node: dict) -> list[dict]:
    nodes = [node]
    for child in node.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


async def test_batch_delete_plan_does_not_depend_on_row_estimates(db_session):
    # asyncpg runs prepared statements, and a generic plan does not know the cutoff or
    # the batch size. With "id IN (subquery)" such a plan joins the batch against the
    # whole table (a hash semi join over a sequential scan on a large log). Fetching the
    # batch first, as an InitPlan, and deleting by "id = ANY(array)" keeps every
    # plan on the primary key index, whatever the statistics say.
    stmt = purge_batch_statement(utcnow(), 1000)
    sql = str(stmt.compile(dialect=db_session.bind.dialect, compile_kwargs={"render_postcompile": True}))
    # The statement keeps its $n placeholders, so it is explained from inside the
    # server: the driver would otherwise expect values for them.
    await db_session.execute(
        text(
            "CREATE FUNCTION pg_temp.generic_plan(q text) RETURNS json LANGUAGE plpgsql AS $fn$"
            " DECLARE plan json; BEGIN"
            " EXECUTE 'EXPLAIN (GENERIC_PLAN, FORMAT JSON) ' || q INTO plan; RETURN plan;"
            " END $fn$"
        )
    )
    plan = (await db_session.execute(text("SELECT pg_temp.generic_plan(:q)"), {"q": sql})).scalar_one()
    nodes = _plan_nodes(plan[0]["Plan"])

    assert [n["Node Type"] for n in nodes if "Join Type" in n] == []
    # (Whether the delete itself uses the index depends on the table size, so on
    # this near-empty test table it is not asserted; the plans on a 1.5M-row
    # table are in the docstring of purge_batch_statement.)
    assert any(n.get("Parent Relationship") == "InitPlan" for n in nodes)


async def test_one_run_stops_after_max_batches(db_session, webhook):
    # A first run on a large backlog must not outlive the cleanup lock: the rest goes next run.
    old = WEBHOOK_DELIVERY_RETENTION_DAYS + 5
    db_session.add_all([_row(webhook, f"old.{i:02d}", "succeeded", old) for i in range(7)])
    await db_session.commit()

    assert await purge_old_deliveries(db_session, batch_size=3, max_batches=2) == 6
    assert await _events(db_session) == ["old.06"]

    assert await purge_old_deliveries(db_session, batch_size=3, max_batches=2) == 1
    assert await _events(db_session) == []
