"""AI spend reports stay at a bounded number of queries: a year of daily usage for 50 users.

50 users, each with a subscription (every fifth in EUR), one entry a day for a
year spread over two projects, a third of them on a subscription, daily
EUR/USD rates. The report's statements are counted with a small data set and
with the full one: they must not grow with the data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.exchange_rate import ExchangeRate
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import AiSpendReportService
from specivo.services.permission_service import clear_role_cache
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.services.test_ai_spend_reports import _member, _role

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service, pytest.mark.slow]

USERS = 50
FIRST = date(2025, 11, 1)
LAST = date(2026, 10, 31)
MAX_STATEMENTS = 30

_svc = AiSpendReportService()


def _days() -> list[date]:
    return [FIRST + timedelta(days=i) for i in range((LAST - FIRST).days + 1)]


async def _count(db: AsyncSession, viewer: User, project: Project | None, **kw: Any) -> tuple[int, Any]:
    seen: list[str] = []

    def _on(conn, cursor, statement, *args) -> None:  # noqa: ANN001
        seen.append(statement)

    clear_role_cache(db)
    engine = db.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", _on)
    try:
        report = await _svc.report(db, viewer, project=project, date_from=FIRST, date_to=LAST, currency="USD", **kw)
    finally:
        event.remove(engine, "before_cursor_execute", _on)
    return len(seen), report


def _entry_row(user_id: int, project_id: int, day: date, n: int, sub_id: int | None, currency: str) -> dict[str, Any]:
    api = Decimal(n % 7 + 1)
    on_sub = sub_id is not None and n % 3 == 0
    return {
        "project_id": project_id,
        "user_id": user_id,
        "reporter_user_id": user_id,
        "source": "manual",
        "attribution_method": "explicit",
        "tool_slug": "claude-code" if n % 2 else "codex",
        "provider": "testlab",
        "model": f"lab-model-{n % 3}",
        "usage_date": day,
        "request_count": 3,
        "input": 1000 + n,
        "output": 100,
        "cost_type": "subscription" if on_sub else "api",
        "subscription_id": sub_id if on_sub else None,
        "priced_at": datetime(day.year, day.month, day.day, tzinfo=UTC),
        "marginal_amount": Decimal(0) if on_sub else api,
        "marginal_currency": currency,
        "api_equivalent_amount": api,
        "api_equivalent_currency": currency,
        "cost_basis": "calculated",
        "git_branch": f"feat/{n % 4}",
    }


async def test_query_count_is_bounded_for_a_year_of_fifty_users(db_session: AsyncSession) -> None:
    db = db_session
    p = ProjectFactory.build(key="RPPA", identifier="rppa", is_public=False)
    q = ProjectFactory.build(key="RPPB", identifier="rppb", is_public=False)
    admin = AdminUserFactory.build(login="rpp_admin", status="active")
    mgr = UserFactory.build(login="rpp_mgr", status="active")
    users = [UserFactory.build(login=f"rpp_user{i}", status="active") for i in range(USERS)]
    db.add_all([p, q, admin, mgr, *users])
    await db.flush()
    await _member(db, p, mgr, await _role(db, "Rpp Manager", ["*"], "all"))
    subs = []
    for i, u in enumerate(users):
        subs.append(
            AiSubscription(
                user_id=u.id,
                provider="testlab",
                plan_key="custom",
                plan_name="Plan",
                tool_slugs=["claude-code", "codex"],
                amount=Decimal("1200") if i % 5 == 0 else Decimal("100"),
                currency_code="EUR" if i % 5 == 0 else "USD",
                billing_period="year" if i % 5 == 0 else "month",
                starts_on=FIRST,
            )
        )
    db.add_all(subs)
    await db.flush()
    days = _days()
    await db.execute(
        insert(ExchangeRate),
        [
            {"rate_date": d, "base_code": "EUR", "quote_code": "USD", "rate": Decimal("1.1"), "source": "manual"}
            for d in days
        ],
    )
    # The small data set: one entry.
    await db.execute(insert(AiUsageEntry), [_entry_row(users[0].id, p.id, FIRST, 0, subs[0].id, "EUR")])
    await db.flush()

    shapes: list[dict[str, Any]] = [
        {"project": None, "viewer": admin, "group_by": ["user", "project", "model"], "period": "month"},
        {"project": None, "viewer": admin, "group_by": ["tool", "branch"], "period": "week"},
        {"project": p, "viewer": mgr, "group_by": ["user", "issue"], "period": "day"},
    ]
    few = []
    for shape in shapes:
        kw = dict(shape)
        count, _report = await _count(db, kw.pop("viewer"), kw.pop("project"), **kw)
        few.append(count)

    rows = []
    n = 0
    for i, u in enumerate(users):
        currency = "EUR" if i % 5 == 0 else "USD"
        for day in days:
            n += 1
            rows.append(_entry_row(u.id, p.id if n % 2 else q.id, day, n, subs[i].id, currency))
    for start in range(0, len(rows), 5000):
        await db.execute(insert(AiUsageEntry), rows[start : start + 5000])
    await db.flush()

    for shape, small in zip(shapes, few, strict=True):
        kw = dict(shape)
        count, report = await _count(db, kw.pop("viewer"), kw.pop("project"), **kw)
        assert count <= MAX_STATEMENTS, (shape, count)
        assert count <= small + 2, (shape, small, count)
        assert report.totals.entries >= USERS * len(days) // 2
        if report.seat_cost_shown:
            assert report.totals.seat_allocated is not None
            assert report.totals.seat_allocated.total.amount > 0
