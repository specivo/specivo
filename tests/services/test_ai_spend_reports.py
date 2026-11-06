"""AI spend reports: grouping, visibility, currencies, rate policies, seat proration and allocation, leaks.

The world: two private projects, ``RPA`` and ``RPB``, and entries written
straight into the table with known costs (no pricing involved), so every
amount below is exact:

- ``admin``; ``mgr``: Manager-like in RPA (``*``, visibility ``all``);
- ``alice`` and ``bob``: Developer-like in both (``view_ai_usage`` own);
- ``carol``: ``view_ai_usage`` with ``all`` in RPA, cannot see the private issue;
- ``dave``: holds a subscription but logs nothing.

Subscriptions: alice 100 USD a month; bob 1200 EUR a year (100 EUR a month);
dave 31 USD a month from 17 March (15 of March's 31 days: 15 USD).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.exchange_rate import ExchangeRate
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    GROUP_BY,
    HIDDEN_ISSUE,
    AiSpendReportService,
    ReportFilters,
    ReportRow,
    SpendReport,
    default_range,
    parse_group_by,
    report_csv,
)
from specivo.services.permission_service import clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = AiSpendReportService()

MARCH_1, MARCH_31 = date(2026, 3, 1), date(2026, 3, 31)
APRIL_1, APRIL_30 = date(2026, 4, 1), date(2026, 4, 30)


@dataclass
class World:
    p: Project
    q: Project
    public_issue: Issue
    private_issue: Issue
    users: dict[str, User]
    subs: dict[str, AiSubscription]

    def __getattr__(self, name: str) -> User:
        try:
            return self.__dict__["users"][name]
        except KeyError:
            raise AttributeError(name) from None


async def _role(db: AsyncSession, name: str, permissions: list[str], visibility: str) -> Role:
    role = Role(
        name=name, permissions=permissions, builtin=0, issues_visibility="default", ai_usage_visibility=visibility
    )
    db.add(role)
    await db.flush()
    return role


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


def _sub(user: User, amount: str, currency: str, period: str, starts: date, **kw: Any) -> AiSubscription:
    return AiSubscription(
        user_id=user.id,
        provider="testlab",
        plan_key="custom",
        plan_name="Secret plan",
        tool_slugs=["claude-code"],
        amount=Decimal(amount),
        currency_code=currency,
        billing_period=period,
        starts_on=starts,
        **kw,
    )


async def build_world(db: AsyncSession, prefix: str = "rp") -> World:
    """The world above; *prefix* keeps keys and logins unique to the calling module (``RPA``, ``rp_alice``)."""
    status = StatusFactory.build(name=f"{prefix} Open")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{prefix} Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{prefix} Normal")
    p = ProjectFactory.build(key=f"{prefix.upper()}A", identifier=f"{prefix}a", is_public=False)
    q = ProjectFactory.build(key=f"{prefix.upper()}B", identifier=f"{prefix}b", is_public=False)
    db.add_all([tracker, priority, p, q])
    await db.flush()

    users: dict[str, User] = {}
    for name in ("mgr", "alice", "bob", "carol", "dave"):
        users[name] = UserFactory.build(login=f"{prefix}_{name}", status="active", display_name=f"Rep {name.title()}")
    users["admin"] = AdminUserFactory.build(login=f"{prefix}_admin", status="active")
    db.add_all(users.values())
    await db.flush()

    mgr_role = await _role(db, f"{prefix} Manager", ["*"], "all")
    dev_role = await _role(db, f"{prefix} Developer", ["view_issues", "view_ai_usage", "log_ai_usage"], "own")
    viewer_role = await _role(db, f"{prefix} Viewer", ["view_issues", "view_ai_usage"], "all")
    await _member(db, p, users["mgr"], mgr_role)
    for project in (p, q):
        for name in ("alice", "bob"):
            await _member(db, project, users[name], dev_role)
    await _member(db, p, users["carol"], viewer_role)

    def issue(**kw: Any) -> Issue:
        return IssueFactory.build(
            project_id=p.id,
            project_key=p.key,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=users["mgr"].id,
            **kw,
        )

    public_issue = issue(sequence_number=1, subject="Public work")
    private_issue = issue(sequence_number=2, subject="Private work", is_private=True)
    db.add_all([public_issue, private_issue])
    subs = {
        "alice": _sub(users["alice"], "100", "USD", "month", date(2026, 1, 1)),
        "bob": _sub(users["bob"], "1200", "EUR", "year", date(2026, 1, 1)),
        "dave": _sub(users["dave"], "31", "USD", "month", date(2026, 3, 17)),
    }
    db.add_all(subs.values())
    await db.commit()
    clear_role_cache(db)
    return World(p=p, q=q, public_issue=public_issue, private_issue=private_issue, users=users, subs=subs)


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    return await build_world(db_session)


async def entry(
    db: AsyncSession,
    user: User,
    *,
    project: Project | None,
    day: date,
    api: str | None = None,
    marginal: str | None = None,
    currency: str = "USD",
    marginal_currency: str | None = None,
    cost_type: str = "api",
    subscription: AiSubscription | None = None,
    model: str = "lab-model-1",
    tool: str = "claude-code",
    issue: Issue | None = None,
    branch: str | None = None,
    tokens: int = 1000,
    excluded: bool = False,
) -> AiUsageEntry:
    """An entry with fixed costs. ``marginal`` defaults to ``api`` for api usage and to 0 for subscriptions."""
    if marginal is None:
        marginal = "0" if cost_type in ("subscription", "free") else api
    row = AiUsageEntry(
        project_id=project.id if project else None,
        issue_id=issue.id if issue else None,
        user_id=user.id,
        reporter_user_id=user.id,
        source="manual",
        attribution_method="explicit" if project else "none",
        tool_slug=tool,
        provider="testlab",
        model=model,
        usage_date=day,
        request_count=2,
        input=tokens,
        output=tokens // 10,
        cost_type=cost_type,
        subscription_id=subscription.id if subscription else None,
        priced_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
        marginal_amount=Decimal(marginal) if marginal is not None else None,
        marginal_currency=(marginal_currency or currency) if marginal is not None else None,
        api_equivalent_amount=Decimal(api) if api is not None else None,
        api_equivalent_currency=currency if api is not None else None,
        cost_basis="calculated" if api is not None else "unpriced",
        git_branch=branch,
        excluded_at=datetime(2026, 5, 1, tzinfo=UTC) if excluded else None,
    )
    db.add(row)
    await db.flush()
    return row


async def rate(db: AsyncSession, day: date, base: str, quote: str, value: str) -> None:
    db.add(ExchangeRate(rate_date=day, base_code=base, quote_code=quote, rate=Decimal(value), source="manual"))
    await db.flush()


async def run(
    db: AsyncSession,
    viewer: User,
    project: Project | None = None,
    *,
    date_from: date = MARCH_1,
    date_to: date = MARCH_31,
    **kw: Any,
) -> SpendReport:
    kw.setdefault("currency", "USD")
    clear_role_cache(db)
    return await _svc.report(db, viewer, project=project, date_from=date_from, date_to=date_to, **kw)


def by(report: SpendReport, **key: Any) -> ReportRow:
    found = [r for r in report.rows if all(r.key.get(k) == v for k, v in key.items()) and not r.seat_only]
    assert len(found) == 1, (key, [(r.key, r.seat_only) for r in report.rows])
    return found[0]


def amount(summary: Any) -> Decimal:
    assert summary is not None
    return summary.total.amount


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


async def test_parse_group_by_orders_and_validates() -> None:
    assert parse_group_by("model, user") == ("user", "model")
    assert parse_group_by(["branch"]) == ("branch",)
    assert parse_group_by(None) == ()
    with pytest.raises(AppError):
        parse_group_by("user,salary")
    with pytest.raises(AppError):
        parse_group_by("user,user")


async def test_presets() -> None:
    today = date(2026, 11, 6)
    assert default_range("7d", today) == (date(2026, 10, 31), today)
    assert default_range("30d", today) == (date(2026, 10, 8), today)
    assert default_range("this_month", today) == (date(2026, 11, 1), today)
    assert default_range("last_month", today) == (date(2026, 10, 1), date(2026, 10, 31))


async def test_bad_parameters_are_422(db_session: AsyncSession, w: World) -> None:
    for kw in (
        {"period": "year"},
        {"sort": "rank"},
        {"date_from": MARCH_31, "date_to": MARCH_1},
        {"date_from": date(2020, 1, 1), "date_to": date(2026, 1, 1)},
        {"rate_policy": "yesterday"},
        {"currency": "XYZ"},
    ):
        with pytest.raises(AppError) as exc:
            await run(db_session, w.admin, **kw)
        assert exc.value.status_code in (404, 422), kw


async def test_instance_report_is_for_administrators(db_session: AsyncSession, w: World) -> None:
    for name in ("mgr", "alice"):
        with pytest.raises(PermissionDeniedError):
            await run(db_session, w.users[name])
    assert (await run(db_session, w.admin)).scope == "instance"


# ---------------------------------------------------------------------------
# Grouping and measures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", GROUP_BY)
async def test_each_group_by(db_session: AsyncSession, w: World, dim: str) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="2", issue=w.public_issue, branch="feat/a")
    await entry(db, w.bob, project=w.q, day=date(2026, 3, 3), api="3", model="lab-model-2", tool="codex")
    await entry(db, w.bob, project=None, day=date(2026, 3, 4), api="5", cost_type="free")
    report = await run(db, w.admin, group_by=[dim], include_seat_cost=False)
    assert report.group_by == (dim,)
    assert sum(r.entries for r in report.rows) == 3
    assert report.totals.entries == 3
    assert amount(report.totals.api_equivalent) == Decimal(10)
    keys = {r.key[dim] for r in report.rows}
    expected = {
        "user": {w.alice.id, w.bob.id},
        "project": {w.p.id, w.q.id, None},
        "issue": {w.public_issue.id, None},
        "tool": {"claude-code", "codex"},
        "provider": {"testlab"},
        "model": {"lab-model-1", "lab-model-2"},
        "cost_type": {"api", "free"},
        "branch": {"feat/a", None},
    }[dim]
    assert keys == expected
    labels = {r.labels[dim] for r in report.rows}
    if dim == "user":
        assert labels == {"Rep Alice", "Rep Bob"}
    if dim == "project":
        assert labels == {"RPA", "RPB", ""}
    if dim == "issue":
        assert labels == {"RPA-1", ""}


async def test_measures_tokens_requests_and_spend(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="2.50", tokens=1000)
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 9), api="1.25", tokens=3000)
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 9), api=None, cost_type="unknown", marginal=None)
    report = await run(db, w.admin, group_by=["user"], include_seat_cost=False)
    row = by(report, user=w.alice.id)
    assert row.entries == 3 and row.unpriced_entries == 1
    assert row.request_count == 6
    assert row.tokens["input"] == 5000 and row.tokens["output"] == 500
    assert row.total_tokens == 5500
    assert amount(row.marginal) == Decimal("3.75")
    assert amount(row.api_equivalent) == Decimal("3.75")
    assert row.total_spend is row.marginal  # no seat cost asked for
    assert row.seat_allocated is None and row.leverage is None


@pytest.mark.parametrize(
    ("period", "starts"),
    [
        ("day", {date(2026, 3, 2), date(2026, 3, 4), date(2026, 3, 30)}),
        ("week", {date(2026, 3, 2), date(2026, 3, 30)}),
        ("month", {date(2026, 3, 1), date(2026, 4, 1)}),
    ],
)
async def test_period_buckets(db_session: AsyncSession, w: World, period: str, starts: set[date]) -> None:
    db = db_session
    for day in (date(2026, 3, 2), date(2026, 3, 4), date(2026, 3, 30)):
        await entry(db, w.alice, project=w.p, day=day, api="1")
    await entry(db, w.alice, project=w.p, day=date(2026, 4, 1), api="1")
    report = await run(
        db,
        w.admin,
        date_to=date(2026, 3, 31) if period != "month" else APRIL_30,
        group_by=[],
        period=period,
        include_seat_cost=False,
    )
    assert {r.period for r in report.rows} == starts


async def test_sorted_by_key_unless_spend_is_asked_for(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="1")
    await entry(db, w.bob, project=w.p, day=date(2026, 3, 2), api="9")
    by_key = await run(db, w.admin, group_by=["user"], include_seat_cost=False)
    assert [r.labels["user"] for r in by_key.rows] == ["Rep Alice", "Rep Bob"]
    by_spend = await run(db, w.admin, group_by=["user"], include_seat_cost=False, sort="spend")
    assert [r.labels["user"] for r in by_spend.rows] == ["Rep Bob", "Rep Alice"]


async def test_excluded_entries_are_left_out(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="1")
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 3), api="7", excluded=True)
    report = await run(db, w.admin, group_by=["user"], include_seat_cost=False)
    assert report.totals.entries == 1
    assert amount(report.totals.api_equivalent) == Decimal(1)


async def test_filters(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="1", tool="codex")
    await entry(db, w.bob, project=w.p, day=date(2026, 3, 2), api="2")
    report = await run(db, w.admin, group_by=["user"], filters=ReportFilters(tool="codex"), include_seat_cost=False)
    assert [r.key["user"] for r in report.rows] == [w.alice.id]
    report = await run(db, w.admin, group_by=["user"], filters=ReportFilters(user_id=w.bob.id))
    assert {r.key["user"] for r in report.rows} == {w.bob.id}


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def test_rows_only_from_visible_entries(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="1")
    await entry(db, w.bob, project=w.p, day=date(2026, 3, 2), api="2")
    await entry(db, w.bob, project=w.p, day=date(2026, 3, 3), api="4", issue=w.private_issue)
    await entry(db, w.bob, project=w.q, day=date(2026, 3, 3), api="8")

    alice = await run(db, w.alice, w.p, group_by=["user"])
    assert {r.key["user"] for r in alice.rows} == {w.alice.id}
    assert amount(alice.totals.api_equivalent) == Decimal(1)

    carol = await run(db, w.carol, w.p, group_by=["user"])
    assert amount(carol.totals.api_equivalent) == Decimal(3)  # not the private issue's 4

    mgr = await run(db, w.mgr, w.p, group_by=["user"])
    assert amount(mgr.totals.api_equivalent) == Decimal(7)  # never RPB's 8

    with pytest.raises(AppError):
        await run(db, w.carol, w.q)


async def test_own_entries_on_hidden_issues_keep_the_issue_hidden(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.carol, project=w.p, day=date(2026, 3, 2), api="1", issue=w.private_issue)
    report = await run(db, w.carol, w.p, group_by=["issue"])
    assert [r.key["issue"] for r in report.rows] == [HIDDEN_ISSUE]
    assert report.rows[0].labels["issue"] == ""


# ---------------------------------------------------------------------------
# Currencies and rate policies
# ---------------------------------------------------------------------------


async def test_mixed_currencies_with_and_without_rates(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await rate(db, date(2026, 3, 8), "EUR", "USD", "1.1")
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 10), api="10", currency="EUR")
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 10), api="2", currency="USD")
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 10), api="1000", currency="THB")
    report = await run(db, w.admin, group_by=["user"], include_seat_cost=False)
    api = report.totals.api_equivalent
    assert api is not None
    assert api.total.amount == Decimal(13)
    assert [(g.currency, g.amount.amount, g.reason.value) for g in api.unconverted] == [
        ("THB", Decimal(1000), "no_rate")
    ]
    assert date(2026, 3, 8) in report.rate_dates
    assert any(r.from_currency == "EUR" for r in report.rates_used)


async def test_latest_versus_entry_date_policy(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await rate(db, date(2026, 3, 8), "EUR", "USD", "1.1")
    await rate(db, date(2026, 3, 25), "EUR", "USD", "1.2")
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 10), api="10", currency="EUR")
    on_entry = await run(db, w.admin, group_by=[], include_seat_cost=False)
    assert amount(on_entry.totals.api_equivalent) == Decimal(11)
    latest = await run(db, w.admin, group_by=[], include_seat_cost=False, rate_policy="latest")
    assert amount(latest.totals.api_equivalent) == Decimal(12)
    assert latest.rate_dates == (date(2026, 3, 25),)


async def test_stale_rates_stay_unconverted(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await rate(db, date(2026, 2, 1), "EUR", "USD", "1.1")
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 10), api="10", currency="EUR")
    report = await run(db, w.admin, group_by=[], include_seat_cost=False)
    api = report.totals.api_equivalent
    assert api is not None and api.total.amount == 0
    assert [(g.reason.value, g.newest_rate_date) for g in api.unconverted] == [("stale", date(2026, 2, 1))]


async def test_reporting_currency_defaults_to_the_project(db_session: AsyncSession, w: World) -> None:
    db = db_session
    w.p.reporting_currency_code = "THB"
    await db.flush()
    report = await _svc.report(db, w.admin, project=w.p, date_from=MARCH_1, date_to=MARCH_31)
    assert report.currency == "THB"


# ---------------------------------------------------------------------------
# Seat allocation
# ---------------------------------------------------------------------------


async def test_seat_fee_is_spread_by_api_equivalent_value(db_session: AsyncSession, w: World) -> None:
    db = db_session
    sub = w.subs["alice"]
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub)
    await entry(db, w.alice, project=w.q, day=date(2026, 3, 6), api="10", cost_type="subscription", subscription=sub)
    await entry(db, w.alice, project=w.q, day=date(2026, 3, 7), api=None, cost_type="subscription", subscription=sub)

    instance = await run(db, w.admin, group_by=["user", "project"])
    assert amount(by(instance, user=w.alice.id, project=w.p.id).seat_allocated) == Decimal(75)
    assert amount(by(instance, user=w.alice.id, project=w.q.id).seat_allocated) == Decimal(25)
    alice_total = by(instance, user=w.alice.id, project=w.p.id)
    assert amount(alice_total.total_spend) == Decimal(75)  # marginal 0 + seat 75
    assert alice_total.leverage == Decimal("0.40")  # 30 / 75

    project = await run(db, w.mgr, w.p, group_by=["user"])
    row = by(project, user=w.alice.id)
    assert amount(row.seat_allocated) == Decimal(75)
    assert project.seat_cost_shown and not project.unallocated_shown
    assert amount(row.seat_unallocated) == 0


async def test_allocation_does_not_depend_on_the_period_or_the_viewer(db_session: AsyncSession, w: World) -> None:
    db = db_session
    sub = w.subs["alice"]
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub)
    await entry(db, w.alice, project=w.q, day=date(2026, 3, 20), api="10", cost_type="subscription", subscription=sub)
    one_day = await run(db, w.admin, date_from=date(2026, 3, 5), date_to=date(2026, 3, 5), group_by=["user"])
    assert amount(by(one_day, user=w.alice.id).seat_allocated) == Decimal(75)
    mgr = await run(db, w.mgr, w.p, group_by=["user"])
    assert amount(by(mgr, user=w.alice.id).seat_allocated) == Decimal(75)


async def test_excluded_entries_carry_no_seat_share(db_session: AsyncSession, w: World) -> None:
    db = db_session
    sub = w.subs["alice"]
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub)
    await entry(
        db,
        w.alice,
        project=w.q,
        day=date(2026, 3, 6),
        api="10",
        cost_type="subscription",
        subscription=sub,
        excluded=True,
    )
    report = await run(db, w.admin, group_by=["user"])
    assert amount(by(report, user=w.alice.id).seat_allocated) == Decimal(100)


async def test_proration_and_months_without_entries(db_session: AsyncSession, w: World) -> None:
    db = db_session
    report = await run(db, w.admin, group_by=["user"])
    # dave: 31 USD a month from 17 March: 15 of 31 days, nobody used it.
    dave = by(report, user=w.dave.id)
    assert dave.entries == 0
    assert amount(dave.seat_unallocated) == Decimal(15)
    assert amount(dave.seat_allocated) == 0
    # alice: 100 USD, no entries at all in March.
    assert amount(by(report, user=w.alice.id).seat_unallocated) == Decimal(100)


async def test_all_unpriced_month_is_unallocated_at_the_month_end_rate(db_session: AsyncSession, w: World) -> None:
    db = db_session
    sub = w.subs["bob"]
    await rate(db, date(2026, 4, 10), "EUR", "USD", "1.0")
    await rate(db, date(2026, 4, 28), "EUR", "USD", "1.1")
    await entry(db, w.bob, project=w.p, day=date(2026, 4, 10), api=None, cost_type="subscription", subscription=sub)
    report = await run(db, w.admin, date_from=APRIL_1, date_to=APRIL_30, group_by=["user"])
    bob = by(report, user=w.bob.id)
    assert amount(bob.seat_allocated) == 0
    # 1200 EUR a year = 100 EUR in April, converted at the rate of 30 April (28 April's, within 7 days).
    assert amount(bob.seat_unallocated) == Decimal(110)
    assert amount(bob.total_spend) == Decimal(110)


async def test_unallocated_seat_gets_its_own_row_when_grouped_by_project(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.dave, project=w.p, day=date(2026, 3, 20), api="1")
    report = await run(db, w.admin, group_by=["project"])
    seat_rows = [r for r in report.rows if r.seat_only]
    assert seat_rows and all(r.entries == 0 for r in seat_rows)
    assert all(r.key["project"] is None for r in seat_rows)
    assert amount(by(report, project=w.p.id).seat_unallocated) == 0
    # alice 100 USD + dave 15 USD; bob's EUR fee has no rate.
    assert amount(report.totals.seat_unallocated) == Decimal(115)
    assert report.totals.seat_unallocated is not None
    assert [g.currency for g in report.totals.seat_unallocated.unconverted] == ["EUR"]


async def test_seat_fee_in_another_currency_weighs_converted_values(db_session: AsyncSession, w: World) -> None:
    db = db_session
    sub = w.subs["bob"]  # 100 EUR in March
    await rate(db, date(2026, 3, 1), "EUR", "USD", "2")
    await rate(db, date(2026, 3, 31), "EUR", "USD", "1.25")
    await entry(db, w.bob, project=w.p, day=date(2026, 3, 2), api="20", cost_type="subscription", subscription=sub)
    await entry(
        db,
        w.bob,
        project=w.q,
        day=date(2026, 3, 2),
        api="10",
        currency="EUR",
        cost_type="subscription",
        subscription=sub,
    )
    report = await run(db, w.admin, group_by=["project"])
    # Weights in EUR: 20 USD = 10 EUR, and 10 EUR: half each, 50 EUR, at 31 March's 1.25.
    assert amount(by(report, project=w.p.id).seat_allocated) == Decimal("62.5")
    assert amount(by(report, project=w.q.id).seat_allocated) == Decimal("62.5")


# ---------------------------------------------------------------------------
# Leak protection
# ---------------------------------------------------------------------------


async def test_partial_viewers_see_no_seat_cost(db_session: AsyncSession, w: World) -> None:
    db = db_session
    sub = w.subs["alice"]
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub)
    # carol (visibility all) sees every entry so far: she sees the seat cost.
    carol = await run(db, w.carol, w.p, group_by=["user"])
    assert carol.seat_cost_shown and amount(by(carol, user=w.alice.id).seat_allocated) == Decimal(100)
    # An entry on an issue she cannot see: she no longer sees every entry of the range.
    await entry(db, w.bob, project=w.p, day=date(2026, 3, 6), api="1", issue=w.private_issue)
    for viewer in (w.alice, w.carol):
        report = await run(db, viewer, w.p, group_by=["user"])
        assert report.seat_cost_hidden and not report.seat_cost_shown
        assert all(r.seat_allocated is None and r.seat_unallocated is None and r.leverage is None for r in report.rows)
        assert report.totals.total_spend is report.totals.marginal


async def test_project_report_never_shows_the_whole_fee(db_session: AsyncSession, w: World) -> None:
    """All of alice's March usage is in RPB: RPA's manager sees no fee of hers, not even unallocated."""
    db = db_session
    sub = w.subs["alice"]
    await entry(db, w.alice, project=w.q, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=sub)
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 6), api=None, cost_type="subscription", subscription=sub)
    report = await run(db, w.mgr, w.p, group_by=["user"])
    row = by(report, user=w.alice.id)
    assert amount(row.seat_allocated) == 0
    assert amount(report.totals.seat_unallocated) == 0
    assert amount(report.totals.total_spend) == 0
    # dave and bob hold plans and are no RPA members with entries: nothing about them.
    assert {r.key["user"] for r in report.rows} == {w.alice.id}


async def test_unallocated_needs_a_filter_a_fee_can_match(db_session: AsyncSession, w: World) -> None:
    db = db_session
    for f in (ReportFilters(model="lab-model-1"), ReportFilters(tool="codex"), ReportFilters(cost_type="api")):
        report = await run(db, w.admin, group_by=["user"], filters=f)
        assert not report.unallocated_shown
        assert amount(report.totals.seat_unallocated) == 0
    report = await run(db, w.admin, group_by=["user"], filters=ReportFilters(user_id=w.dave.id))
    assert report.unallocated_shown
    assert amount(report.totals.seat_unallocated) == Decimal(15)


async def test_reports_name_no_plan(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(
        db, w.alice, project=w.p, day=date(2026, 3, 5), api="30", cost_type="subscription", subscription=w.subs["alice"]
    )
    report = await run(db, w.admin, group_by=list(GROUP_BY), period="day")
    assert "Secret plan" not in report_csv(report)
    assert "Secret plan" not in repr(report.rows)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


async def test_csv(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await entry(db, w.alice, project=w.p, day=date(2026, 3, 2), api="2.5", branch="=HYPERLINK(1)")
    report = await run(db, w.admin, group_by=["user", "branch"], period="month")
    lines = report_csv(report).splitlines()
    header = lines[0].split(",")
    assert header[:3] == ["period", "user", "branch"]
    assert "allocated_seat_cost" in header and "leverage" in header
    alice = next(line for line in lines if "Rep Alice" in line)
    assert alice.startswith("2026-03-01,Rep Alice,'=HYPERLINK(1)")
    assert ",USD,2.50,2.50," in alice
    assert lines[-1].startswith("total,")
