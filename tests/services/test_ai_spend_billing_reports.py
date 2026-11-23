"""AI spend reports by payer and billing account: dimensions, filters, organization scope, reimbursable.

The world of ``test_ai_spend_reports`` with the prefix ``rb`` (projects
``RBA`` and ``RBB``), plus who pays. ``RBA`` is company work (payer default
``employer``), ``RBB`` a personal project (default ``employee``).

Billing accounts: ``Corp gateway`` (instance, employer); alice's ``Alice Max``
(the account of her 100 USD subscription, share 0.5) and ``Alice key`` (API
key, share 1); the manager's ``Mgr key`` (API key, share 0.25). All employee
accounts but the gateway.

March 2026 entries:

1. alice, RBA, subscription, API value 30, ``Alice Max`` (carries 75 of the fee)
2. alice, RBB, subscription, API value 10, ``Alice Max`` (carries 25)
3. alice, RBA, api 10, ``Alice key``, route ``openai``
4. alice, RBB, api 4, ``Alice key`` (personal project: never reimbursable)
5. bob, RBA, api 20, ``Corp gateway``, payer employer, route ``corp-gw``
6. bob, no project, api 2, payer unknown
7. mgr, RBA, api 8, ``Mgr key``
8. bob, RBA, api 3, payer employee decided by a project default (no account)

Reimbursable: 0.5 x 75 (entry 1) + 1 x 10 (entry 3) + 0.25 x 8 (entry 7) = 49.5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.user import User
from specivo.services.ai_spend_billing import (
    HIDDEN_ACCOUNT,
    BillingFilter,
    parse_payer,
    parse_scope,
    reimbursable_allowed,
    reimbursable_amount,
    resolve_billing_account,
    route_filter,
)
from specivo.services.ai_spend_report_service import ReportFilters, report_csv
from specivo.services.ai_usage_service import AiUsageService
from tests.services.test_ai_spend_reports import World, amount, build_world, by, entry, run

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


@dataclass
class Billing:
    w: World
    gateway: AiBillingAccount
    alice_max: AiBillingAccount
    alice_key: AiBillingAccount
    mgr_key: AiBillingAccount
    entries: dict[int, AiUsageEntry]


def _account(
    slug: str, name: str, kind: str, payer: str, owner: User | None = None, share: str | None = None
) -> AiBillingAccount:
    return AiBillingAccount(
        slug=slug,
        name=name,
        kind=kind,
        owner_user_id=owner.id if owner is not None else None,
        payer=payer,
        reimburse_share=Decimal(share) if share is not None else None,
    )


async def build_billing(db: AsyncSession, prefix: str = "rb") -> Billing:
    """The world above; *prefix* keeps project keys, logins and account slugs unique to the calling module."""
    w = await build_world(db, prefix)
    w.p.settings = {"ai_usage_payer_default": "employer"}
    w.q.settings = {"ai_usage_payer_default": "employee"}
    gateway = _account(f"{prefix}-gateway", "Corp gateway", "gateway", "employer")
    alice_max = _account(f"{prefix}-alice-max", "Alice Max", "subscription", "employee", w.alice, "0.5")
    alice_key = _account(f"{prefix}-alice-key", "Alice key", "api_key", "employee", w.alice, "1")
    mgr_key = _account(f"{prefix}-mgr-key", "Mgr key", "api_key", "employee", w.mgr, "0.25")
    db.add_all([gateway, alice_max, alice_key, mgr_key])
    await db.flush()
    sub = w.subs["alice"]
    sub.billing_account_id = alice_max.id
    await db.flush()

    def day(n: int) -> date:
        return date(2026, 3, n)

    employee = {"payer": "employee"}
    e = {
        1: await entry(
            db,
            w.alice,
            project=w.p,
            day=day(5),
            api="30",
            cost_type="subscription",
            subscription=sub,
            billing_account_id=alice_max.id,
            billing_method="subscription",
            **employee,
        ),
        2: await entry(
            db,
            w.alice,
            project=w.q,
            day=day(6),
            api="10",
            cost_type="subscription",
            subscription=sub,
            billing_account_id=alice_max.id,
            billing_method="subscription",
            **employee,
        ),
        3: await entry(
            db,
            w.alice,
            project=w.p,
            day=day(7),
            api="10",
            billing_account_id=alice_key.id,
            billing_method="record",
            route="openai",
            **employee,
        ),
        4: await entry(db, w.alice, project=w.q, day=day(8), api="4", billing_account_id=alice_key.id, **employee),
        5: await entry(
            db,
            w.bob,
            project=w.p,
            day=day(9),
            api="20",
            payer="employer",
            billing_account_id=gateway.id,
            billing_method="instance_route",
            route="corp-gw",
        ),
        6: await entry(db, w.bob, project=None, day=day(10), api="2"),
        7: await entry(db, w.mgr, project=w.p, day=day(11), api="8", billing_account_id=mgr_key.id, **employee),
        8: await entry(db, w.bob, project=w.p, day=day(12), api="3", billing_method="project_default", **employee),
    }
    await db.commit()
    return Billing(w=w, gateway=gateway, alice_max=alice_max, alice_key=alice_key, mgr_key=mgr_key, entries=e)


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session)


def _payers(report: object) -> dict[str, object]:
    return {r.key["payer"]: r for r in report.payer_totals}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


async def test_filter_values() -> None:
    assert parse_payer(" Employer ") == "employer"
    assert parse_payer("") is None
    with pytest.raises(AppError) as exc:
        parse_payer("company")
    assert exc.value.field == "payer"
    assert parse_scope("organization") is True
    assert parse_scope("all") is False and parse_scope(None) is False
    with pytest.raises(AppError) as exc:
        parse_scope("mine")
    assert exc.value.field == "scope"
    assert route_filter("-") == {"without_route": True}
    assert route_filter(" corp-gw ") == {"route": "corp-gw"}
    assert route_filter("") == {}
    assert reimbursable_amount(Decimal("0.5"), Decimal(10), Decimal(75)) == Decimal("42.5")
    assert reimbursable_amount(Decimal(1), None, None) == 0


async def test_billing_filters_never_match_a_seat_fee() -> None:
    for fields in (
        {"payer": "employee"},
        {"billing_account_id": 3},
        {"without_billing_account": True},
        {"route": "x"},
        {"without_route": True},
        {"organization": True},
    ):
        assert ReportFilters(**fields).excludes_seats, fields
        assert BillingFilter(**fields).active
    assert not BillingFilter().active


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


async def test_payer_rows_and_totals(db_session: AsyncSession, b: Billing) -> None:
    report = await run(db_session, b.w.admin, group_by=["payer"])
    assert {r.key["payer"]: r.labels["payer"] for r in report.rows if not r.seat_only} == {
        "employee": "Employee",
        "employer": "Employer",
        "unknown": "Unknown",
    }
    payers = _payers(report)
    assert list(payers) == ["employer", "employee", "unknown"]
    employer, employee, unknown = payers["employer"], payers["employee"], payers["unknown"]
    assert amount(employer.total_spend) == Decimal(20)
    assert amount(employee.marginal) == Decimal(25)
    assert amount(employee.seat_allocated) == Decimal(100)
    assert amount(employee.total_spend) == Decimal(125)
    assert amount(employee.api_equivalent) == Decimal(65)
    assert employee.leverage == Decimal("0.40")
    # The unallocated fee of dave's plan (no account) is the unknown payer's.
    assert amount(unknown.seat_unallocated) == Decimal(15)
    # The payer totals add up to the report's.
    assert sum(amount(p.total_spend) for p in payers.values()) == amount(report.totals.total_spend) == Decimal(162)
    assert sum(p.entries for p in payers.values()) == report.totals.entries == 8


async def test_billing_account_names_follow_who_may_see_the_account(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    admin = await run(db, b.w.admin, b.w.p, group_by=["billing_account"], include_seat_cost=False)
    # Entry 8 has no account: no label.
    labels = {r.labels["billing_account"] for r in admin.rows}
    assert labels == {"Alice Max", "Alice key", "Corp gateway", "Mgr key", ""}

    # The manager reads every entry of RBA but not alice's personal accounts: one "Personal account" row.
    mgr = await run(db, b.w.mgr, b.w.p, group_by=["billing_account"], include_seat_cost=False)
    labels = {r.key["billing_account"]: r.labels["billing_account"] for r in mgr.rows}
    assert labels == {
        b.gateway.id: "Corp gateway",
        b.mgr_key.id: "Mgr key",
        HIDDEN_ACCOUNT: "Personal account",
        None: "",
    }
    assert by(mgr, billing_account=HIDDEN_ACCOUNT).entries == 2
    assert "Alice" not in report_csv(mgr)

    # Alice sees her own accounts by name.
    alice = await run(db, b.w.alice, b.w.p, group_by=["billing_account"], include_seat_cost=False)
    assert {r.labels["billing_account"] for r in alice.rows} == {"Alice Max", "Alice key"}


async def test_route_rows_and_filter(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    report = await run(db, b.w.admin, b.w.p, group_by=["route"], include_seat_cost=False)
    assert {r.key["route"] for r in report.rows} == {"openai", "corp-gw", None}
    gw = await run(db, b.w.admin, b.w.p, filters=ReportFilters(route="corp-gw"), include_seat_cost=False)
    assert gw.totals.entries == 1 and amount(gw.totals.api_equivalent) == Decimal(20)
    none = await run(db, b.w.admin, b.w.p, filters=ReportFilters(without_route=True), include_seat_cost=False)
    assert none.totals.entries == 3


async def test_payer_and_account_filters(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    employer = await run(db, b.w.admin, filters=ReportFilters(payer="employer"))
    assert employer.totals.entries == 1
    assert not employer.unallocated_shown
    assert amount(employer.totals.total_spend) == Decimal(20)

    key = await run(db, b.w.admin, filters=ReportFilters(billing_account_id=b.alice_key.id), include_seat_cost=False)
    assert key.totals.entries == 2
    without = await run(db, b.w.admin, filters=ReportFilters(without_billing_account=True), include_seat_cost=False)
    assert without.totals.entries == 2  # entries 6 and 8
    nothing = await run(db, b.w.admin, filters=ReportFilters(billing_account_id=-1), include_seat_cost=False)
    assert nothing.totals.entries == 0


async def test_resolving_a_billing_account_filter(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    # Someone else's personal account matches nothing and is not named.
    hidden = await resolve_billing_account(db, b.w.mgr, str(b.alice_key.id))
    assert hidden.fields == {"billing_account_id": -1} and hidden.label == str(b.alice_key.id) and not hidden.found
    by_slug = await resolve_billing_account(db, b.w.mgr, "rb-gateway")
    assert by_slug.fields == {"billing_account_id": b.gateway.id} and by_slug.label == "Corp gateway"
    own = await resolve_billing_account(db, b.w.alice, "rb-alice-key")
    assert own.fields == {"billing_account_id": b.alice_key.id}
    assert (await resolve_billing_account(db, b.w.mgr, "rb-alice-key")).fields == {"billing_account_id": -1}
    admin = await resolve_billing_account(db, b.w.admin, str(b.alice_key.id))
    assert admin.label == "Alice key"
    assert (await resolve_billing_account(db, b.w.mgr, "-")).fields == {"without_billing_account": True}
    assert (await resolve_billing_account(db, b.w.mgr, "999999999")).fields == {"billing_account_id": -1}


# ---------------------------------------------------------------------------
# Organization scope
# ---------------------------------------------------------------------------


async def test_organization_scope_leaves_personal_usage_out(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    org = await run(db, b.w.admin, group_by=["project"], filters=ReportFilters(organization=True))
    # No project (6), the personal project RBB (2, 4), and an employee payer from a project default (8).
    assert {r.key["project"] for r in org.rows} == {b.w.p.id}
    assert org.totals.entries == 4
    assert amount(org.totals.api_equivalent) == Decimal(68)
    assert not org.unallocated_shown
    # Allocated seat cost of the company-project entry stays.
    assert amount(org.totals.seat_allocated) == Decimal(75)

    entries, total = await AiUsageService().list_entries(
        db, b.w.admin, billing=BillingFilter(payer="employee", organization=True), limit=50
    )
    assert total == 3
    assert {e.id for e in entries} == {b.entries[1].id, b.entries[3].id, b.entries[7].id}


# ---------------------------------------------------------------------------
# Reimbursable
# ---------------------------------------------------------------------------


async def test_reimbursable_for_administrators(db_session: AsyncSession, b: Billing) -> None:
    report = await run(db_session, b.w.admin, group_by=["user"])
    assert report.reimbursable_shown
    assert amount(report.totals.reimbursable) == Decimal("49.5")
    assert amount(by(report, user=b.w.alice.id).reimbursable) == Decimal("47.5")
    assert amount(by(report, user=b.w.mgr.id).reimbursable) == Decimal(2)
    assert amount(by(report, user=b.w.bob.id).reimbursable) == 0
    payers = _payers(report)
    assert amount(payers["employee"].reimbursable) == Decimal("49.5")
    assert amount(payers["employer"].reimbursable) == 0


async def test_reimbursable_only_to_administrators_and_the_holder(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    w = b.w
    # The manager sees seat cost in RBA, but not everyone's reimbursable amounts.
    team = await run(db, w.mgr, w.p, group_by=["user"])
    assert team.seat_cost_shown and not team.reimbursable_shown
    assert team.totals.reimbursable is None
    assert "reimbursable" not in report_csv(team).splitlines()[0]
    # Their own usage: yes.
    own = await run(db, w.mgr, w.p, group_by=["user"], filters=ReportFilters(user_id=w.mgr.id))
    assert own.reimbursable_shown and amount(own.totals.reimbursable) == Decimal(2)
    # Alice reads only her own entries, so she sees no seat cost and no reimbursable amount.
    alice = await run(db, w.alice, w.p, filters=ReportFilters(user_id=w.alice.id))
    assert not alice.seat_cost_shown and not alice.reimbursable_shown
    # Leaving seat cost out leaves reimbursable out too.
    noseat = await run(db, w.admin, include_seat_cost=False)
    assert not noseat.reimbursable_shown


async def test_reimbursable_rule() -> None:
    admin = User(id=1, login="x", is_admin=True)
    person = User(id=2, login="y", is_admin=False)
    assert reimbursable_allowed(admin, None, True)
    assert not reimbursable_allowed(admin, None, False)
    assert reimbursable_allowed(person, 2, True)
    assert not reimbursable_allowed(person, None, True)
    assert not reimbursable_allowed(person, 3, True)


async def test_csv_has_reimbursable_and_payer_totals(db_session: AsyncSession, b: Billing) -> None:
    report = await run(db_session, b.w.admin, group_by=["billing_account"])
    lines = report_csv(report).splitlines()
    header = lines[0].split(",")
    assert header[1] == "billing_account" and "reimbursable" in header
    col = header.index("reimbursable")
    total = next(line for line in lines if line.startswith("total,")).split(",")
    assert total[col] == "49.50"
    payer_lines = [line.split(",") for line in lines if line.startswith("total:")]
    assert [cells[0] for cells in payer_lines] == ["total:employer", "total:employee", "total:unknown"]
    assert all(cells[1] == "" for cells in payer_lines)
    assert dict((cells[0], cells[col]) for cells in payer_lines)["total:employee"] == "49.50"
