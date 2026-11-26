"""Company seat plans: the register, seats by date, coverage and billing precedence, audit, the API and pages.

The world: ``admin`` manages a company plan *Claude Team* (300 USD a month, 10
seats, covering ``claude-code``, from 1 January 2026). ``alice`` also holds her
own *Claude Max 5x* covering ``claude-code``; ``bob`` has no plan of his own;
``carol`` holds no seat. The project ``CSP`` is company work (payer default
``employer``). One model is priced at 2 USD per million input tokens.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import PROJECT_PAYER_DEFAULT_KEY
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_subscription import AiSubscription, AiSubscriptionMember
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.ai_subscription_service import AiSubscriptionService, SubscriptionInput
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/ai-usage/ingest"
MODEL = "lab-seat-1"
NOV_1 = date(2026, 11, 1)

_service = AiSubscriptionService()
_accounts = AiBillingAccountService()


@dataclass
class World:
    admin: User
    alice: User
    bob: User
    carol: User
    runner: User
    project: Project
    plan: AiSubscription
    personal: AiSubscription
    keys: dict[str, str]

    def auth(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[name]}"}


def _plan_input(**values: Any) -> SubscriptionInput:
    fields: dict[str, Any] = {
        "plan_key": "custom",
        "provider": "anthropic",
        "plan_name": "Claude Team",
        "tool_slugs": ["claude-code"],
        "amount": "300",
        "currency": "USD",
        "starts_on": date(2026, 1, 1),
        "seat_count": 10,
    }
    fields.update(values)
    return SubscriptionInput(**fields)


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    admin: User = AdminUserFactory.build(login="csp_admin", status="active")
    alice: User = UserFactory.build(login="csp_alice", status="active", display_name="Alice")
    bob: User = UserFactory.build(login="csp_bob", status="active", display_name="Bob")
    carol: User = UserFactory.build(login="csp_carol", status="active", display_name="Carol")
    runner: User = ServiceAccountFactory.build(login="csp_runner", status="active")
    project: Project = ProjectFactory.build(
        key="CSP", identifier="csp", is_public=False, settings={PROJECT_PAYER_DEFAULT_KEY: "employer"}
    )
    db.add_all([admin, alice, bob, carol, runner, project])
    await db.flush()
    role = Role(
        name="Csp Logger",
        permissions=["view_issues", "view_ai_usage", "log_ai_usage"],
        builtin=0,
        issues_visibility="default",
        ai_usage_visibility="own",
    )
    db.add(role)
    await db.flush()
    for user in (alice, bob, carol):
        member = Member(project_id=project.id, user_id=user.id)
        db.add(member)
        await db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    await AiModelPriceService().create(
        db,
        PriceInput(
            provider="testlab",
            model=MODEL,
            currency="USD",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            prices={"input": "2", "output": "10"},
        ),
        actor=admin,
    )
    personal = await _service.create(
        db,
        SubscriptionInput(plan_key="claude-max-5x", starts_on=date(2026, 1, 1), tool_slugs=["claude-code"]),
        owner=alice,
        actor=admin,
    )
    plan = await _service.create(db, _plan_input(), owner=None, actor=admin)
    keys: dict[str, str] = {}
    for name, user in (("alice", alice), ("bob", bob), ("carol", carol)):
        _key, raw = await ApiKeyService().create_key(db, user_id=user.id, name=f"{name}-key", check_scope_access=False)
        keys[name] = raw
    await db.commit()
    clear_role_cache(db)
    return World(admin, alice, bob, carol, runner, project, plan, personal, keys)


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


async def _seat(w: World, db: AsyncSession, user: User, starts: date = NOV_1, ends: date | None = None):
    member = await _service.add_member(db, w.plan, user, starts_on=starts, ends_on=ends, actor=w.admin)
    await db.commit()
    return member


def record(key: str, day: str = "2026-11-05", **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": key,
        "tool": "claude-code",
        "provider": "testlab",
        "model": MODEL,
        "usage_date": day,
        "tokens": {"input": 1_000_000},
        "attribution": {"project_key": "CSP"},
    }
    values.update(kw)
    return values


async def ingest(client: AsyncClient, w: World, who: str, rec: dict[str, Any]) -> dict[str, Any]:
    body = {"schema": "specivo.ai-usage.v1", "client": {"name": "seat-tests"}, "records": [rec]}
    resp = await client.post(URL, json=body, headers=w.auth(who))
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["status"] != "rejected", result
    return result


async def stored(db: AsyncSession, entry_id: int) -> AiUsageEntry:
    entry = await db.get(AiUsageEntry, entry_id)
    assert entry is not None
    await db.refresh(entry)
    return entry


async def _company_account(db: AsyncSession, w: World) -> AiBillingAccount:
    account = await db.get(AiBillingAccount, w.plan.billing_account_id)
    assert account is not None
    return account


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


async def test_a_company_plan_is_the_fee_schedule_of_an_employer_account(db_session: AsyncSession, w: World) -> None:
    plan = w.plan
    assert (plan.user_id, plan.seat_count, plan.is_company) == (None, 10, True)
    account = await _company_account(db_session, w)
    assert (account.owner_user_id, account.kind, account.payer) == (None, "subscription", "employer")
    assert (account.default_cost_type, account.reimburse_share, account.tool_slugs) == ("subscription", None, [])
    assert account.slug == f"custom-{plan.id}" and account.name == "Claude Team"

    [created] = [r for r in await _audit(db_session, AuditEvent.AI_SUBSCRIPTION_CREATED) if r.resource_id == plan.id]
    assert created.user_id == w.admin.id
    assert created.details["target_login"] is None and created.details["seat_count"] == 10
    assert created.details["amount"] == "300.00"


async def test_company_plans_are_for_administrators_and_need_seats(db_session: AsyncSession, w: World) -> None:
    db = db_session
    with pytest.raises(PermissionDeniedError):
        await _service.create(db, _plan_input(), owner=None, actor=w.alice)
    with pytest.raises(AppError) as exc:
        await _service.create(db, _plan_input(seat_count=None), owner=None, actor=w.admin)
    assert (exc.value.code, exc.value.field) == ("ai_subscription_invalid", "seat_count")
    with pytest.raises(AppError) as exc:
        await _service.create(db, _plan_input(seat_count=0), owner=None, actor=w.admin)
    assert exc.value.field == "seat_count"
    with pytest.raises(PermissionDeniedError):
        await _service.update(db, w.plan, {"plan_name": "Mine"}, actor=w.alice)
    with pytest.raises(PermissionDeniedError):
        await _service.delete(db, w.plan, actor=w.alice)


async def test_only_a_company_plan_may_cost_nothing(db_session: AsyncSession, w: World) -> None:
    db = db_session
    free = await _service.create(db, _plan_input(amount="0", plan_name="Booked elsewhere"), owner=None, actor=w.admin)
    assert free.amount == 0
    with pytest.raises(AppError) as exc:
        await _service.create(
            db, SubscriptionInput(plan_key="claude-pro", amount="0", starts_on=NOV_1), owner=w.bob, actor=w.bob
        )
    assert (exc.value.code, exc.value.field) == ("ai_subscription_invalid", "amount")
    with pytest.raises(AppError) as exc:
        await _service.create(db, _plan_input(amount="-1"), owner=None, actor=w.admin)
    assert exc.value.field == "amount"
    # A person's plan has no seats.
    with pytest.raises(AppError) as exc:
        await _service.create(
            db, SubscriptionInput(plan_key="claude-pro", starts_on=NOV_1, seat_count=3), owner=w.bob, actor=w.bob
        )
    assert exc.value.field == "seat_count"


async def test_the_company_accounts_payer_stays_the_employer(db_session: AsyncSession, w: World) -> None:
    account = await _company_account(db_session, w)
    with pytest.raises(AppError) as exc:
        await _accounts.update(db_session, account, {"payer": "employee"}, actor=w.admin)
    assert (exc.value.code, exc.value.field) == ("ai_billing_account_invalid", "payer")
    # Other changes are fine.
    await _accounts.update(db_session, account, {"name": "Claude Team (company)"}, actor=w.admin)
    assert account.payer == "employer"


async def test_people_and_the_personal_listings_never_see_a_company_plan(db_session: AsyncSession, w: World) -> None:
    db = db_session
    rows, total = await _service.list_subscriptions(db)
    assert w.plan.id not in {r.id for r in rows}
    company, count = await _service.list_subscriptions(db, company=True)
    assert [r.id for r in company] == [w.plan.id] and count == 1
    with pytest.raises(AppError):
        await _service.get_personal(db, w.plan.id)
    with pytest.raises(AppError):
        await _service.get_company(db, w.personal.id)
    with pytest.raises(AppError):
        await _service.get_visible(db, w.alice, w.plan.id)


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------


async def test_seats_by_date_with_overlaps_and_the_seat_limit(db_session: AsyncSession, w: World) -> None:
    db = db_session
    plan = await _service.create(db, _plan_input(plan_name="Two seats", seat_count=2), owner=None, actor=w.admin)
    alice = await _service.add_member(db, plan, w.alice, starts_on=NOV_1, actor=w.admin)
    bob = await _service.add_member(db, plan, w.bob, starts_on=NOV_1, ends_on=date(2026, 11, 15), actor=w.admin)

    # Every seat is taken until bob leaves.
    with pytest.raises(AppError) as exc:
        await _service.add_member(db, plan, w.carol, starts_on=date(2026, 11, 10), actor=w.admin)
    assert exc.value.code == "ai_subscription_seats_full"
    carol = await _service.add_member(db, plan, w.carol, starts_on=date(2026, 11, 16), actor=w.admin)

    # Alice holds a seat already.
    with pytest.raises(AppError) as exc:
        await _service.add_member(db, plan, w.alice, starts_on=date(2026, 12, 1), actor=w.admin)
    assert exc.value.code == "ai_subscription_member_overlap"
    # Service accounts hold none; people cannot hand out seats.
    with pytest.raises(AppError) as exc:
        await _service.add_member(db, plan, w.runner, starts_on=NOV_1, actor=w.admin)
    assert exc.value.code == "ai_subscription_member_invalid"
    with pytest.raises(PermissionDeniedError):
        await _service.add_member(db, plan, w.carol, starts_on=date(2027, 1, 1), actor=w.alice)
    # A personal subscription has no seats.
    with pytest.raises(AppError) as exc:
        await _service.add_member(db, w.personal, w.bob, starts_on=NOV_1, actor=w.admin)
    assert exc.value.code == "ai_subscription_member_invalid"
    # Bad days.
    with pytest.raises(AppError) as exc:
        await _service.add_member(
            db, plan, w.alice, starts_on=date(2027, 2, 1), ends_on=date(2027, 1, 1), actor=w.admin
        )
    assert exc.value.field == "ends_on"

    # Bob staying longer would overlap carol's seat.
    with pytest.raises(AppError) as exc:
        await _service.update_member(db, plan, bob, {"ends_on": date(2026, 11, 20)}, actor=w.admin)
    assert exc.value.code == "ai_subscription_seats_full"
    # Two people hold a seat at once, so the plan keeps two seats.
    with pytest.raises(AppError) as exc:
        await _service.update(db, plan, {"seat_count": 1}, actor=w.admin)
    assert exc.value.code == "ai_subscription_seats_full"
    await _service.update(db, plan, {"seat_count": 3}, actor=w.admin)

    # Alice leaves; the seat keeps its days.
    await _service.update_member(db, plan, alice, {"ends_on": date(2026, 11, 30)}, actor=w.admin)
    await _service.remove_member(db, plan, carol, actor=w.admin)
    await db.commit()
    seats = await _service.list_members(db, plan)
    assert [(m.user_id, m.starts_on, m.ends_on) for m in seats] == [
        (w.alice.id, NOV_1, date(2026, 11, 30)),
        (w.bob.id, NOV_1, date(2026, 11, 15)),
    ]


async def test_seat_changes_are_audited(db_session: AsyncSession, w: World) -> None:
    db = db_session
    member = await _seat(w, db, w.bob)
    await _service.update_member(db, w.plan, member, {"ends_on": date(2026, 12, 31)}, actor=w.admin)
    await _service.remove_member(db, w.plan, member, actor=w.admin)
    await db.commit()

    [added] = await _audit(db, AuditEvent.AI_SUBSCRIPTION_MEMBER_ADDED)
    assert added.user_id == w.admin.id and added.resource_type == "ai_subscription" and added.resource_id == w.plan.id
    assert added.details == {
        "subscription_id": w.plan.id,
        "plan_name": "Claude Team",
        "member_id": member.id,
        "target_user_id": w.bob.id,
        "target_login": "csp_bob",
        "starts_on": "2026-11-01",
        "ends_on": None,
    }
    [updated] = await _audit(db, AuditEvent.AI_SUBSCRIPTION_MEMBER_UPDATED)
    assert updated.details["changes"] == {"ends_on": {"old": None, "new": "2026-12-31"}}
    [removed] = await _audit(db, AuditEvent.AI_SUBSCRIPTION_MEMBER_REMOVED)
    assert removed.details["ends_on"] == "2026-12-31"

    await _service.update(db, w.plan, {"seat_count": 12}, actor=w.admin)
    updates = [r for r in await _audit(db, AuditEvent.AI_SUBSCRIPTION_UPDATED) if r.resource_id == w.plan.id]
    assert updates[-1].details["changes"] == {"seat_count": {"old": 10, "new": 12}}


async def test_deleting_a_plan_deletes_its_seats_and_keeps_its_account(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await _seat(w, db, w.bob)
    account_id, plan_id = w.plan.billing_account_id, w.plan.id
    await _service.delete(db, w.plan, actor=w.admin)
    await db.commit()
    left = await db.execute(select(AiSubscriptionMember).where(AiSubscriptionMember.subscription_id == plan_id))
    assert left.scalars().all() == []
    assert await db.get(AiBillingAccount, account_id) is not None
    [deleted] = [r for r in await _audit(db, AuditEvent.AI_SUBSCRIPTION_DELETED) if r.resource_id == plan_id]
    assert deleted.details["seat_count"] == 10


# ---------------------------------------------------------------------------
# Coverage and billing resolution
# ---------------------------------------------------------------------------


async def test_coverage_tries_the_company_seat_before_the_persons_own_plan(db_session: AsyncSession, w: World) -> None:
    db = db_session
    await _seat(w, db, w.alice, ends=date(2026, 11, 30))
    at = datetime(2026, 11, 5, tzinfo=UTC)
    covering = await _service.coverages(db, w.alice.id, "claude-code", None, at)
    assert [c.subscription.id for c in covering] == [w.plan.id, w.personal.id]
    assert (await _service.subscription_for(db, w.alice.id, "claude-code", None, at)).subscription.id == w.plan.id
    # After the seat ends, her own plan.
    later = datetime(2026, 12, 1, tzinfo=UTC)
    assert (await _service.subscription_for(db, w.alice.id, "claude-code", None, later)).subscription.id == (
        w.personal.id
    )
    # Not a member: no seat.
    assert await _service.subscription_for(db, w.carol.id, "claude-code", None, at) is None
    # A tool the plan does not cover.
    assert await _service.coverages(db, w.bob.id, "codex", None, at) == []


async def test_a_members_usage_is_paid_by_the_employer_through_the_plan(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    await _seat(w, db, w.alice, ends=date(2026, 11, 10))
    await _seat(w, db, w.bob)
    account = await _company_account(db, w)

    for who in ("alice", "bob"):
        result = await ingest(client, w, who, record(f"k-{who}"))
        assert (result["payer"], result["billing_account"]) == ("employer", account.slug), who
        entry = await stored(db, result["entry_id"])
        assert (entry.billing_method, entry.cost_type, entry.subscription_id) == (
            "subscription",
            "subscription",
            w.plan.id,
        )
        assert entry.billing_account_id == account.id
        assert entry.marginal is not None and entry.marginal.amount == 0

    # After her seat ends, alice's usage goes to her own plan, paid by her.
    later = await ingest(client, w, "alice", record("k-alice-later", day="2026-11-12"))
    entry = await stored(db, later["entry_id"])
    assert (entry.payer, entry.subscription_id) == ("employee", w.personal.id)

    # Carol holds no seat: the project's payer default decides, with no account.
    carol = await stored(db, (await ingest(client, w, "carol", record("k-carol")))["entry_id"])
    assert (carol.billing_method, carol.payer, carol.billing_account_id) == ("project_default", "employer", None)
    assert carol.subscription_id is None


async def test_a_plan_whose_account_is_off_is_skipped(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    db = db_session
    await _seat(w, db, w.alice)
    await _accounts.deactivate(db, await _company_account(db, w), actor=w.admin)
    await db.commit()
    entry = await stored(db, (await ingest(client, w, "alice", record("k-off")))["entry_id"])
    assert (entry.payer, entry.subscription_id) == ("employee", w.personal.id)


async def test_a_seat_added_later_moves_past_usage_onto_the_plan(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    before = await ingest(client, w, "bob", record("k-before"))
    entry = await stored(db, before["entry_id"])
    assert (entry.billing_method, entry.subscription_id) == ("project_default", None)

    member = await _seat(w, db, w.bob)
    entry = await stored(db, before["entry_id"])
    assert (entry.billing_method, entry.payer, entry.subscription_id) == ("subscription", "employer", w.plan.id)
    assert entry.revision == 2

    # Ending the seat before the usage day takes it off the plan again; earlier days keep it.
    await _service.update_member(db, w.plan, member, {"ends_on": date(2026, 11, 3)}, actor=w.admin)
    await db.commit()
    entry = await stored(db, before["entry_id"])
    assert (entry.billing_method, entry.subscription_id) == ("project_default", None)
    early = await stored(db, (await ingest(client, w, "bob", record("k-early", day="2026-11-02")))["entry_id"])
    assert early.subscription_id == w.plan.id

    # Removing the seat (a mistake) takes every day off the plan.
    await _service.remove_member(db, w.plan, member, actor=w.admin)
    await db.commit()
    early = await stored(db, early.id)
    assert (early.billing_method, early.subscription_id) == ("project_default", None)
    recomputed = await _audit(db, AuditEvent.AI_USAGE_COSTS_RECOMPUTED)
    assert recomputed and recomputed[0].details["reason"] == "billing"


async def test_naming_the_plans_account_charges_the_plan_only_on_seat_days(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db = db_session
    await _seat(w, db, w.bob)
    slug = (await _company_account(db, w)).slug
    bob = await stored(db, (await ingest(client, w, "bob", record("k-slug-bob", billing_account=slug)))["entry_id"])
    assert (bob.billing_method, bob.cost_type, bob.subscription_id) == ("record", "subscription", w.plan.id)
    carol = await stored(
        db, (await ingest(client, w, "carol", record("k-slug-carol", billing_account=slug)))["entry_id"]
    )
    # An instance account any person may name, but carol holds no seat: no plan to share.
    assert (carol.billing_method, carol.payer, carol.subscription_id) == ("record", "employer", None)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def test_the_company_plan_api(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    admin = _bearer(w.admin)
    base = "/api/v1/admin/ai-company-plans/"
    resp = await client.post(
        base,
        headers=admin,
        json={
            "plan_name": "Codex Team",
            "provider": "openai",
            "tool_slugs": ["codex"],
            "amount": "0",
            "currency": "USD",
            "starts_on": "2026-11-01",
            "seat_count": 5,
        },
    )
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert (plan["user_id"], plan["seat_count"], plan["seats_held"], plan["amount"]) == (None, 5, 0, "0.00")
    plan_id = plan["id"]

    listed = (await client.get(base, headers=admin)).json()
    assert {p["id"] for p in listed["items"]} == {plan_id, w.plan.id}

    resp = await client.post(f"{base}{plan_id}/members/", headers=admin, json={"login": "csp_bob"})
    assert resp.status_code == 201, resp.text
    member = resp.json()
    assert member["user_login"] == "csp_bob" and member["ends_on"] is None
    resp = await client.post(f"{base}{plan_id}/members/", headers=admin, json={"user_id": w.bob.id})
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "ai_subscription_member_overlap"
    resp = await client.post(f"{base}{plan_id}/members/", headers=admin, json={"login": "nobody-here"})
    assert resp.status_code == 422

    resp = await client.patch(f"{base}{plan_id}/members/{member['id']}/", headers=admin, json={"ends_on": "2026-12-31"})
    assert resp.status_code == 200 and resp.json()["ends_on"] == "2026-12-31"
    members = (await client.get(f"{base}{plan_id}/members/", headers=admin)).json()
    assert [m["id"] for m in members] == [member["id"]]

    resp = await client.patch(f"{base}{plan_id}/", headers=admin, json={"seat_count": 7, "amount": "70"})
    assert resp.status_code == 200 and (resp.json()["seat_count"], resp.json()["amount"]) == (7, "70.00")

    assert (await client.delete(f"{base}{plan_id}/members/{member['id']}/", headers=admin)).status_code == 204
    assert (await client.delete(f"{base}{plan_id}/", headers=admin)).status_code == 204
    assert (await client.get(f"{base}{plan_id}/", headers=admin)).status_code == 404


async def test_the_api_keeps_company_plans_and_personal_ones_apart(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    admin = _bearer(w.admin)
    assert (await client.get(f"/api/v1/admin/ai-subscriptions/{w.plan.id}/", headers=admin)).status_code == 404
    assert (await client.get(f"/api/v1/admin/ai-company-plans/{w.personal.id}/", headers=admin)).status_code == 404
    listed = (await client.get("/api/v1/admin/ai-subscriptions/", headers=admin)).json()
    assert w.plan.id not in {s["id"] for s in listed["items"]}
    # People cannot reach company plans at all.
    assert (await client.get("/api/v1/admin/ai-company-plans/", headers=_bearer(w.alice))).status_code == 403
    resp = await client.post(
        f"/api/v1/admin/ai-company-plans/{w.plan.id}/members/", headers=_bearer(w.alice), json={"login": "csp_alice"}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _csrf(client: AsyncClient, user: User, page: str) -> str:
    resp = await client.get(page, cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError(f"no csrf_token cookie on {page}")


async def _post_form(client: AsyncClient, user: User, url: str, data: Mapping[str, object], page: str):
    csrf = await _csrf(client, user, page)
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


async def test_the_admin_pages(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    page = "/admin/ai-company-plans/"
    resp = await client.get(page, cookies=_cookies(w.admin))
    assert resp.status_code == 200 and "Claude Team" in resp.text and 'name="seat_count"' in resp.text
    assert (await client.get(page, cookies=_cookies(w.alice), follow_redirects=False)).status_code in (302, 303, 403)

    resp = await _post_form(
        client,
        w.admin,
        page,
        {
            "plan_key": "custom",
            "plan_name": "Gemini Team",
            "provider": "gemini",
            "amount": "100",
            "currency": "USD",
            "billing_period": "month",
            "starts_on": "2026-11-01",
            "seat_count": "4",
        },
        page,
    )
    assert resp.status_code == 303, resp.text
    plan_url = resp.headers["location"].split("?")[0]
    plan_id = int(plan_url.rstrip("/").rsplit("/", 1)[1])

    resp = await _post_form(
        client, w.admin, f"{plan_url}members/", {"login": "csp_carol", "starts_on": "2026-11-01"}, plan_url
    )
    assert resp.status_code == 303, resp.text
    shown = await client.get(plan_url, cookies=_cookies(w.admin))
    assert shown.status_code == 200 and "csp_carol" in shown.text

    resp = await _post_form(client, w.admin, f"{plan_url}members/", {"login": "nobody", "starts_on": ""}, plan_url)
    assert resp.status_code == 422 and "No person has that login." in resp.text

    resp = await _post_form(client, w.admin, plan_url, {"seat_count": "", "plan_name": "", "amount": ""}, plan_url)
    assert resp.status_code == 422

    [member] = (
        await db_session.execute(select(AiSubscriptionMember).where(AiSubscriptionMember.subscription_id == plan_id))
    ).scalars()
    resp = await _post_form(client, w.admin, f"{plan_url}members/{member.id}/", {"ends_on": "2026-11-20"}, plan_url)
    assert resp.status_code == 303
    await db_session.refresh(member)
    assert member.ends_on == date(2026, 11, 20)
    # The personal subscription pages do not open a company plan.
    assert (await client.get(f"/admin/ai-subscriptions/{plan_id}/", cookies=_cookies(w.admin))).status_code == 404
