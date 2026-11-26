"""An agent (service account) that works for a person is billed through that person's accounts and plans.

The world of ``test_ai_usage_billing`` plus ``agent``, a service account that
reports usage with its own API key. ``alice`` holds a subscription covering
``claude-code`` and, in some tests, a personal key routed ``home-*``.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from tests.factories.user import ServiceAccountFactory, UserFactory
from tests.integration.test_ai_usage_billing import World, billing, build_world, gateway, one, record, stored

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    return await build_world(db_session)


async def _agent(db: AsyncSession, w: World) -> User:
    agent: User = ServiceAccountFactory.build(login="bilr_agent", status="active")
    db.add(agent)
    await db.flush()
    _key, raw = await ApiKeyService().create_key(db, user_id=agent.id, name="agent-key", check_scope_access=False)
    w.keys["agent"] = raw
    _key, raw = await ApiKeyService().create_key(db, user_id=w.admin.id, name="admin-key", check_scope_access=False)
    w.keys["admin"] = raw
    await db.commit()
    return agent


async def _works_for(client: AsyncClient, w: World, agent: User, owner: User | None) -> Response:
    return await client.put(
        f"/api/v1/admin/users/{agent.id}/works-for/",
        json={"works_for_id": owner.id if owner else None},
        headers=w.auth("admin"),
    )


async def test_an_agent_is_billed_through_the_accounts_of_the_person_it_works_for(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    agent = await _agent(db_session, w)
    await gateway(
        db_session, w, "alice-home", owner=w.alice, kind="api_key", payer="employee", route_patterns=["home-*"]
    )

    routed = await one(client, w, "agent", record("k-agent-route", route="home-1", cost_type="api"))
    covered = await one(client, w, "agent", record("k-agent-plan"))
    # On its own the agent has no account and no plan.
    assert billing(routed) == ("unknown", None) and billing(covered) == ("unknown", None)

    resp = await _works_for(client, w, agent, w.alice)
    assert resp.status_code == 200, resp.text
    assert resp.json()["works_for_id"] == w.alice.id

    # Its entries were resolved again as alice's: her key, her plan; they stay the agent's.
    entry = await stored(db_session, routed["entry_id"])
    assert (entry.user_id, entry.payer, entry.billing_method) == (agent.id, "employee", "personal_route")
    entry = await stored(db_session, covered["entry_id"])
    assert (entry.billing_method, entry.cost_type, entry.subscription_id) == (
        "subscription",
        "subscription",
        w.subscription.id,
    )
    assert entry.marginal is not None and entry.marginal.amount == 0

    # New usage resolves the same way, and a slug may name alice's account.
    named = await one(client, w, "agent", record("k-agent-slug", billing_account="alice-home", cost_type="api"))
    assert billing(named) == ("employee", "alice-home")

    audit: Any = await db_session.scalar(
        SecurityAuditLog.__table__.select().where(
            SecurityAuditLog.details["action"].astext == "agent_works_for_changed"
        )
    )
    assert audit is not None

    # Cleared: billed as itself again.
    resp = await _works_for(client, w, agent, None)
    assert resp.status_code == 200 and resp.json()["works_for_id"] is None
    entry = await stored(db_session, routed["entry_id"])
    assert (entry.payer, entry.billing_account_id) == ("unknown", None)


async def test_a_change_to_the_owners_account_reaches_the_agents_entries(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    agent = await _agent(db_session, w)
    assert (await _works_for(client, w, agent, w.alice)).status_code == 200
    result = await one(client, w, "agent", record("k-agent-late", route="home-1", cost_type="api"))
    assert billing(result) == ("unknown", None)

    await gateway(
        db_session, w, "alice-home", owner=w.alice, kind="api_key", payer="employee", route_patterns=["home-*"]
    )
    entry = await stored(db_session, result["entry_id"])
    assert entry.payer == "employee"


@pytest.mark.parametrize("case", ["owner_is_agent", "owner_is_self", "target_is_person", "owner_inactive"])
async def test_only_an_agent_works_for_an_active_person(
    client: AsyncClient, db_session: AsyncSession, w: World, case: str
) -> None:
    agent = await _agent(db_session, w)
    other: User = ServiceAccountFactory.build(login="bilr_agent_two", status="active")
    gone: User = UserFactory.build(login="bilr_gone", status="deactivated")
    db_session.add_all([other, gone])
    await db_session.commit()
    target, owner = {
        "owner_is_agent": (agent, other),
        "owner_is_self": (agent, agent),
        "target_is_person": (w.bob, w.alice),
        "owner_inactive": (agent, gone),
    }[case]
    resp = await _works_for(client, w, target, owner)
    assert resp.status_code == 422, resp.text
    await db_session.refresh(target)
    assert target.works_for_id is None


async def test_the_admin_page_sets_and_clears_works_for(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    from tests.integration.test_email_change_web import Browser

    agent = await _agent(db_session, w)
    browser = Browser(client)
    await browser.sign_in(w.admin.login)

    page = await browser.get(f"/admin/users/{agent.id}/")
    assert page.status_code == 200
    assert 'data-testid="works-for"' in page.text and 'data-testid="works-for-none"' in page.text

    resp = await browser.post(f"/admin/users/{agent.id}/works-for/", {"works_for_login": w.alice.login})
    assert resp.status_code == 303 and "account=works_for_set" in resp.headers["location"]
    await db_session.refresh(agent)
    assert agent.works_for_id == w.alice.id
    page = await browser.get(f"/admin/users/{agent.id}/?account=works_for_set")
    assert 'data-testid="works-for-current"' in page.text and w.alice.login in page.text

    resp = await browser.post(f"/admin/users/{agent.id}/works-for/", {"works_for_login": "nobody-here"})
    assert "account=works_for_invalid" in resp.headers["location"]

    resp = await browser.post(f"/admin/users/{agent.id}/works-for/", {"clear": "1"})
    assert "account=works_for_cleared" in resp.headers["location"]
    await db_session.refresh(agent)
    assert agent.works_for_id is None

    # A person's page has no such section.
    page = await browser.get(f"/admin/users/{w.bob.id}/")
    assert 'data-testid="works-for"' not in page.text


async def test_a_deactivated_owner_no_longer_pays_for_the_agent(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    agent = await _agent(db_session, w)
    assert (await _works_for(client, w, agent, w.alice)).status_code == 200
    covered = await one(client, w, "agent", record("k-agent-before"))
    assert billing(covered)[0] == "employee"

    w.alice.status = "deactivated"
    await db_session.commit()
    after = await one(client, w, "agent", record("k-agent-after"))
    assert billing(after) == ("unknown", None)
