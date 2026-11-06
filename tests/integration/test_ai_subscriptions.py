"""AI tools and personal AI subscriptions: the register, who may change it, the API, the pages and the resolver.

Users here hold their own subscriptions through ``/my/ai-subscriptions/``;
administrators manage anyone's through ``/admin/ai-subscriptions/``. Nobody
else sees a subscription. Changes an administrator makes to somebody else's
subscription are audited.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.money import Money
from specivo.models.ai_subscription import AiSubscription, AiTool
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_subscription_service import (
    SELF_SERVICE_SETTING_KEY,
    AiSubscriptionService,
    AiToolService,
    SubscriptionInput,
)
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.security_audit_service import AuditEvent
from specivo.services.settings_service import SettingsService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = AiSubscriptionService()
_tools = AiToolService()
BANGKOK = ZoneInfo("Asia/Bangkok")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user: User = AdminUserFactory.build(login="sub_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def alice(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="sub_alice", status="active", display_name="Alice")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def bob(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="sub_bob", status="active", display_name="Bob")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def agent(db_session: AsyncSession) -> User:
    user: User = ServiceAccountFactory.build(login="sub_agent", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


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


async def _add(
    db: AsyncSession,
    owner: User,
    *,
    plan_key: str = "claude-pro",
    actor: User | None = None,
    **values: object,
) -> AiSubscription:
    data = SubscriptionInput(plan_key=plan_key, starts_on=values.pop("starts_on", date(2026, 1, 1)), **values)  # type: ignore[arg-type]
    sub = await _service.create(db, data, owner=owner, actor=actor or owner)
    await db.commit()
    return sub


async def _audit_rows(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event).order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


async def _self_service(db: AsyncSession, enabled: bool) -> None:
    await SettingsService().set_many(db, {SELF_SERVICE_SETTING_KEY: "true" if enabled else "false"})
    await db.commit()


# ---------------------------------------------------------------------------
# Tables, seed and constraints
# ---------------------------------------------------------------------------


async def test_tools_are_seeded_in_order(db_session: AsyncSession) -> None:
    tools = await _tools.list_tools(db_session)
    seeded = ["claude-code", "codex", "gemini-cli", "cursor", "copilot", "opencode", "other"]
    assert [t.slug for t in tools if t.slug in seeded] == seeded
    by_slug = {t.slug: t for t in tools}
    assert by_slug["claude-code"].default_provider == "anthropic"
    assert by_slug["codex"].default_provider == "openai"
    assert by_slug["gemini-cli"].default_provider == "gemini"
    assert by_slug["cursor"].default_provider is None
    assert all(t.is_active for t in tools)


async def test_unknown_tool_is_created_inactive_on_first_use(db_session: AsyncSession) -> None:
    tool = await _tools.ensure(db_session, "Aider-Chat")
    assert (tool.slug, tool.name, tool.is_active) == ("aider-chat", "aider-chat", False)
    again = await _tools.ensure(db_session, "aider-chat")
    assert again is tool
    assert "aider-chat" not in {t.slug for t in await _tools.list_tools(db_session)}
    assert "aider-chat" in {t.slug for t in await _tools.list_tools(db_session, active_only=False)}
    with pytest.raises(AppError) as exc:
        await _tools.ensure(db_session, "not a slug")
    assert exc.value.code == "ai_tool_invalid"


async def test_database_checks(db_session: AsyncSession, alice: User) -> None:
    base = {
        "user_id": alice.id,
        "provider": "anthropic",
        "plan_key": "custom",
        "plan_name": "Plan",
        "amount": Decimal("10"),
        "currency_code": "USD",
        "billing_period": "month",
        "starts_on": date(2026, 3, 1),
    }
    for bad in (
        {"amount": Decimal("0")},
        {"billing_period": "week"},
        {"ends_on": date(2026, 2, 28)},
        {"provider": "Bad Provider"},
        {"plan_name": "  "},
    ):
        savepoint = await db_session.begin_nested()
        db_session.add(AiSubscription(**{**base, **bad}))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await savepoint.rollback()
    savepoint = await db_session.begin_nested()
    db_session.add(AiTool(slug="X_bad", name="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await savepoint.rollback()


# ---------------------------------------------------------------------------
# Service: validation and presets
# ---------------------------------------------------------------------------


async def test_preset_fills_missing_values(db_session: AsyncSession, alice: User) -> None:
    sub = await _add(db_session, alice, plan_key="cursor-pro")
    assert (sub.provider, sub.plan_name, sub.tool_slugs) == ("cursor", "Cursor Pro", ["cursor"])
    assert sub.fee == Money("20", "USD")
    assert sub.billing_period == "month"
    assert sub.created_by_id == alice.id


async def test_typed_values_win_over_the_preset(db_session: AsyncSession, alice: User) -> None:
    sub = await _add(
        db_session,
        alice,
        plan_key="claude-max-5x",
        amount="3500",
        currency="THB",
        billing_period="month",
        plan_name="Claude Max 5x (Thailand)",
    )
    assert sub.fee == Money("3500", "THB")
    assert sub.plan_name == "Claude Max 5x (Thailand)"
    assert sub.provider == "anthropic"


@pytest.mark.parametrize(
    ("values", "code", "field"),
    [
        ({"amount": "0"}, "ai_subscription_invalid", "amount"),
        ({"amount": "-5"}, "ai_subscription_invalid", "amount"),
        ({"amount": "10.005"}, "ai_subscription_invalid", "amount"),
        ({"amount": "abc"}, "ai_subscription_invalid", "amount"),
        ({"amount": 1.5}, "ai_subscription_invalid", "amount"),
        ({"amount": "1000000000000"}, "ai_subscription_invalid", "amount"),
        ({"currency": "XAU"}, "currency_inactive", "currency"),
        ({"currency": "QQQ"}, "currency_unknown", "currency"),
        ({"ends_on": date(2025, 12, 31)}, "ai_subscription_invalid", "ends_on"),
        ({"tool_slugs": ["no-such-tool"]}, "ai_subscription_unknown_tool", "tool_slugs"),
        ({"tool_slugs": ["Bad Slug!"]}, "ai_subscription_invalid", "tool_slugs"),
        ({"billing_period": "week"}, "ai_subscription_invalid", "billing_period"),
        ({"plan_key": "claude-ultra"}, "ai_subscription_invalid", "plan_key"),
        ({"notes": "x" * 2001}, "ai_subscription_invalid", "notes"),
    ],
)
async def test_validation(db_session: AsyncSession, alice: User, values: dict, code: str, field: str) -> None:
    plan_key = values.pop("plan_key", "claude-pro")
    with pytest.raises(AppError) as exc:
        await _add(db_session, alice, plan_key=plan_key, **values)
    assert (exc.value.code, exc.value.field) == (code, field)


async def test_custom_plan_needs_provider_and_fee(db_session: AsyncSession, alice: User) -> None:
    with pytest.raises(AppError) as exc:
        await _add(db_session, alice, plan_key="custom", plan_name="Team trial", amount="10", currency="USD")
    assert exc.value.field == "provider"
    with pytest.raises(AppError) as exc:
        await _add(db_session, alice, plan_key="custom", plan_name="Team trial", provider="openai", currency="USD")
    assert exc.value.field == "amount"
    sub = await _add(
        db_session, alice, plan_key="custom", plan_name="Trial", provider="Google", amount="1500", currency="JPY"
    )
    assert sub.provider == "gemini"
    with pytest.raises(AppError) as exc:
        await _add(db_session, alice, plan_key="custom", plan_name="T", provider="openai", amount="1.5", currency="JPY")
    assert exc.value.field == "amount"


async def test_currency_check_on_update_only_when_it_changes(db_session: AsyncSession, alice: User) -> None:
    sub = await _add(db_session, alice, amount="20", currency="EUR")
    from specivo.models.currency import Currency

    eur = await db_session.get(Currency, "EUR")
    assert eur is not None
    eur.is_active = False
    await db_session.commit()
    # A stored disabled currency stays valid for other edits.
    await _service.update(db_session, sub, {"plan_name": "Claude Pro (EU)"}, actor=alice)
    await _service.update(db_session, sub, {"amount": "21.50"}, actor=alice)
    assert sub.fee == Money("21.50", "EUR")
    with pytest.raises(AppError) as exc:
        await _service.update(db_session, sub, {"currency": "XAU"}, actor=alice)
    assert exc.value.code == "currency_inactive"
    # Changing the currency rechecks the stored amount's precision.
    await _service.update(db_session, sub, {"amount": "21.5"}, actor=alice)
    with pytest.raises(AppError) as exc:
        await _service.update(db_session, sub, {"amount": "21.55", "currency": "JPY"}, actor=alice)
    assert exc.value.field == "amount"


async def test_update_period_is_checked_against_the_stored_start(db_session: AsyncSession, alice: User) -> None:
    sub = await _add(db_session, alice, starts_on=date(2026, 3, 1))
    with pytest.raises(AppError) as exc:
        await _service.update(db_session, sub, {"ends_on": date(2026, 2, 1)}, actor=alice)
    assert exc.value.field == "ends_on"
    await _service.update(db_session, sub, {"ends_on": date(2026, 3, 1)}, actor=alice)
    assert sub.ends_on == date(2026, 3, 1)
    await _service.update(db_session, sub, {"ends_on": None}, actor=alice)
    assert sub.ends_on is None


async def test_service_accounts_hold_no_subscriptions(db_session: AsyncSession, admin: User, agent: User) -> None:
    with pytest.raises(AppError) as exc:
        await _add(db_session, agent, actor=admin)
    assert exc.value.code == "ai_subscription_owner_invalid"
    with pytest.raises(AppError) as exc:
        await _add(db_session, agent)
    assert exc.value.code == "ai_subscription_owner_invalid"


# ---------------------------------------------------------------------------
# Service: permissions and audit
# ---------------------------------------------------------------------------


async def test_user_cannot_change_someone_elses(db_session: AsyncSession, alice: User, bob: User) -> None:
    sub = await _add(db_session, alice)
    with pytest.raises(AppError) as exc:
        await _service.update(db_session, sub, {"plan_name": "Mine now"}, actor=bob)
    assert exc.value.status_code == 404
    with pytest.raises(AppError):
        await _service.delete(db_session, sub, actor=bob)
    with pytest.raises(AppError):
        await _service.create(db_session, SubscriptionInput(starts_on=date(2026, 1, 1)), owner=alice, actor=bob)


async def test_self_service_off_refuses_the_user_but_not_admins(
    db_session: AsyncSession, admin: User, alice: User
) -> None:
    sub = await _add(db_session, alice)
    await _self_service(db_session, False)
    assert not await _service.can_edit_own(db_session, alice)
    with pytest.raises(PermissionDeniedError):
        await _add(db_session, alice)
    with pytest.raises(PermissionDeniedError):
        await _service.update(db_session, sub, {"plan_name": "x"}, actor=alice)
    with pytest.raises(PermissionDeniedError):
        await _service.delete(db_session, sub, actor=alice)
    await _service.update(db_session, sub, {"plan_name": "Set by admin"}, actor=admin)
    assert sub.plan_name == "Set by admin"
    # Admins keep managing their own.
    assert await _service.can_edit_own(db_session, admin)


async def test_own_changes_are_not_audited(db_session: AsyncSession, alice: User) -> None:
    sub = await _add(db_session, alice)
    await _service.update(db_session, sub, {"amount": "25"}, actor=alice)
    await _service.delete(db_session, sub, actor=alice)
    for event in (AuditEvent.AI_SUBSCRIPTION_CREATED, AuditEvent.AI_SUBSCRIPTION_UPDATED):
        assert await _audit_rows(db_session, event) == []
    assert await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_DELETED) == []


async def test_admin_changes_to_another_users_subscription_are_audited(
    db_session: AsyncSession, admin: User, alice: User
) -> None:
    sub = await _add(db_session, alice, actor=admin, notes="Paid by card")
    assert sub.created_by_id == admin.id
    created = await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_CREATED)
    assert len(created) == 1
    row = created[0]
    assert (row.user_id, row.resource_type, row.resource_id) == (admin.id, "ai_subscription", sub.id)
    assert row.details["target_login"] == "sub_alice"
    assert row.details["amount"] == "20.00" and row.details["currency"] == "USD"
    assert "notes" not in row.details

    await _service.update(
        db_session, sub, {"amount": "100", "notes": "Upgraded", "plan_key": "claude-max-5x"}, actor=admin
    )
    updated = await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_UPDATED)
    assert len(updated) == 1
    changes = updated[0].details["changes"]
    assert changes["amount"] == {"old": "20.00", "new": "100.00"}
    assert changes["plan_key"] == {"old": "claude-pro", "new": "claude-max-5x"}
    assert changes["notes"] == {"changed": True}
    assert "Upgraded" not in str(updated[0].details)

    # A no-op edit writes nothing.
    await _service.update(db_session, sub, {"amount": "100.00"}, actor=admin)
    assert len(await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_UPDATED)) == 1

    await _service.delete(db_session, sub, actor=admin)
    deleted = await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_DELETED)
    assert len(deleted) == 1 and deleted[0].details["subscription_id"] == sub.id


async def test_self_service_switch_is_audited_when_it_changes(db_session: AsyncSession, admin: User) -> None:
    assert await _service.self_service_enabled(db_session)
    await _service.set_self_service(db_session, True, actor=admin)
    assert await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED) == []
    await _service.set_self_service(db_session, False, actor=admin)
    assert not await _service.self_service_enabled(db_session)
    rows = await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED)
    assert len(rows) == 1 and rows[0].details == {"enabled": False} and rows[0].resource_type == "setting"


# ---------------------------------------------------------------------------
# Resolver and seat cost against the database
# ---------------------------------------------------------------------------


async def test_subscription_for_tool_then_provider(db_session: AsyncSession, alice: User, bob: User) -> None:
    claude = await _add(db_session, alice, plan_key="claude-max-20x", starts_on=date(2026, 1, 1))
    cursor = await _add(db_session, alice, plan_key="cursor-pro", starts_on=date(2026, 1, 1))
    await _add(db_session, bob, plan_key="chatgpt-plus", starts_on=date(2026, 1, 1))
    at = datetime(2026, 2, 10, 9, 0, tzinfo=UTC)

    found = await _service.subscription_for(db_session, alice.id, "claude-code", "anthropic", at)
    assert found is not None and found.subscription.id == claude.id and found.matched_by == "provider"
    found = await _service.subscription_for(db_session, alice.id, "cursor", "anthropic", at)
    assert found is not None and found.subscription.id == cursor.id and found.matched_by == "tool"
    # Bob's plan never covers Alice, and Alice has no OpenAI plan.
    assert await _service.subscription_for(db_session, alice.id, "codex", "openai", at) is None
    # The provider defaults to the tool's.
    found = await _service.subscription_for(db_session, alice.id, "claude-code", None, at)
    assert found is not None and found.subscription.id == claude.id
    # A tool created on first use has no default provider: not covered by a provider plan.
    await _tools.ensure(db_session, "new-agent")
    assert await _service.subscription_for(db_session, alice.id, "new-agent", "anthropic", at) is None


async def test_subscription_for_uses_the_utc_day(db_session: AsyncSession, alice: User) -> None:
    await _add(db_session, alice, starts_on=date(2026, 3, 1), ends_on=date(2026, 3, 31))
    # 1 April 05:00 in Bangkok is 31 March in UTC: covered. 1 April 07:00 is not.
    early = datetime(2026, 4, 1, 5, 0, tzinfo=BANGKOK)
    late = datetime(2026, 4, 1, 7, 0, tzinfo=BANGKOK)
    assert await _service.subscription_for(db_session, alice.id, "claude-code", "anthropic", early) is not None
    assert await _service.subscription_for(db_session, alice.id, "claude-code", "anthropic", late) is None
    assert await _service.subscription_for(db_session, alice.id, "claude-code", "anthropic", date(2026, 3, 31))


async def test_seat_cost_for_a_month(db_session: AsyncSession, alice: User, bob: User) -> None:
    await _add(db_session, alice, plan_key="claude-pro", starts_on=date(2026, 1, 1), ends_on=date(2026, 4, 15))
    await _add(db_session, alice, plan_key="chatgpt-plus", amount="2400", currency="THB", billing_period="year")
    await _add(db_session, bob, plan_key="chatgpt-pro")
    charges = await _service.seat_cost(db_session, alice.id, 2026, 4)
    assert sorted(c.amount for c in charges if c.amount.currency == "USD") == [Money("10", "USD")]
    assert [c.amount for c in charges if c.amount.currency == "THB"] == [Money("200", "THB")]
    assert all(c.user_id == alice.id for c in charges)
    assert await _service.seat_cost(db_session, alice.id, 2025, 12) == []


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_tools_and_presets_endpoints(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    await _tools.ensure(db_session, "hidden-tool")
    await db_session.commit()
    resp = await client.get("/api/v1/ai-tools/", headers=_bearer(alice))
    assert resp.status_code == 200
    slugs = [t["slug"] for t in resp.json()]
    assert "claude-code" in slugs and "hidden-tool" not in slugs
    resp = await client.get("/api/v1/ai-tools/?include_inactive=true", headers=_bearer(alice))
    assert "hidden-tool" in [t["slug"] for t in resp.json()]
    resp = await client.get("/api/v1/ai-subscription-presets/", headers=_bearer(alice))
    assert resp.status_code == 200
    by_key = {p["key"]: p for p in resp.json()}
    assert by_key["claude-max-5x"]["amount"] == "100.00"
    assert by_key["cursor-pro"]["tool_slugs"] == ["cursor"]
    assert (await client.get("/api/v1/ai-tools/")).status_code == 401


async def test_user_edits_own_through_the_api(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    resp = await client.post(
        "/api/v1/my/ai-subscriptions/",
        json={"plan_key": "claude-max-5x", "starts_on": "2026-02-01", "amount": "90.00", "currency": "EUR"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["amount"] == "90.00" and body["currency"] == "EUR"
    assert body["user_id"] == alice.id and body["plan_name"] == "Claude Max 5x"
    sub_id = body["id"]

    resp = await client.patch(
        f"/api/v1/my/ai-subscriptions/{sub_id}/", json={"ends_on": "2026-06-30"}, headers=_bearer(alice)
    )
    assert resp.status_code == 200 and resp.json()["ends_on"] == "2026-06-30"
    resp = await client.patch(f"/api/v1/my/ai-subscriptions/{sub_id}/", json={"ends_on": None}, headers=_bearer(alice))
    assert resp.json()["ends_on"] is None
    resp = await client.patch(f"/api/v1/my/ai-subscriptions/{sub_id}/", json={"amount": 5}, headers=_bearer(alice))
    assert resp.status_code == 422

    resp = await client.get("/api/v1/my/ai-subscriptions/", headers=_bearer(alice))
    assert resp.json()["total_count"] == 1
    resp = await client.get("/api/v1/my/ai-subscriptions/?active_on=2026-01-15", headers=_bearer(alice))
    assert resp.json()["total_count"] == 0

    assert (await client.delete(f"/api/v1/my/ai-subscriptions/{sub_id}/", headers=_bearer(alice))).status_code == 204
    assert (await client.get(f"/api/v1/my/ai-subscriptions/{sub_id}/", headers=_bearer(alice))).status_code == 404


async def test_user_cannot_see_or_edit_others_through_the_api(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, bob: User
) -> None:
    sub = await _add(db_session, alice)
    for method, kwargs in (("get", {}), ("patch", {"json": {"plan_name": "x"}}), ("delete", {})):
        resp = await getattr(client, method)(f"/api/v1/my/ai-subscriptions/{sub.id}/", headers=_bearer(bob), **kwargs)
        assert resp.status_code == 404, method
    resp = await client.get("/api/v1/my/ai-subscriptions/", headers=_bearer(bob))
    assert resp.json()["total_count"] == 0
    # Even an administrator reaches someone else's subscription only through the admin API.
    assert (await client.get(f"/api/v1/my/ai-subscriptions/{sub.id}/", headers=_bearer(admin))).status_code == 404
    for url in ("/api/v1/admin/ai-subscriptions/", f"/api/v1/admin/ai-subscriptions/{sub.id}/"):
        assert (await client.get(url, headers=_bearer(bob))).status_code == 403
    resp = await client.post(
        "/api/v1/admin/ai-subscriptions/",
        json={"user_id": bob.id, "plan_key": "claude-pro", "starts_on": "2026-01-01"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 403


async def test_admin_edits_anyone_through_the_api(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, bob: User, agent: User
) -> None:
    resp = await client.post(
        "/api/v1/admin/ai-subscriptions/",
        json={"user_id": alice.id, "plan_key": "copilot-pro", "starts_on": "2026-01-01"},
        headers=_bearer(admin),
    )
    assert resp.status_code == 201, resp.text
    sub_id = resp.json()["id"]
    assert resp.json()["user_login"] == "sub_alice"
    await _add(db_session, bob, plan_key="chatgpt-plus")

    resp = await client.get(f"/api/v1/admin/ai-subscriptions/?user_id={alice.id}", headers=_bearer(admin))
    assert [s["id"] for s in resp.json()["items"]] == [sub_id]
    resp = await client.get("/api/v1/admin/ai-subscriptions/?provider=openai", headers=_bearer(admin))
    assert [s["user_login"] for s in resp.json()["items"]] == ["sub_bob"]

    resp = await client.patch(
        f"/api/v1/admin/ai-subscriptions/{sub_id}/", json={"amount": "12.00"}, headers=_bearer(admin)
    )
    assert resp.status_code == 200 and resp.json()["amount"] == "12.00"
    assert (await client.delete(f"/api/v1/admin/ai-subscriptions/{sub_id}/", headers=_bearer(admin))).status_code == 204
    assert len(await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_CREATED)) == 1
    assert len(await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_UPDATED)) == 1
    assert len(await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_DELETED)) == 1

    resp = await client.post(
        "/api/v1/admin/ai-subscriptions/",
        json={"user_id": agent.id, "plan_key": "claude-pro", "starts_on": "2026-01-01"},
        headers=_bearer(admin),
    )
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "ai_subscription_owner_invalid"
    resp = await client.post(
        "/api/v1/admin/ai-subscriptions/",
        json={"user_id": 99999999, "plan_key": "claude-pro", "starts_on": "2026-01-01"},
        headers=_bearer(admin),
    )
    assert resp.status_code == 422


async def test_self_service_off_refuses_the_user_through_the_api(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User
) -> None:
    sub = await _add(db_session, alice)
    resp = await client.patch(
        "/api/v1/admin/ai-subscriptions/settings/", json={"self_service": False}, headers=_bearer(admin)
    )
    assert resp.status_code == 200 and resp.json() == {"self_service": False}
    resp = await client.post(
        "/api/v1/my/ai-subscriptions/",
        json={"plan_key": "claude-pro", "starts_on": "2026-01-01"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 403
    resp = await client.patch(f"/api/v1/my/ai-subscriptions/{sub.id}/", json={"plan_name": "x"}, headers=_bearer(alice))
    assert resp.status_code == 403
    assert (await client.delete(f"/api/v1/my/ai-subscriptions/{sub.id}/", headers=_bearer(alice))).status_code == 403
    # Reading stays allowed.
    assert (await client.get("/api/v1/my/ai-subscriptions/", headers=_bearer(alice))).json()["total_count"] == 1
    # The generic settings endpoint refuses the key.
    resp = await client.patch(
        "/api/v1/admin/settings/", json={SELF_SERVICE_SETTING_KEY: "true"}, headers=_bearer(admin)
    )
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "setting_managed_elsewhere"
    assert (await client.get("/api/v1/admin/ai-subscriptions/settings/", headers=_bearer(admin))).json() == {
        "self_service": False
    }


async def test_service_account_cannot_hold_one_through_the_api(
    client: AsyncClient, db_session: AsyncSession, agent: User
) -> None:
    _key, raw = await ApiKeyService().create_key(session=db_session, user_id=agent.id, name="sub-agent")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    resp = await client.post(
        "/api/v1/my/ai-subscriptions/", json={"plan_key": "claude-pro", "starts_on": "2026-01-01"}, headers=headers
    )
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "ai_subscription_owner_invalid"
    assert (await client.get("/api/v1/my/ai-subscriptions/", headers=headers)).json()["total_count"] == 0


async def test_project_limited_key_is_refused(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    project = ProjectFactory.build(key="SUBLIM", identifier="sub-lim", path="sub_lim")
    db_session.add(project)
    await db_session.commit()
    assert isinstance(project, Project)
    # An administrator's key limited to one project reaches neither their own nor the admin register.
    _key, raw = await ApiKeyService().create_key(
        session=db_session, user_id=admin.id, name="sub-limited", scopes={"projects": [project.key]}
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.get("/api/v1/my/ai-subscriptions/", headers=headers)).status_code == 403
    resp = await client.post(
        "/api/v1/my/ai-subscriptions/", json={"plan_key": "claude-pro", "starts_on": "2026-01-01"}, headers=headers
    )
    assert resp.status_code == 403
    assert (await client.get("/api/v1/admin/ai-subscriptions/", headers=headers)).status_code == 403


async def _gone(db: AsyncSession, sub_id: int) -> bool:
    found = await db.execute(select(AiSubscription.id).where(AiSubscription.id == sub_id))
    return found.first() is None


async def test_admin_tool_endpoints(client: AsyncClient, db_session: AsyncSession, admin: User, alice: User) -> None:
    resp = await client.post(
        "/api/v1/admin/ai-tools/",
        json={"slug": "aider", "name": "Aider", "default_provider": "Anthropic", "position": 70},
        headers=_bearer(admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["default_provider"] == "anthropic"
    resp = await client.post("/api/v1/admin/ai-tools/", json={"slug": "aider", "name": "Again"}, headers=_bearer(admin))
    assert resp.status_code == 409
    resp = await client.patch(
        "/api/v1/admin/ai-tools/aider/", json={"is_active": False, "default_provider": None}, headers=_bearer(admin)
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is False and resp.json()["default_provider"] is None
    resp = await client.patch("/api/v1/admin/ai-tools/aider/", json={"name": " "}, headers=_bearer(admin))
    assert resp.status_code == 422
    tool = await db_session.get(AiTool, "aider")
    assert tool is not None and tool.name == "Aider"
    assert (await client.patch("/api/v1/admin/ai-tools/nope/", json={}, headers=_bearer(admin))).status_code == 404
    assert (await client.get("/api/v1/admin/ai-tools/", headers=_bearer(alice))).status_code == 403


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


async def test_my_page_lists_own_and_adds(
    client: AsyncClient, db_session: AsyncSession, alice: User, bob: User
) -> None:
    await _add(db_session, bob, plan_key="chatgpt-pro")
    resp = await client.get("/my/ai-subscriptions/?preset=claude-max-20x", cookies=_cookies(alice))
    assert resp.status_code == 200
    assert 'value="200.00"' in resp.text
    assert "ChatGPT Pro" not in resp.text.split('data-testid="subscriptions-table"')[1].split("</table>")[0]

    resp = await _post_form(
        client,
        alice,
        "/my/ai-subscriptions/",
        {
            "plan_key": "claude-max-20x",
            "plan_name": "Claude Max 20x",
            "provider": "anthropic",
            "amount": "7000",
            "currency": "THB",
            "billing_period": "month",
            "starts_on": "2026-02-01",
            "ends_on": "",
            "notes": "",
            "tool_slugs": ["claude-code", "opencode"],
        },
        "/my/ai-subscriptions/",
    )
    assert resp.status_code == 303, resp.text
    rows, _ = await _service.list_subscriptions(db_session, user_id=alice.id)
    assert len(rows) == 1 and rows[0].fee == Money("7000", "THB") and rows[0].tool_slugs == ["claude-code", "opencode"]

    resp = await client.get("/my/ai-subscriptions/", cookies=_cookies(alice))
    assert "Claude Max 20x" in resp.text and "OpenCode" in resp.text


async def test_my_page_shows_errors_and_keeps_input(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    resp = await _post_form(
        client,
        alice,
        "/my/ai-subscriptions/",
        {
            "plan_key": "custom",
            "plan_name": "Trial",
            "provider": "openai",
            "amount": "10.555",
            "currency": "USD",
            "billing_period": "month",
            "starts_on": "2026-02-01",
        },
        "/my/ai-subscriptions/",
    )
    assert resp.status_code == 422
    assert 'data-testid="subscription-error"' in resp.text
    assert 'value="10.555"' in resp.text
    resp = await _post_form(
        client,
        alice,
        "/my/ai-subscriptions/",
        {"plan_key": "claude-pro", "starts_on": "01/02/2026"},
        "/my/ai-subscriptions/",
    )
    assert resp.status_code == 422


async def test_my_page_edit_and_delete(client: AsyncClient, db_session: AsyncSession, alice: User, bob: User) -> None:
    sub = await _add(db_session, alice)
    resp = await client.get(f"/my/ai-subscriptions/{sub.id}/", cookies=_cookies(alice))
    assert resp.status_code == 200 and 'value="20.00"' in resp.text
    assert (await client.get(f"/my/ai-subscriptions/{sub.id}/", cookies=_cookies(bob))).status_code == 404

    form = {
        "plan_key": "claude-pro",
        "plan_name": "Claude Pro",
        "provider": "anthropic",
        "amount": "18",
        "currency": "EUR",
        "billing_period": "month",
        "starts_on": "2026-01-01",
        "ends_on": "2026-05-31",
    }
    resp = await _post_form(client, bob, f"/my/ai-subscriptions/{sub.id}/", form, "/my/ai-subscriptions/")
    assert resp.status_code == 404
    resp = await _post_form(client, alice, f"/my/ai-subscriptions/{sub.id}/", form, "/my/ai-subscriptions/")
    assert resp.status_code == 303
    await db_session.refresh(sub)
    assert sub.fee == Money("18", "EUR") and sub.ends_on == date(2026, 5, 31)

    resp = await _post_form(client, bob, f"/my/ai-subscriptions/{sub.id}/delete/", {}, "/my/ai-subscriptions/")
    assert resp.status_code == 404
    resp = await _post_form(client, alice, f"/my/ai-subscriptions/{sub.id}/delete/", {}, "/my/ai-subscriptions/")
    assert resp.status_code == 303
    assert await _gone(db_session, sub.id)


async def test_my_page_with_self_service_off(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    sub = await _add(db_session, alice)
    await _self_service(db_session, False)
    resp = await client.get("/my/ai-subscriptions/", cookies=_cookies(alice))
    assert resp.status_code == 200
    assert 'data-testid="subscription-self-service-off"' in resp.text
    assert 'data-testid="subscription-form"' not in resp.text
    assert "Claude Pro" in resp.text
    resp = await _post_form(
        client,
        alice,
        "/my/ai-subscriptions/",
        {"plan_key": "claude-pro", "starts_on": "2026-01-01"},
        "/my/ai-subscriptions/",
    )
    assert resp.status_code == 422 and "administrator manages" in resp.text
    resp = await _post_form(client, alice, f"/my/ai-subscriptions/{sub.id}/delete/", {}, "/my/ai-subscriptions/")
    assert resp.status_code == 403


async def test_my_page_for_a_service_account(client: AsyncClient, db_session: AsyncSession, agent: User) -> None:
    resp = await client.get("/my/ai-subscriptions/", cookies=_cookies(agent))
    # Service accounts cannot sign in to the web; whatever the answer, no form is offered.
    assert 'data-testid="subscription-form"' not in resp.text


async def test_admin_pages(client: AsyncClient, db_session: AsyncSession, admin: User, alice: User) -> None:
    resp = await client.get("/admin/ai-subscriptions/", cookies=_cookies(admin))
    assert resp.status_code == 200 and 'data-testid="self-service-card"' in resp.text
    assert (await client.get("/admin/ai-subscriptions/", cookies=_cookies(alice))).status_code == 403

    resp = await _post_form(
        client,
        admin,
        "/admin/ai-subscriptions/",
        {
            "user_id": str(alice.id),
            "plan_key": "google-ai-pro",
            "plan_name": "",
            "provider": "",
            "amount": "",
            "currency": "",
            "billing_period": "month",
            "starts_on": "2026-03-01",
        },
        "/admin/ai-subscriptions/",
    )
    assert resp.status_code == 303, resp.text
    rows, _ = await _service.list_subscriptions(db_session, user_id=alice.id)
    assert len(rows) == 1 and rows[0].fee == Money("19.99", "USD") and rows[0].provider == "gemini"
    sub = rows[0]
    assert len(await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_CREATED)) == 1

    resp = await client.get(f"/admin/ai-subscriptions/?user_id={alice.id}", cookies=_cookies(admin))
    assert "Google AI Pro" in resp.text
    resp = await client.get(f"/admin/ai-subscriptions/{sub.id}/", cookies=_cookies(admin))
    assert resp.status_code == 200 and "sub_alice" in resp.text

    resp = await _post_form(
        client,
        admin,
        f"/admin/ai-subscriptions/{sub.id}/",
        {
            "plan_key": "google-ai-pro",
            "plan_name": "Google AI Pro",
            "provider": "gemini",
            "amount": "21.99",
            "currency": "USD",
            "billing_period": "month",
            "starts_on": "2026-03-01",
            "ends_on": "",
        },
        "/admin/ai-subscriptions/",
    )
    assert resp.status_code == 303
    assert len(await _audit_rows(db_session, AuditEvent.AI_SUBSCRIPTION_UPDATED)) == 1

    resp = await _post_form(client, admin, "/admin/ai-subscriptions/settings/", {}, "/admin/ai-subscriptions/")
    assert resp.status_code == 303
    assert not await _service.self_service_enabled(db_session)

    resp = await _post_form(client, admin, f"/admin/ai-subscriptions/{sub.id}/delete/", {}, "/admin/ai-subscriptions/")
    assert resp.status_code == 303
    assert await _gone(db_session, sub.id)


async def test_admin_tools_page(client: AsyncClient, db_session: AsyncSession, admin: User, alice: User) -> None:
    await _tools.ensure(db_session, "reported-tool")
    await db_session.commit()
    resp = await client.get("/admin/ai-tools/", cookies=_cookies(admin))
    assert resp.status_code == 200 and "reported-tool" in resp.text
    assert (await client.get("/admin/ai-tools/", cookies=_cookies(alice))).status_code == 403

    resp = await _post_form(
        client,
        admin,
        "/admin/ai-tools/reported-tool/",
        {"name": "Reported Tool", "default_provider": "openai", "position": "80", "is_active": "1"},
        "/admin/ai-tools/",
    )
    assert resp.status_code == 303
    tool = await db_session.get(AiTool, "reported-tool")
    assert tool is not None
    await db_session.refresh(tool)
    assert (tool.name, tool.default_provider, tool.position, tool.is_active) == ("Reported Tool", "openai", 80, True)

    resp = await _post_form(client, admin, "/admin/ai-tools/", {"slug": "Bad Slug", "name": "x"}, "/admin/ai-tools/")
    assert resp.status_code == 422 and 'data-testid="tool-error"' in resp.text
    resp = await _post_form(
        client, admin, "/admin/ai-tools/", {"slug": "web-tool", "name": "Web Tool"}, "/admin/ai-tools/"
    )
    assert resp.status_code == 303
    assert await db_session.get(AiTool, "web-tool") is not None
