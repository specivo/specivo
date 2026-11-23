"""A person's billing override on AI usage entries: REST PATCH, the web edit form and the service rules.

The world of the REST tests (``test_ai_usage_api.build_world``) with its own
prefix: private project ``AUOA``; ``mgr`` manages it (``manage_ai_usage``),
``alice`` and ``bob`` log their own usage there, ``carol`` reads everyone's
usage without managing it, ``admin`` is an administrator. The instance has a
corporate gateway (employer, ``api`` by default, routes ``corp-*``); alice and
bob each hold a personal API key account (employee). One model is priced at
2 USD per million input tokens and 10 per million output tokens.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService, BillingAccountInput
from specivo.services.ai_usage_service import BILLING_OVERRIDE, AiUsageService, BillingOverride
from specivo.services.security_audit_service import AuditEvent
from tests.integration.test_ai_usage_api import World, build_world
from tests.integration.test_ai_usage_web import _get, _post

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

INGEST = "/api/v1/ai-usage/ingest"
DAY = "2026-03-10"

_accounts = AiBillingAccountService()
_usage = AiUsageService()


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    world = await build_world(db_session, "auo")
    admin = world.users["admin"]
    await _accounts.create(
        db_session,
        BillingAccountInput(
            slug="auo-gw",
            name="Corp gateway",
            kind="gateway",
            payer="employer",
            route_patterns=["corp-*"],
            default_cost_type="api",
        ),
        owner=None,
        actor=admin,
    )
    for name in ("alice", "bob"):
        await _accounts.create(
            db_session,
            BillingAccountInput(
                slug=f"{name}-key",
                name=f"{name.title()} key",
                kind="api_key",
                payer="employee",
                default_cost_type="api",
            ),
            owner=world.users[name],
            actor=admin,
        )
    await db_session.commit()
    return world


async def _account(db: AsyncSession, slug: str, owner: User | None = None) -> AiBillingAccount:
    stmt = select(AiBillingAccount).where(AiBillingAccount.slug == slug)
    stmt = stmt.where(
        AiBillingAccount.owner_user_id == owner.id if owner is not None else AiBillingAccount.owner_user_id.is_(None)
    )
    account = (await db.execute(stmt)).scalar_one()
    return account


def _record(key: str, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": key,
        "tool": "claude-code",
        "provider": "testlab",
        "model": "lab-model-1",
        "usage_date": DAY,
        "tokens": {"input": 1_000_000},
        "attribution": {"project_key": "AUOA"},
    }
    values.update(kw)
    return values


async def _ingest(client: AsyncClient, w: World, who: str, rec: dict[str, Any]) -> dict[str, Any]:
    body = {"schema": "specivo.ai-usage.v1", "client": {"name": "override-tests"}, "records": [rec]}
    resp = await client.post(INGEST, json=body, headers=w.auth(who))
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["status"] != "rejected", result
    return dict(result)


async def _entry(db: AsyncSession, entry_id: int) -> AiUsageEntry:
    entry = await db.get(AiUsageEntry, entry_id)
    assert entry is not None
    await db.refresh(entry)
    return entry


async def _reported(client: AsyncClient, db: AsyncSession, w: World, key: str = "k-1", **kw: Any) -> AiUsageEntry:
    """Alice's reporter entry over the corporate gateway (route ``corp-1``): employer, ``api``, 2 USD."""
    result = await _ingest(client, w, "alice", _record(key, route="corp-1", **kw))
    return await _entry(db, result["entry_id"])


async def _patch(client: AsyncClient, w: World, who: str, entry_id: int, body: dict[str, Any]) -> Any:
    return await client.patch(f"/api/v1/ai-usage/{entry_id}/", json=body, headers=w.auth(who))


async def _events(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


def _billing(entry: AiUsageEntry) -> tuple[str, int | None, str, str]:
    return entry.billing_method, entry.billing_account_id, entry.payer, entry.cost_type


# ---------------------------------------------------------------------------
# Setting, keeping and clearing an override (REST)
# ---------------------------------------------------------------------------


async def test_a_reported_entry_takes_an_override_of_its_users_account(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    gateway = await _account(db_session, "auo-gw")
    assert _billing(entry) == ("instance_route", gateway.id, "employer", "api")
    own = await _account(db_session, "alice-key", w.users["alice"])

    resp = await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["payer"], body["billing_method"], body["locked"]) == ("employee", "manual", True)
    assert body["billing_account"] == {
        "id": own.id,
        "slug": "alice-key",
        "name": "Alice key",
        "kind": "api_key",
        "instance": False,
    }
    entry = await _entry(db_session, entry.id)
    assert _billing(entry) == ("manual", own.id, "employee", "api")
    assert entry.revision == 2


async def test_a_replay_keeps_the_override(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    own = await _account(db_session, "alice-key", w.users["alice"])
    assert (await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})).status_code == 200

    result = await _ingest(
        client,
        w,
        "alice",
        _record("k-1", route="corp-2", billing_account="auo-gw", tokens={"input": 2_000_000}),
    )
    assert result["status"] == "updated"
    assert (result["payer"], result["billing_account"]) == ("employee", "alice-key")
    entry = await _entry(db_session, entry.id)
    assert _billing(entry) == ("manual", own.id, "employee", "api")
    assert entry.input == 2_000_000


async def test_an_account_change_does_not_undo_the_override(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    gateway = await _account(db_session, "auo-gw")
    own = await _account(db_session, "alice-key", w.users["alice"])
    assert (await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})).status_code == 200
    # Reload the entry the request changed before the accounts change.
    entry = await _entry(db_session, entry.id)

    # The overriding account's payer and default change, and a new instance account claims the route.
    await _accounts.update(
        db_session, own, {"payer": "employer", "default_cost_type": "subscription"}, actor=w.users["admin"]
    )
    await _accounts.update(db_session, gateway, {"route_patterns": ["corp-1"]}, actor=w.users["admin"])
    await _accounts.create(
        db_session,
        BillingAccountInput(
            slug="auo-gw2", name="Second gateway", kind="gateway", payer="employer", route_patterns=["*"]
        ),
        owner=None,
        actor=w.users["admin"],
    )
    await db_session.commit()

    entry = await _entry(db_session, entry.id)
    # The account and payer stay as the person set them; only the cost type follows the account's default.
    assert _billing(entry) == ("manual", own.id, "employee", "subscription")


async def test_clearing_the_override_resolves_the_billing_again(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    gateway = await _account(db_session, "auo-gw")
    own = await _account(db_session, "alice-key", w.users["alice"])
    assert (await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})).status_code == 200

    resp = await _patch(client, w, "alice", entry.id, {"billing_account_id": None})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["billing_method"], resp.json()["payer"]) == ("instance_route", "employer")
    entry = await _entry(db_session, entry.id)
    assert _billing(entry) == ("instance_route", gateway.id, "employer", "api")

    # Cleared, a replay resolves it by the rules again (the record's slug wins).
    result = await _ingest(client, w, "alice", _record("k-1", route="corp-1", billing_account="alice-key"))
    assert (result["payer"], result["billing_account"]) == ("employee", "alice-key")
    assert (await _entry(db_session, entry.id)).billing_method == "record"


async def test_a_payer_without_an_account(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    resp = await _patch(client, w, "alice", entry.id, {"payer": "employee"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["payer"], resp.json()["billing_account"]) == ("employee", None)
    entry = await _entry(db_session, entry.id)
    # No account: the cost type is the one the record asked for, here none.
    assert _billing(entry) == ("manual", None, "employee", "unknown")
    assert entry.marginal is None

    result = await _ingest(client, w, "alice", _record("k-1", route="corp-1", tokens={"input": 3}))
    assert result["payer"] == "employee"
    assert (await _entry(db_session, entry.id)).billing_method == "manual"


async def test_the_payer_of_an_account_is_the_accounts(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    gateway = await _account(db_session, "auo-gw")
    resp = await _patch(client, w, "alice", entry.id, {"billing_account_id": gateway.id, "payer": "employee"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "payer"
    ok = await _patch(client, w, "alice", entry.id, {"billing_account_id": gateway.id, "payer": "employer"})
    assert ok.status_code == 200
    assert _billing(await _entry(db_session, entry.id)) == ("manual", gateway.id, "employer", "api")


async def test_an_unchanged_override_writes_nothing(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    own = await _account(db_session, "alice-key", w.users["alice"])
    await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})
    again = await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})
    assert again.json()["revision"] == 2
    # Clearing an entry that has no override changes nothing either.
    other = await _reported(client, db_session, w, key="k-2")
    cleared = await _patch(client, w, "alice", other.id, {"payer": None})
    assert (cleared.json()["revision"], cleared.json()["billing_method"]) == (1, "instance_route")
    assert len(await _events(db_session, AuditEvent.AI_USAGE_BILLING_OVERRIDDEN)) == 1


# ---------------------------------------------------------------------------
# Who may override, and with which account
# ---------------------------------------------------------------------------


async def test_who_may_not_override(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    gateway = await _account(db_session, "auo-gw")
    body = {"payer": "employee"}
    # bob reads only his own entries; carol reads every entry but does not manage them.
    assert (await _patch(client, w, "bob", entry.id, body)).status_code == 404
    refused = await _patch(client, w, "carol", entry.id, body)
    assert refused.status_code == 403
    assert _billing(await _entry(db_session, entry.id)) == ("instance_route", gateway.id, "employer", "api")
    assert await _events(db_session, AuditEvent.AI_USAGE_BILLING_OVERRIDDEN) == []


async def test_a_manager_overrides_with_company_accounts_only(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    own = await _account(db_session, "alice-key", w.users["alice"])
    # alice's own account is private to her and the administrators.
    refused = await _patch(client, w, "mgr", entry.id, {"billing_account_id": own.id})
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "ai_usage_billing_account_unknown"

    ok = await _patch(client, w, "mgr", entry.id, {"payer": "employee"})
    assert ok.status_code == 200
    assert (ok.json()["payer"], ok.json()["billing_method"]) == ("employee", "manual")
    # The administrator may name it.
    admin = await _patch(client, w, "admin", entry.id, {"billing_account_id": own.id})
    assert admin.status_code == 200
    # The manager reads the entry without the private account's name.
    read = await client.get(f"/api/v1/ai-usage/{entry.id}/", headers=w.auth("mgr"))
    assert (read.json()["payer"], read.json()["billing_account"]) == ("employee", None)


async def test_an_account_the_entrys_user_may_not_use_is_refused(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    bobs = await _account(db_session, "bob-key", w.users["bob"])
    gateway = await _account(db_session, "auo-gw")
    # Even the administrator, who sees bob's account, cannot charge alice's usage to it.
    for who, account_id in (("admin", bobs.id), ("alice", bobs.id), ("alice", 999_999)):
        resp = await _patch(client, w, who, entry.id, {"billing_account_id": account_id})
        assert resp.status_code == 422, (who, account_id)
        assert resp.json()["errors"][0] == {
            "code": "ai_usage_billing_account_unknown",
            "message": "Choose an active billing account of the entry's user or of the instance",
            "field": "billing_account",
            "details": None,
        }
    # An inactive account is refused too, unless it is already the entry's.
    second = await _accounts.create(
        db_session,
        BillingAccountInput(slug="auo-off", name="Old gateway", kind="gateway", payer="employer"),
        owner=None,
        actor=w.users["admin"],
    )
    await _accounts.update(db_session, second, {"is_active": False}, actor=w.users["admin"])
    await db_session.commit()
    assert (await _patch(client, w, "alice", entry.id, {"billing_account_id": second.id})).status_code == 422
    assert (await _patch(client, w, "alice", entry.id, {"billing_account_id": gateway.id})).status_code == 200
    entry = await _entry(db_session, entry.id)
    await _accounts.update(db_session, gateway, {"is_active": False}, actor=w.users["admin"])
    await db_session.commit()
    kept = await _patch(client, w, "alice", entry.id, {"billing_account_id": gateway.id, "comments": "Still"})
    assert kept.status_code == 200, kept.text
    assert _billing(await _entry(db_session, entry.id))[:2] == ("manual", gateway.id)


async def test_an_unknown_payer_is_refused(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    assert (await _patch(client, w, "alice", entry.id, {"payer": "nobody"})).status_code == 422
    with pytest.raises(AppError) as exc:
        await _usage.update(db_session, w.users["alice"], entry, {BILLING_OVERRIDE: BillingOverride(payer="nobody")})
    assert (exc.value.code, exc.value.field) == ("ai_usage_invalid", "payer")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_every_override_is_audited(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    gateway = await _account(db_session, "auo-gw")
    own = await _account(db_session, "alice-key", w.users["alice"])

    await _patch(client, w, "alice", entry.id, {"billing_account_id": own.id})
    await _patch(client, w, "mgr", entry.id, {"billing_account_id": None, "comments": "Checked"})

    events = await _events(db_session, AuditEvent.AI_USAGE_BILLING_OVERRIDDEN)
    assert [(e.user_id, e.details["override"]) for e in events] == [
        (w.users["alice"].id, "set"),
        (w.users["mgr"].id, "cleared"),
    ]
    first = events[0]
    assert (first.resource_type, first.resource_id, first.project_id) == ("ai_usage_entry", entry.id, w.a.id)
    assert first.details["entry_id"] == entry.id
    assert first.details["target_login"] == "auo_alice"
    assert first.details["changes"] == {
        "billing_method": {"old": "instance_route", "new": "manual"},
        "billing_account_id": {"old": gateway.id, "new": own.id},
        "payer": {"old": "employer", "new": "employee"},
    }
    assert first.details["billing_account"] == {"old": "auo-gw", "new": "alice-key"}
    assert events[1].details["billing_account"] == {"old": "alice-key", "new": "auo-gw"}

    # The manager's other change goes to the generic event, without the billing fields.
    others = await _events(db_session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER)
    assert [set(e.details["changes"]) for e in others] == [{"comments"}]


async def test_a_managers_override_alone_writes_one_event(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    await _patch(client, w, "mgr", entry.id, {"payer": "employee"})
    assert len(await _events(db_session, AuditEvent.AI_USAGE_BILLING_OVERRIDDEN)) == 1
    assert await _events(db_session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER) == []


# ---------------------------------------------------------------------------
# The web edit form
# ---------------------------------------------------------------------------


async def test_the_edit_form_sets_and_clears_the_override(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    alice = w.users["alice"]
    own = await _account(db_session, "alice-key", alice)
    url = f"/ai-usage/{entry.id}/edit/"

    page = await _get(client, alice, url)
    assert page.status_code == 200
    assert 'data-testid="aiu-form-billing"' in page.text
    assert "Corp gateway (auo-gw)" in page.text and "Alice key (alice-key)" in page.text
    assert "Bob key" not in page.text
    assert 'data-testid="aiu-entry-billing"' in page.text

    base = {"project": "AUOA", "issue": "", "comments": ""}
    resp = await _post(client, alice, url, {**base, "billing": f"account:{own.id}"}, page=url)
    assert resp.status_code == 303, resp.text
    assert _billing(await _entry(db_session, entry.id)) == ("manual", own.id, "employee", "api")
    shown = await _get(client, alice, url)
    assert f'value="account:{own.id}" selected' in shown.text
    assert "Set by hand" in shown.text

    # A post without the billing field leaves the override alone.
    await _post(client, alice, url, {**base, "comments": "note"}, page=url)
    assert (await _entry(db_session, entry.id)).billing_method == "manual"

    resp = await _post(client, alice, url, {**base, "billing": ""}, page=url)
    assert resp.status_code == 303
    assert (await _entry(db_session, entry.id)).billing_method == "instance_route"

    resp = await _post(client, alice, url, {**base, "billing": "payer:employer"}, page=url)
    assert resp.status_code == 303
    assert _billing(await _entry(db_session, entry.id))[:3] == ("manual", None, "employer")


async def test_the_edit_form_refuses_an_account_the_user_may_not_use(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    entry = await _reported(client, db_session, w)
    bobs = await _account(db_session, "bob-key", w.users["bob"])
    url = f"/ai-usage/{entry.id}/edit/"
    base = {"project": "AUOA", "issue": "", "comments": ""}
    for value in (f"account:{bobs.id}", "account:x", "payer:nobody"):
        resp = await _post(client, w.users["alice"], url, {**base, "billing": value}, page=url)
        assert resp.status_code == 422, value
        assert "Choose an active billing account" in resp.text
    assert (await _entry(db_session, entry.id)).billing_method == "instance_route"


async def test_a_manager_sees_company_accounts_only(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await _reported(client, db_session, w)
    page = await _get(client, w.users["mgr"], f"/ai-usage/{entry.id}/edit/")
    assert page.status_code == 200
    assert "Corp gateway (auo-gw)" in page.text
    assert "Alice key" not in page.text
