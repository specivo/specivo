"""Billing account and payer of AI usage entries: resolution at ingest, replays and re-resolution.

The world: ``alice`` (logs her own usage in the project ``BILR``, which has no
payer default, and ``BILS``, whose default is ``employer``) holds a
subscription covering ``claude-code``; ``bob`` is another person; ``admin``
manages the billing accounts. One model is priced at 2 USD per million input
tokens, so an ``api`` entry of one million input tokens costs 2 USD and a
``subscription`` one costs nothing at the margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import PROJECT_PAYER_DEFAULT_KEY
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService, BillingAccountInput
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.ai_subscription_service import AiSubscriptionService, SubscriptionInput
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/ai-usage/ingest"
DAY = "2026-11-05"
MODEL = "lab-bill-1"

_accounts = AiBillingAccountService()
_usage = AiUsageService()


@dataclass
class World:
    admin: User
    alice: User
    bob: User
    p: Project
    s: Project
    subscription: AiSubscription
    keys: dict[str, str]

    def auth(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[name]}"}


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    admin: User = AdminUserFactory.build(login="bilr_admin", status="active")
    alice: User = UserFactory.build(login="bilr_alice", status="active")
    bob: User = UserFactory.build(login="bilr_bob", status="active")
    p: Project = ProjectFactory.build(key="BILR", identifier="bilr", is_public=False)
    s: Project = ProjectFactory.build(
        key="BILS", identifier="bils", is_public=False, settings={PROJECT_PAYER_DEFAULT_KEY: "employer"}
    )
    db.add_all([admin, alice, bob, p, s])
    await db.flush()
    role = Role(
        name="Bilr Logger",
        permissions=["view_issues", "view_ai_usage", "log_ai_usage"],
        builtin=0,
        issues_visibility="default",
        ai_usage_visibility="own",
    )
    db.add(role)
    await db.flush()
    for project in (p, s):
        for user in (alice, bob):
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
    subscription = await AiSubscriptionService().create(
        db,
        SubscriptionInput(plan_key="claude-max-5x", starts_on=date(2026, 1, 1), tool_slugs=["claude-code"]),
        owner=alice,
        actor=admin,
    )
    keys: dict[str, str] = {}
    for name, user in (("alice", alice), ("bob", bob)):
        _key, raw = await ApiKeyService().create_key(db, user_id=user.id, name=f"{name}-key", check_scope_access=False)
        keys[name] = raw
    await db.commit()
    clear_role_cache(db)
    return World(admin=admin, alice=alice, bob=bob, p=p, s=s, subscription=subscription, keys=keys)


def record(key: str, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": key,
        "tool": "claude-code",
        "provider": "testlab",
        "model": MODEL,
        "usage_date": DAY,
        "tokens": {"input": 1_000_000},
    }
    values.update(kw)
    return {k: v for k, v in values.items() if v is not None}


async def post(client: AsyncClient, w: World, who: str, *records: dict[str, Any]) -> list[dict[str, Any]]:
    body = {"schema": "specivo.ai-usage.v1", "client": {"name": "billing-tests"}, "records": list(records)}
    resp = await client.post(URL, json=body, headers=w.auth(who))
    assert resp.status_code == 200, resp.text
    results: list[dict[str, Any]] = resp.json()["results"]
    return results


async def one(client: AsyncClient, w: World, who: str, rec: dict[str, Any]) -> dict[str, Any]:
    [result] = await post(client, w, who, rec)
    assert result["status"] != "rejected", result
    return result


async def stored(db: AsyncSession, entry_id: int) -> AiUsageEntry:
    entry = await db.get(AiUsageEntry, entry_id)
    assert entry is not None
    await db.refresh(entry)
    return entry


async def gateway(
    db: AsyncSession, w: World, slug: str = "corp-gw", owner: User | None = None, **values: Any
) -> AiBillingAccount:
    fields: dict[str, Any] = {
        "name": f"Account {slug}",
        "kind": "gateway",
        "payer": "employer",
        "route_patterns": ["corp-*"],
        "default_cost_type": "api",
    }
    fields.update(values)
    account = await _accounts.create(db, BillingAccountInput(slug=slug, **fields), owner=owner, actor=w.admin)
    await db.commit()
    return account


def billing(result: dict[str, Any]) -> tuple[str | None, str | None]:
    return result["payer"], result["billing_account"]


# ---------------------------------------------------------------------------
# Resolution steps and their order
# ---------------------------------------------------------------------------


async def test_the_records_slug_names_the_account(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    await gateway(db_session, w, "corp-gw", route_patterns=[])
    result = await one(client, w, "alice", record("k-slug", billing_account="corp-gw"))
    assert billing(result) == ("employer", "corp-gw")
    assert result["warnings"] == []
    entry = await stored(db_session, result["entry_id"])
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == ("record", "api", None)
    assert entry.requested_cost_type is None
    assert entry.marginal is not None and entry.marginal.amount == Decimal("2")


async def test_the_slug_beats_every_route_and_the_subscription(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "corp-gw")
    await gateway(
        db_session, w, "alice-key", owner=w.alice, kind="api_key", payer="employee", route_patterns=["corp-*"]
    )
    result = await one(client, w, "alice", record("k-slug-first", route="corp-1", billing_account="alice-key"))
    assert billing(result) == ("employee", "alice-key")


async def test_a_slug_the_user_has_no_account_for_is_skipped_with_a_warning(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "corp-gw")
    await gateway(db_session, w, "bob-key", owner=w.bob, kind="api_key", payer="employee", route_patterns=[])
    off = await gateway(db_session, w, "old-gw", route_patterns=[])
    await _accounts.deactivate(db_session, off, actor=w.admin)
    await db_session.commit()

    messages = set()
    for index, slug in enumerate(("bob-key", "no-such-account", "old-gw")):
        result = await one(client, w, "alice", record(f"k-foreign-{index}", route="corp-1", billing_account=slug))
        # The next steps decide: the instance gateway's route pattern.
        assert billing(result) == ("employer", "corp-gw"), slug
        [warning] = result["warnings"]
        assert (warning["code"], warning["field"]) == ("billing_account_unknown", "billing_account")
        messages.add(warning["message"].replace(slug, "<slug>"))
    # Someone else's account, a missing one and a switched-off one read the same.
    assert len(messages) == 1


async def test_an_instance_route_beats_the_users_own_accounts(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "corp-gw")
    await gateway(
        db_session,
        w,
        "alice-home",
        owner=w.alice,
        kind="api_key",
        payer="employee",
        route_patterns=["corp-*", "home-*"],
        default_cost_type=None,
    )
    by_instance = await one(client, w, "alice", record("k-route-1", route="CORP-1"))
    assert billing(by_instance) == ("employer", "corp-gw")
    assert (await stored(db_session, by_instance["entry_id"])).billing_method == "instance_route"

    by_personal = await one(client, w, "alice", record("k-route-2", route="home-1"))
    assert billing(by_personal) == ("employee", "alice-home")
    entry = await stored(db_session, by_personal["entry_id"])
    # The personal key has no default cost type, and the record gave none.
    assert (entry.billing_method, entry.cost_type, entry.route) == ("personal_route", "unknown", "home-1")


async def test_an_instance_account_covers_only_its_tools(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "codex-gw", tool_slugs=["codex"])
    result = await one(client, w, "alice", record("k-tools", route="corp-1"))
    # Not codex: the gateway is skipped, and alice's subscription covers claude-code.
    assert billing(result) == ("employee", f"claude-max-5x-{w.subscription.id}")


async def test_the_subscription_covering_the_tool_comes_after_the_routes(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    result = await one(client, w, "alice", record("k-sub"))
    assert billing(result) == ("employee", f"claude-max-5x-{w.subscription.id}")
    entry = await stored(db_session, result["entry_id"])
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == (
        "subscription",
        "subscription",
        w.subscription.id,
    )
    assert entry.billing_account_id == w.subscription.billing_account_id
    assert entry.marginal is not None and entry.marginal.amount == 0

    # A record that says it paid by API is not covered by the plan.
    api = await one(client, w, "alice", record("k-sub-api", cost_type="api"))
    assert billing(api) == ("unknown", None)
    entry = await stored(db_session, api["entry_id"])
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == ("none", "api", None)


async def test_naming_ones_subscription_account_charges_that_subscription(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    slug = f"claude-max-5x-{w.subscription.id}"
    result = await one(client, w, "alice", record("k-sub-slug", tool="codex", billing_account=slug))
    assert billing(result) == ("employee", slug)
    entry = await stored(db_session, result["entry_id"])
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == (
        "record",
        "subscription",
        w.subscription.id,
    )


async def test_gateway_usage_is_no_longer_charged_to_the_personal_plan(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    """The old silent charge: no cost type, a personal plan covering the tool, but a corporate gateway."""
    await gateway(db_session, w, "corp-gw")
    result = await one(client, w, "alice", record("k-silent", route="corp-eu"))
    assert billing(result) == ("employer", "corp-gw")
    entry = await stored(db_session, result["entry_id"])
    assert (entry.cost_type, entry.subscription_id) == ("api", None)
    assert entry.marginal is not None and entry.marginal.amount == Decimal("2")


async def test_the_records_cost_type_beats_the_accounts_default(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "corp-gw")
    result = await one(client, w, "alice", record("k-free", route="corp-1", cost_type="free"))
    entry = await stored(db_session, result["entry_id"])
    assert (entry.payer, entry.cost_type, entry.requested_cost_type) == ("employer", "free", "free")


async def test_the_project_default_decides_when_no_account_does(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    rec = {"cost_type": "api"}  # so the subscription does not apply
    in_s = await one(client, w, "alice", record("k-def-s", attribution={"project_key": "BILS"}, **rec))
    in_p = await one(client, w, "alice", record("k-def-p", attribution={"project_key": "BILR"}, **rec))
    nowhere = await one(client, w, "alice", record("k-def-none", **rec))
    assert billing(in_s) == ("employer", None)
    assert billing(in_p) == ("unknown", None)
    assert billing(nowhere) == ("unknown", None)
    assert (await stored(db_session, in_s["entry_id"])).billing_method == "project_default"
    assert (await stored(db_session, in_p["entry_id"])).billing_method == "none"

    # An account found earlier wins over the project default.
    await gateway(db_session, w, "home-gw", payer="employee", route_patterns=["home-*"])
    routed = await one(client, w, "alice", record("k-def-route", route="home-1", attribution={"project_key": "BILS"}))
    assert billing(routed) == ("employee", "home-gw")


async def test_an_account_a_person_set_is_kept_through_replays(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    gw = await gateway(db_session, w, "corp-gw")
    other = await gateway(db_session, w, "other-gw", payer="employee", route_patterns=[])
    first = await one(client, w, "alice", record("k-manual", route="corp-1"))
    entry = await stored(db_session, first["entry_id"])
    entry.billing_method, entry.billing_account_id, entry.payer = "manual", other.id, "employee"
    await db_session.commit()

    replay = await one(
        client, w, "alice", record("k-manual", route="corp-2", billing_account="corp-gw", tokens={"input": 2_000_000})
    )
    assert replay["status"] == "updated"
    assert billing(replay) == ("employee", "other-gw")
    await _usage.rebill_for_account(db_session, gw, actor_id=w.admin.id)
    entry = await stored(db_session, first["entry_id"])
    assert (entry.billing_method, entry.billing_account_id, entry.payer) == ("manual", other.id, "employee")


# ---------------------------------------------------------------------------
# Replays
# ---------------------------------------------------------------------------


async def test_a_replay_that_only_changes_the_route_is_an_update(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "corp-gw")
    base = record("k-replay", cost_type="api")
    first = await one(client, w, "alice", base)
    assert (first["status"], billing(first)) == ("created", ("unknown", None))

    routed = await one(client, w, "alice", {**base, "route": "corp-1"})
    assert (routed["status"], routed["revision"], billing(routed)) == ("updated", 2, ("employer", "corp-gw"))
    assert (await one(client, w, "alice", {**base, "route": "corp-1"}))["status"] == "unchanged"

    # Another route through the same account: the route is stored, the billing stays.
    moved = await one(client, w, "alice", {**base, "route": "corp-2"})
    assert (moved["status"], billing(moved)) == ("updated", ("employer", "corp-gw"))
    assert (await stored(db_session, moved["entry_id"])).route == "corp-2"

    # Dropping the route resolves again from the other rules.
    dropped = await one(client, w, "alice", base)
    assert (dropped["status"], billing(dropped)) == ("updated", ("unknown", None))


async def test_a_replay_that_only_changes_the_slug_is_an_update(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await gateway(db_session, w, "corp-gw", route_patterns=[])
    first = await one(client, w, "alice", record("k-replay-slug", cost_type="api"))
    named = await one(client, w, "alice", record("k-replay-slug", cost_type="api", billing_account="corp-gw"))
    assert (first["status"], named["status"], billing(named)) == ("created", "updated", ("employer", "corp-gw"))


# ---------------------------------------------------------------------------
# Changing an account resolves and re-prices the entries again
# ---------------------------------------------------------------------------


async def test_a_new_gateway_takes_the_routed_entries_and_reprices_them(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    routed = await one(client, w, "alice", record("k-new-gw", route="corp-1"))
    plain = await one(client, w, "alice", record("k-new-gw-plain"))
    before = await stored(db_session, routed["entry_id"])
    # Before the gateway exists, alice's plan covers the usage.
    assert (before.cost_type, before.marginal.amount if before.marginal else None) == ("subscription", Decimal(0))

    await gateway(db_session, w, "corp-gw")
    entry = await stored(db_session, routed["entry_id"])
    assert (entry.billing_method, entry.payer, entry.cost_type, entry.subscription_id) == (
        "instance_route",
        "employer",
        "api",
        None,
    )
    assert entry.marginal is not None and entry.marginal.amount == Decimal("2")
    assert entry.revision == 2
    untouched = await stored(db_session, plain["entry_id"])
    assert (untouched.revision, untouched.billing_method) == (1, "subscription")

    events = (
        (
            await db_session.execute(
                select(SecurityAuditLog).where(
                    SecurityAuditLog.event_type == AuditEvent.AI_USAGE_COSTS_RECOMPUTED.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(e.details.get("reason") == "billing" and e.details.get("changed") == 1 for e in events)


async def test_changing_an_accounts_payer_default_and_patterns_resolves_again(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    gw = await gateway(db_session, w, "corp-gw")
    routed = await one(client, w, "alice", record("k-change", route="corp-1"))
    explicit = await one(client, w, "alice", record("k-change-api", route="corp-1", cost_type="api"))

    await _accounts.update(db_session, gw, {"payer": "employee"}, actor=w.admin)
    entry = await stored(db_session, routed["entry_id"])
    assert (entry.payer, entry.revision) == ("employee", 2)

    await _accounts.update(db_session, gw, {"default_cost_type": "free"}, actor=w.admin)
    entry = await stored(db_session, routed["entry_id"])
    assert entry.cost_type == "free"
    assert entry.marginal is not None and entry.marginal.amount == 0
    # The record's own cost type stays.
    assert (await stored(db_session, explicit["entry_id"])).cost_type == "api"

    await _accounts.update(db_session, gw, {"route_patterns": ["elsewhere-*"]}, actor=w.admin)
    entry = await stored(db_session, routed["entry_id"])
    # No account by route any more: alice's plan covers it again.
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == (
        "subscription",
        "subscription",
        w.subscription.id,
    )
    entry_api = await stored(db_session, explicit["entry_id"])
    assert (entry_api.billing_method, entry_api.payer, entry_api.cost_type) == ("none", "unknown", "api")

    # A name change resolves nothing.
    revision = entry.revision
    await _accounts.update(db_session, gw, {"name": "Renamed"}, actor=w.admin)
    assert (await stored(db_session, routed["entry_id"])).revision == revision


async def test_deactivating_a_named_account_falls_back_to_the_other_rules(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    named = await gateway(db_session, w, "named-gw", route_patterns=[])
    await gateway(db_session, w, "corp-gw")
    result = await one(client, w, "alice", record("k-deact", route="corp-1", billing_account="named-gw"))
    assert billing(result) == ("employer", "named-gw")

    await _accounts.deactivate(db_session, named, actor=w.admin)
    entry = await stored(db_session, result["entry_id"])
    assert entry.billing_method == "instance_route"
    corp = await _accounts.find_by_slug(db_session, "corp-gw", user_id=w.alice.id)
    assert corp is not None and entry.billing_account_id == corp.id


async def test_a_personal_accounts_change_touches_only_its_owners_entries(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    alice_entry = await one(client, w, "alice", record("k-own", route="home-1", cost_type="api"))
    bob_entry = await one(client, w, "bob", record("k-own", route="home-1", cost_type="api"))
    await gateway(db_session, w, "home-key", owner=w.alice, kind="api_key", payer="employee", route_patterns=["home-*"])
    assert (await stored(db_session, alice_entry["entry_id"])).payer == "employee"
    bob = await stored(db_session, bob_entry["entry_id"])
    assert (bob.payer, bob.revision) == ("unknown", 1)


async def test_changing_the_project_default_resolves_its_entries_again(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    result = await one(client, w, "alice", record("k-proj", cost_type="api", attribution={"project_key": "BILR"}))
    assert billing(result) == ("unknown", None)
    project = await db_session.get(Project, w.p.id)
    assert project is not None
    _accounts.set_project_payer_default(project, "employee")
    await db_session.flush()
    outcome = await _usage.rebill_for_project(db_session, project, actor_id=w.admin.id)
    assert (outcome.entries, outcome.changed) == (1, 1)
    entry = await stored(db_session, result["entry_id"])
    assert (entry.billing_method, entry.payer) == ("project_default", "employee")


async def test_moving_an_entry_takes_the_payer_default_of_its_new_project(db_session: AsyncSession, w: World) -> None:
    entry = await _usage.log(
        db_session,
        w.alice,
        AiUsageInput(
            tool="claude-code",
            provider="testlab",
            model=MODEL,
            usage_date=date(2026, 11, 5),
            tokens={"input": 1_000_000},
            cost_type="api",
            project_id=w.p.id,
        ),
    )
    assert (entry.payer, entry.billing_method, entry.requested_cost_type) == ("unknown", "none", "api")
    await _usage.update(db_session, w.alice, entry, {"project_id": w.s.id})
    assert (entry.payer, entry.billing_method, entry.cost_type) == ("employer", "project_default", "api")

    # Clearing the cost type lets the subscription cover it again.
    await _usage.update(db_session, w.alice, entry, {"cost_type": None})
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == (
        "subscription",
        "subscription",
        w.subscription.id,
    )
    assert entry.requested_cost_type is None
