"""AI billing accounts: REST routes, web pages, audit events and the project payer default setting.

The rules themselves (validation, who may manage which account) are
:class:`AiBillingAccountService`'s and are tested in ``test_ai_billing_accounts``;
re-resolution of usage entries in ``test_ai_usage_billing``. These tests check
that every route keeps the rules, that changes are audited where they should
be, and that changing a project's payer default re-resolves its entries.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import PROJECT_PAYER_DEFAULT_KEY
from specivo.core.config import get_settings
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService, BillingAccountInput
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.ai_subscription_service import SELF_SERVICE_SETTING_KEY
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import EVENT_CATEGORIES, AuditCategory, AuditEvent
from specivo.services.settings_service import SettingsService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = AiBillingAccountService()
MY = "/api/v1/my/ai-billing-accounts/"
ADMIN = "/api/v1/admin/ai-billing-accounts/"
MODEL = "lab-billui-1"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user: User = AdminUserFactory.build(login="bui_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def alice(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="bui_alice", status="active", display_name="Alice")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def bob(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="bui_bob", status="active", display_name="Bob")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def agent(db_session: AsyncSession) -> User:
    user: User = ServiceAccountFactory.build(login="bui_agent", status="active")
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


async def _self_service(db: AsyncSession, enabled: bool) -> None:
    await SettingsService().set_many(db, {SELF_SERVICE_SETTING_KEY: "true" if enabled else "false"})
    await db.commit()


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


async def _account(
    db: AsyncSession, actor: User, owner: User | None = None, slug: str = "corp-gw", **values: Any
) -> AiBillingAccount:
    fields: dict[str, Any] = {"name": f"Account {slug}", "kind": "gateway", "payer": "employer"}
    fields.update(values)
    account = await _service.create(db, BillingAccountInput(slug=slug, **fields), owner=owner, actor=actor)
    await db.commit()
    return account


GATEWAY = {
    "slug": "corp-gateway",
    "name": "Company gateway",
    "kind": "gateway",
    "payer": "employer",
    "route_patterns": ["corp-*"],
    "default_cost_type": "api",
}
PERSONAL_KEY = {
    "slug": "my-key",
    "name": "My API key",
    "kind": "api_key",
    "payer": "employee",
    "reimburse_share": "0.5",
    "default_cost_type": "api",
}


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


async def test_new_events_are_categorised() -> None:
    for event in (
        AuditEvent.AI_BILLING_ACCOUNT_CREATED,
        AuditEvent.AI_BILLING_ACCOUNT_UPDATED,
        AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED,
        AuditEvent.AI_USAGE_PAYER_DEFAULT_CHANGED,
    ):
        assert EVENT_CATEGORIES[event] is AuditCategory.ADMINISTRATION
    assert EVENT_CATEGORIES[AuditEvent.AI_BILLING_ACCOUNTS_CHANGED] is AuditCategory.AGENTS


async def test_instance_account_changes_are_audited(db_session: AsyncSession, admin: User) -> None:
    account = await _account(db_session, admin, route_patterns=["corp-*"])
    [created] = await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_CREATED)
    assert created.user_id == admin.id and created.resource_type == "ai_billing_account"
    assert created.resource_id == account.id
    assert created.details["owner"] == "instance" and created.details["route_patterns"] == ["corp-*"]

    await _service.update(db_session, account, {"name": "Account corp-gw"}, actor=admin)
    assert await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_UPDATED) == []  # nothing changed

    await _service.update(db_session, account, {"payer": "employee", "reimburse_share": "0.25"}, actor=admin)
    [updated] = await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_UPDATED)
    assert updated.details["changes"] == {
        "payer": {"old": "employer", "new": "employee"},
        "reimburse_share": {"old": None, "new": "0.25"},
    }

    await _service.deactivate(db_session, account, actor=admin)
    [off] = await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED)
    assert off.details["changes"] == {"is_active": {"old": True, "new": False}}


async def test_own_changes_are_not_audited_but_an_admins_are(
    db_session: AsyncSession, admin: User, alice: User
) -> None:
    own = await _account(db_session, alice, owner=alice, slug="alice-key", kind="api_key", payer="employee")
    await _service.update(db_session, own, {"reimburse_share": "0.5"}, actor=alice)
    await _service.deactivate(db_session, own, actor=alice)
    for event in (
        AuditEvent.AI_BILLING_ACCOUNT_CREATED,
        AuditEvent.AI_BILLING_ACCOUNT_UPDATED,
        AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED,
    ):
        assert await _audit(db_session, event) == []

    await _service.update(db_session, own, {"is_active": True}, actor=admin)
    [row] = await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_UPDATED)
    assert row.user_id == admin.id and row.details["owner"] == "bui_alice"
    assert row.details["owner_user_id"] == alice.id


# ---------------------------------------------------------------------------
# REST: my accounts
# ---------------------------------------------------------------------------


async def test_person_manages_own_accounts(client: AsyncClient, db_session: AsyncSession, admin: User, alice: User):
    await _account(db_session, admin, slug="corp-gw")
    resp = await client.post(MY, json=PERSONAL_KEY, headers=_bearer(alice))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["owner_user_id"] == alice.id and body["owner_login"] == "bui_alice"
    assert body["reimburse_share"] == "0.5" and body["is_instance"] is False
    account_id = body["id"]

    listed = (await client.get(MY, headers=_bearer(alice))).json()
    assert [a["slug"] for a in listed] == ["corp-gw", "my-key"]  # instance first

    resp = await client.patch(f"{MY}{account_id}/", json={"reimburse_share": None}, headers=_bearer(alice))
    assert resp.status_code == 200 and resp.json()["reimburse_share"] is None

    resp = await client.post(f"{MY}{account_id}/deactivate/", headers=_bearer(alice))
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    assert [a["slug"] for a in (await client.get(MY, headers=_bearer(alice))).json()] == ["corp-gw"]
    listed = (await client.get(f"{MY}?include_inactive=true", headers=_bearer(alice))).json()
    assert [a["slug"] for a in listed] == ["corp-gw", "my-key"]

    resp = await client.patch(f"{MY}{account_id}/", json={"is_active": True}, headers=_bearer(alice))
    assert resp.json()["is_active"] is True


async def test_validation_and_conflicts(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    resp = await client.post(MY, json={**PERSONAL_KEY, "slug": "Bad Slug"}, headers=_bearer(alice))
    assert resp.status_code == 422
    resp = await client.post(MY, json={**PERSONAL_KEY, "payer": "employer"}, headers=_bearer(alice))
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "ai_billing_account_invalid"
    assert resp.json()["errors"][0]["field"] == "reimburse_share"
    resp = await client.post(MY, json={**PERSONAL_KEY, "reimburse_share": 0.5}, headers=_bearer(alice))
    assert resp.status_code == 422  # a JSON number, not a string
    assert (await client.post(MY, json=PERSONAL_KEY, headers=_bearer(alice))).status_code == 201
    resp = await client.post(MY, json=PERSONAL_KEY, headers=_bearer(alice))
    assert resp.status_code == 409 and resp.json()["errors"][0]["code"] == "ai_billing_account_exists"


async def test_person_cannot_change_instance_or_others_accounts(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, bob: User
) -> None:
    corp = await _account(db_session, admin)
    bobs = await _account(db_session, bob, owner=bob, slug="bob-key", kind="api_key", payer="employee")

    # Instance accounts are visible to everyone, but read-only on /my/.
    assert (await client.get(f"{MY}{corp.id}/", headers=_bearer(alice))).status_code == 200
    resp = await client.patch(f"{MY}{corp.id}/", json={"name": "Mine now"}, headers=_bearer(alice))
    assert resp.status_code == 404
    assert (await client.post(f"{MY}{corp.id}/deactivate/", headers=_bearer(alice))).status_code == 404

    # Somebody else's personal account does not exist for alice.
    assert (await client.get(f"{MY}{bobs.id}/", headers=_bearer(alice))).status_code == 404
    assert (await client.patch(f"{MY}{bobs.id}/", json={"name": "x"}, headers=_bearer(alice))).status_code == 404
    assert "bob-key" not in [a["slug"] for a in (await client.get(MY, headers=_bearer(alice))).json()]

    # Nor do the admin routes.
    for method, url in (("GET", ADMIN), ("POST", ADMIN), ("PATCH", f"{ADMIN}{corp.id}/")):
        resp = await client.request(method, url, json=GATEWAY if method != "GET" else None, headers=_bearer(alice))
        assert resp.status_code == 403, (method, url)
    await db_session.refresh(corp)
    assert corp.name == "Account corp-gw"


async def test_self_service_off_refuses_changes(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    created = (await client.post(MY, json=PERSONAL_KEY, headers=_bearer(alice))).json()
    await _self_service(db_session, False)
    assert (await client.get(MY, headers=_bearer(alice))).status_code == 200
    resp = await client.post(MY, json={**PERSONAL_KEY, "slug": "other"}, headers=_bearer(alice))
    assert resp.status_code == 403
    resp = await client.patch(f"{MY}{created['id']}/", json={"name": "x"}, headers=_bearer(alice))
    assert resp.status_code == 403
    assert (await client.post(f"{MY}{created['id']}/deactivate/", headers=_bearer(alice))).status_code == 403


async def test_service_account_owns_none(client: AsyncClient, db_session: AsyncSession, agent: User) -> None:
    _key, raw = await ApiKeyService().create_key(
        db_session, user_id=agent.id, name="bui-agent", check_scope_access=False
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    resp = await client.post(MY, json=PERSONAL_KEY, headers=headers)
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "ai_billing_account_owner_invalid"
    assert (await client.get(MY, headers=headers)).status_code == 200


async def test_project_limited_key_is_refused(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    project = ProjectFactory.build(key="BUILIM", identifier="bui-lim")
    db_session.add(project)
    await db_session.commit()
    _key, raw = await ApiKeyService().create_key(
        session=db_session, user_id=admin.id, name="bui-limited", scopes={"projects": [project.key]}
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.get(MY, headers=headers)).status_code == 403
    assert (await client.post(MY, json=PERSONAL_KEY, headers=headers)).status_code == 403
    assert (await client.get(ADMIN, headers=headers)).status_code == 403
    assert (await client.post(ADMIN, json=GATEWAY, headers=headers)).status_code == 403


# ---------------------------------------------------------------------------
# REST: administration
# ---------------------------------------------------------------------------


async def test_admin_manages_instance_and_personal_accounts(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User
) -> None:
    resp = await client.post(ADMIN, json=GATEWAY, headers=_bearer(admin))
    assert resp.status_code == 201, resp.text
    corp = resp.json()
    assert corp["is_instance"] is True and corp["route_patterns"] == ["corp-*"]

    resp = await client.post(ADMIN, json={**PERSONAL_KEY, "owner_user_id": alice.id}, headers=_bearer(admin))
    assert resp.status_code == 201 and resp.json()["owner_login"] == "bui_alice"
    alices = resp.json()
    resp = await client.post(ADMIN, json={**PERSONAL_KEY, "owner_user_id": 2**30}, headers=_bearer(admin))
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == "user_not_found"

    page = (await client.get(ADMIN, headers=_bearer(admin))).json()
    assert [a["slug"] for a in page["items"]] == ["corp-gateway"]
    page = (await client.get(f"{ADMIN}?scope=personal&user_id={alice.id}", headers=_bearer(admin))).json()
    assert [a["id"] for a in page["items"]] == [alices["id"]] and page["total_count"] == 1
    page = (await client.get(f"{ADMIN}?scope=all&limit=200", headers=_bearer(admin))).json()
    assert {corp["id"], alices["id"]} <= {a["id"] for a in page["items"]}

    resp = await client.patch(f"{ADMIN}{alices['id']}/", json={"reimburse_share": "1"}, headers=_bearer(admin))
    assert resp.status_code == 200 and resp.json()["reimburse_share"] == "1"
    resp = await client.post(f"{ADMIN}{corp['id']}/deactivate/", headers=_bearer(admin))
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    assert (await client.get(f"{ADMIN}{corp['id']}/", headers=_bearer(admin))).json()["is_active"] is False
    assert (await client.get(f"{ADMIN}{2**30}/", headers=_bearer(admin))).status_code == 404

    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_CREATED)) == 2
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_UPDATED)) == 1
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED)) == 1


async def test_subscriptions_link_to_their_account(client: AsyncClient, db_session: AsyncSession, alice: User):
    resp = await client.post(
        "/api/v1/my/ai-subscriptions/",
        json={"plan_key": "claude-pro", "starts_on": "2026-01-01"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 201, resp.text
    sub = resp.json()
    assert sub["billing_account_id"] is not None
    [account] = (await client.get(MY, headers=_bearer(alice))).json()
    assert account["id"] == sub["billing_account_id"] and account["subscription_ids"] == [sub["id"]]
    assert account["kind"] == "subscription"


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


def _form(**values: object) -> dict[str, object]:
    base: dict[str, object] = {
        "slug": "my-key",
        "name": "My API key",
        "kind": "api_key",
        "payer": "employee",
        "reimburse_share": "0.5",
        "route_patterns": "",
        "default_cost_type": "api",
    }
    base.update(values)
    return base


async def test_my_page_lists_adds_edits_and_deactivates(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, bob: User
) -> None:
    await _account(db_session, admin, slug="corp-gw", route_patterns=["corp-*"])
    await _account(db_session, bob, owner=bob, slug="bob-key", kind="api_key", payer="employee")
    resp = await client.get("/my/ai-billing-accounts/", cookies=_cookies(alice))
    assert resp.status_code == 200
    assert "corp-gw" in resp.text and "bob-key" not in resp.text
    assert 'data-testid="billing-account-form"' in resp.text

    page = "/my/ai-billing-accounts/"
    resp = await _post_form(client, alice, page, _form(route_patterns="mine-*, other"), page)
    assert resp.status_code == 303, resp.text
    [account] = await _service.personal_accounts(db_session, alice.id)
    assert account.route_patterns == ["mine-*", "other"] and account.reimburse_share == Decimal("0.5")

    resp = await client.get(f"{page}{account.id}/", cookies=_cookies(alice))
    assert resp.status_code == 200 and 'value="0.5"' in resp.text
    assert (await client.get(f"{page}{account.id}/", cookies=_cookies(bob))).status_code == 404

    # An employer-paid account keeps no share: the field is ignored rather than refused.
    resp = await _post_form(
        client, alice, f"{page}{account.id}/", _form(payer="employer", is_active="1", name="Renamed"), page
    )
    assert resp.status_code == 303, resp.text
    await db_session.refresh(account)
    assert (account.name, account.payer, account.reimburse_share) == ("Renamed", "employer", None)

    resp = await _post_form(client, bob, f"{page}{account.id}/deactivate/", {}, page)
    assert resp.status_code == 404
    resp = await _post_form(client, alice, f"{page}{account.id}/deactivate/", {}, page)
    assert resp.status_code == 303
    await db_session.refresh(account)
    assert account.is_active is False


async def test_my_page_shows_errors_and_keeps_input(client: AsyncClient, db_session: AsyncSession, alice: User):
    page = "/my/ai-billing-accounts/"
    resp = await _post_form(client, alice, page, _form(reimburse_share="1.5"), page)
    assert resp.status_code == 422
    assert 'data-testid="billing-account-error"' in resp.text and 'value="1.5"' in resp.text
    resp = await _post_form(client, alice, page, _form(slug="Not ok"), page)
    assert resp.status_code == 422 and 'aria-invalid="true"' in resp.text


async def test_my_page_with_self_service_off(client: AsyncClient, db_session: AsyncSession, alice: User) -> None:
    await _account(db_session, alice, owner=alice, slug="alice-key", kind="api_key", payer="employee")
    await _self_service(db_session, False)
    resp = await client.get("/my/ai-billing-accounts/", cookies=_cookies(alice))
    assert resp.status_code == 200 and 'data-testid="billing-account-self-service-off"' in resp.text
    assert 'data-testid="billing-account-form"' not in resp.text and "alice-key" in resp.text
    page = "/my/ai-billing-accounts/"
    resp = await _post_form(client, alice, page, _form(), page)
    assert resp.status_code == 422 and "administrator manages" in resp.text


async def test_admin_page(client: AsyncClient, db_session: AsyncSession, admin: User, alice: User) -> None:
    page = "/admin/ai-billing-accounts/"
    assert (await client.get(page, cookies=_cookies(alice))).status_code == 403
    resp = await _post_form(
        client,
        admin,
        page,
        _form(slug="corp-gw", name="Company gateway", kind="gateway", payer="employer", reimburse_share="",
              route_patterns="corp-*", tool_slugs=["claude-code"]),
        page,
    )  # fmt: skip
    assert resp.status_code == 303, resp.text
    [account] = await _service.instance_accounts(db_session)
    assert account.tool_slugs == ["claude-code"] and account.route_patterns == ["corp-*"]
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_CREATED)) == 1

    resp = await client.get(page, cookies=_cookies(admin))
    assert resp.status_code == 200 and "Company gateway" in resp.text
    resp = await client.get(f"{page}{account.id}/", cookies=_cookies(admin))
    assert resp.status_code == 200 and 'data-testid="billing-account-deactivate"' in resp.text
    resp = await _post_form(client, admin, page, _form(slug="corp-gw", payer="employer", reimburse_share=""), page)
    assert resp.status_code == 422 and "exists" in resp.text

    resp = await _post_form(client, admin, f"{page}{account.id}/deactivate/", {}, page)
    assert resp.status_code == 303
    await db_session.refresh(account)
    assert account.is_active is False
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED)) == 1

    # Admins open people's accounts by id too.
    alices = await _account(db_session, alice, owner=alice, slug="alice-key", kind="api_key", payer="employee")
    resp = await client.get(f"{page}{alices.id}/", cookies=_cookies(admin))
    assert resp.status_code == 200 and "bui_alice" in resp.text


async def test_subscriptions_pages_link_to_the_account(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User
) -> None:
    resp = await client.post(
        "/api/v1/my/ai-subscriptions/",
        json={"plan_key": "claude-pro", "starts_on": "2026-01-01"},
        headers=_bearer(alice),
    )
    account_id = resp.json()["billing_account_id"]
    resp = await client.get("/my/ai-subscriptions/", cookies=_cookies(alice))
    assert f'/my/ai-billing-accounts/{account_id}/"' in resp.text
    resp = await client.get("/admin/ai-subscriptions/", cookies=_cookies(admin))
    assert f'/admin/ai-billing-accounts/{account_id}/"' in resp.text


# ---------------------------------------------------------------------------
# Project payer default
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession, alice: User, bob: User, admin: User) -> Project:
    """``BUIP``: alice manages it and logs AI usage there; bob only logs usage."""
    db = db_session
    project: Project = ProjectFactory.build(key="BUIP", identifier="buip", is_public=False)
    db.add(project)
    await db.flush()
    manager = Role(
        name="Buip Manager",
        permissions=["view_issues", "manage_project", "view_ai_usage", "log_ai_usage"],
        builtin=0,
        issues_visibility="default",
        ai_usage_visibility="all",
    )
    logger_role = Role(
        name="Buip Logger",
        permissions=["view_issues", "view_ai_usage", "log_ai_usage"],
        builtin=0,
        issues_visibility="default",
        ai_usage_visibility="own",
    )
    db.add_all([manager, logger_role])
    await db.flush()
    for user, role in ((alice, manager), (bob, logger_role)):
        member = Member(project_id=project.id, user_id=user.id)
        db.add(member)
        await db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))
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
    await db.commit()
    clear_role_cache(db)
    return project


async def _ingest(client: AsyncClient, db: AsyncSession, user: User, key: str) -> int:
    _k, raw = await ApiKeyService().create_key(db, user_id=user.id, name=f"bui-{key}", check_scope_access=False)
    await db.commit()
    body = {
        "schema": "specivo.ai-usage.v1",
        "client": {"name": "billing-ui-tests"},
        "records": [
            {
                "idempotency_key": key,
                "tool": "claude-code",
                "provider": "testlab",
                "model": MODEL,
                "usage_date": "2026-11-05",
                "tokens": {"input": 1_000_000},
                "cost_type": "api",
                "attribution": {"project_key": "BUIP"},
            }
        ],
    }
    resp = await client.post("/api/v1/ai-usage/ingest", json=body, headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["status"] == "created", result
    entry_id: int = result["entry_id"]
    return entry_id


async def test_project_payer_default_through_the_api_re_resolves_entries(
    client: AsyncClient, db_session: AsyncSession, alice: User, project: Project
) -> None:
    entry_id = await _ingest(client, db_session, alice, "buip-1")
    entry = await db_session.get(AiUsageEntry, entry_id)
    assert entry is not None and entry.payer == "unknown"

    url = f"/api/v1/projects/{project.key}/"
    assert (await client.get(url, headers=_bearer(alice))).json()["ai_usage_payer_default"] == "none"

    resp = await client.patch(url, json={"ai_usage_payer_default": "employer"}, headers=_bearer(alice))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ai_usage_payer_default"] == "employer"
    await db_session.refresh(project)
    assert project.settings[PROJECT_PAYER_DEFAULT_KEY] == "employer"
    await db_session.refresh(entry)
    assert (entry.payer, entry.billing_method, entry.revision) == ("employer", "project_default", 2)

    [row] = await _audit(db_session, AuditEvent.AI_USAGE_PAYER_DEFAULT_CHANGED)
    assert row.project_id == project.id and row.user_id == alice.id and row.resource_type == "setting"
    assert row.details == {"project_key": "BUIP", "old": "none", "new": "employer", "entries": 1, "changed": 1}
    [recompute] = await _audit(db_session, AuditEvent.AI_USAGE_COSTS_RECOMPUTED)
    assert recompute.details["project_key"] == "BUIP"

    # The same value again changes nothing and writes nothing.
    resp = await client.patch(url, json={"ai_usage_payer_default": "employer"}, headers=_bearer(alice))
    assert resp.status_code == 200
    assert len(await _audit(db_session, AuditEvent.AI_USAGE_PAYER_DEFAULT_CHANGED)) == 1

    # "none" removes the key, and the entry's payer is unknown again.
    resp = await client.patch(url, json={"ai_usage_payer_default": "none"}, headers=_bearer(alice))
    assert resp.status_code == 200 and resp.json()["ai_usage_payer_default"] == "none"
    await db_session.refresh(project)
    assert PROJECT_PAYER_DEFAULT_KEY not in (project.settings or {})
    await db_session.refresh(entry)
    assert (entry.payer, entry.billing_method) == ("unknown", "none")


async def test_project_payer_default_needs_manage_project(
    client: AsyncClient, db_session: AsyncSession, bob: User, project: Project
) -> None:
    url = f"/api/v1/projects/{project.key}/"
    resp = await client.patch(url, json={"ai_usage_payer_default": "employee"}, headers=_bearer(bob))
    assert resp.status_code == 403
    resp = await client.patch(url, json={"ai_usage_payer_default": "somebody"}, headers=_bearer(bob))
    assert resp.status_code == 422
    await db_session.refresh(project)
    assert PROJECT_PAYER_DEFAULT_KEY not in (project.settings or {})
    assert await _audit(db_session, AuditEvent.AI_USAGE_PAYER_DEFAULT_CHANGED) == []


async def test_project_payer_default_leaves_entries_an_account_decided(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, project: Project
) -> None:
    await _account(db_session, admin, slug="corp-gw", route_patterns=["corp-*"], default_cost_type="api")
    _k, raw = await ApiKeyService().create_key(
        db_session, user_id=alice.id, name="bui-routed", check_scope_access=False
    )
    await db_session.commit()
    body = {
        "schema": "specivo.ai-usage.v1",
        "client": {"name": "billing-ui-tests"},
        "records": [
            {
                "idempotency_key": "buip-routed",
                "tool": "claude-code",
                "provider": "testlab",
                "model": MODEL,
                "usage_date": "2026-11-05",
                "tokens": {"input": 1_000_000},
                "route": "corp-litellm",
                "attribution": {"project_key": "BUIP"},
            }
        ],
    }
    resp = await client.post("/api/v1/ai-usage/ingest", json=body, headers={"Authorization": f"Bearer {raw}"})
    [result] = resp.json()["results"]
    assert result["payer"] == "employer" and result["billing_account"] == "corp-gw"
    resp = await client.patch(
        f"/api/v1/projects/{project.key}/", json={"ai_usage_payer_default": "employee"}, headers=_bearer(alice)
    )
    assert resp.status_code == 200
    entry = await db_session.get(AiUsageEntry, result["entry_id"])
    assert entry is not None
    await db_session.refresh(entry)
    assert (entry.payer, entry.billing_method) == ("employer", "instance_route")
    [row] = await _audit(db_session, AuditEvent.AI_USAGE_PAYER_DEFAULT_CHANGED)
    assert row.details["entries"] == 0


async def test_project_settings_page_shows_the_payer_default(
    client: AsyncClient, db_session: AsyncSession, alice: User, project: Project
) -> None:
    project.settings = {**(project.settings or {}), PROJECT_PAYER_DEFAULT_KEY: "employee"}
    await db_session.commit()
    resp = await client.get(f"/projects/{project.key}/settings/", cookies=_cookies(alice))
    assert resp.status_code == 200
    assert 'data-testid="project-ai-payer-default"' in resp.text
    assert "payerDefault: &#34;employee&#34;" in resp.text or 'payerDefault: "employee"' in resp.text
