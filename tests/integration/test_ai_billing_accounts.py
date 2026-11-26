"""AI billing accounts: the table's checks, who may manage which account, lookups, the subscription link,
the payer and route on usage entries and the project payer default.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import PROJECT_PAYER_DEFAULT_KEY, project_payer_default
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService, BillingAccountInput
from specivo.services.ai_subscription_service import (
    SELF_SERVICE_SETTING_KEY,
    AiSubscriptionService,
    SubscriptionInput,
)
from specivo.services.settings_service import SettingsService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = AiBillingAccountService()
_subscriptions = AiSubscriptionService()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user: User = AdminUserFactory.build(login="bill_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def alice(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="bill_alice", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def bob(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="bill_bob", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


def _gateway(slug: str = "corp-gateway", **values: object) -> BillingAccountInput:
    defaults: dict[str, object] = {
        "name": "Corporate gateway",
        "kind": "gateway",
        "payer": "employer",
        "route_patterns": ["litellm-*"],
        "default_cost_type": "api",
    }
    defaults.update(values)
    return BillingAccountInput(slug=slug, **defaults)  # type: ignore[arg-type]


def _personal(slug: str = "my-key", **values: object) -> BillingAccountInput:
    defaults: dict[str, object] = {
        "name": "My API key",
        "kind": "api_key",
        "payer": "employee",
        "reimburse_share": "0.5",
    }
    defaults.update(values)
    return BillingAccountInput(slug=slug, **defaults)  # type: ignore[arg-type]


async def _self_service(db: AsyncSession, enabled: bool) -> None:
    await SettingsService().set_many(db, {SELF_SERVICE_SETTING_KEY: "true" if enabled else "false"})
    await db.commit()


def _entry(user: User, **values: object) -> AiUsageEntry:
    return AiUsageEntry(
        user_id=user.id,
        reporter_user_id=user.id,
        source="manual",
        tool_slug="claude-code",
        model="lab-model-1",
        usage_date=date(2026, 1, 15),
        priced_at=datetime(2026, 1, 15, tzinfo=UTC),
        **values,
    )


# ---------------------------------------------------------------------------
# Table checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {"slug": "Bad Slug"},
        {"name": "  "},
        {"kind": "card"},
        {"payer": "nobody"},
        {"payer": "employer", "reimburse_share": Decimal("0.5")},
        {"reimburse_share": Decimal("1.5")},
        {"default_cost_type": "unknown"},
    ],
)
async def test_database_checks(db_session: AsyncSession, values: dict) -> None:
    row = {
        "slug": "check-me",
        "name": "Check",
        "kind": "gateway",
        "payer": "employee",
        **values,
    }
    db_session.add(AiBillingAccount(**row))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_slug_is_unique_per_owner_and_among_instance_accounts(
    db_session: AsyncSession, alice: User, bob: User
) -> None:
    for owner in (None, alice.id, bob.id):
        db_session.add(AiBillingAccount(slug="shared", name="S", kind="gateway", payer="employer", owner_user_id=owner))
    await db_session.flush()

    for owner in (None, alice.id):
        savepoint = await db_session.begin_nested()
        db_session.add(AiBillingAccount(slug="shared", name="S", kind="api_key", payer="employee", owner_user_id=owner))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await savepoint.rollback()


async def test_entry_payer_defaults_to_unknown_and_is_checked(db_session: AsyncSession, alice: User) -> None:
    entry = _entry(alice, route="LiteLLM-EU")
    db_session.add(entry)
    await db_session.flush()
    await db_session.refresh(entry)
    assert (entry.payer, entry.billing_account_id, entry.route) == ("unknown", None, "LiteLLM-EU")

    savepoint = await db_session.begin_nested()
    with pytest.raises(IntegrityError):
        await db_session.execute(text("UPDATE ai_usage_entries SET payer = 'nobody' WHERE id = :id"), {"id": entry.id})
    await savepoint.rollback()


async def test_deleting_an_account_unlinks_entries_and_subscriptions(db_session: AsyncSession, alice: User) -> None:
    sub = await _subscriptions.create(
        db_session, SubscriptionInput(plan_key="claude-pro", starts_on=date(2026, 1, 1)), owner=alice, actor=alice
    )
    account_id = sub.billing_account_id
    assert account_id is not None
    entry = _entry(alice, billing_account_id=account_id, payer="employee")
    db_session.add(entry)
    await db_session.flush()

    await db_session.execute(text("DELETE FROM ai_billing_accounts WHERE id = :id"), {"id": account_id})
    linked = await db_session.execute(
        text(
            "SELECT (SELECT billing_account_id FROM ai_subscriptions WHERE id = :sub),"
            " (SELECT billing_account_id FROM ai_usage_entries WHERE id = :entry)"
        ),
        {"sub": sub.id, "entry": entry.id},
    )
    assert linked.one() == (None, None)


# ---------------------------------------------------------------------------
# Subscriptions get an account
# ---------------------------------------------------------------------------


async def test_a_new_subscription_is_the_fee_schedule_of_a_personal_account(
    db_session: AsyncSession, admin: User, alice: User
) -> None:
    sub = await _subscriptions.create(
        db_session,
        SubscriptionInput(plan_key="claude-max-5x", starts_on=date(2026, 1, 1), tool_slugs=["claude-code"]),
        owner=alice,
        actor=admin,
    )
    account = await _service.get(db_session, sub.billing_account_id or 0)
    assert account.slug == f"claude-max-5x-{sub.id}"
    assert (account.owner_user_id, account.kind, account.payer) == (alice.id, "subscription", "employee")
    assert account.name == sub.plan_name
    assert account.default_cost_type == "subscription"
    assert account.tool_slugs == []
    assert account.created_by_id == admin.id

    await _subscriptions.delete(db_session, sub, actor=alice)
    assert await db_session.get(AiBillingAccount, account.id) is not None


# ---------------------------------------------------------------------------
# Who manages what
# ---------------------------------------------------------------------------


async def test_admin_creates_instance_accounts(db_session: AsyncSession, admin: User) -> None:
    account = await _service.create(db_session, _gateway(), owner=None, actor=admin)
    assert account.is_instance
    assert (account.slug, account.payer, account.route_patterns, account.default_cost_type) == (
        "corp-gateway",
        "employer",
        ["litellm-*"],
        "api",
    )
    assert account.reimburse_share is None and account.is_active
    assert account.created_by_id == admin.id


async def test_people_cannot_manage_instance_accounts(db_session: AsyncSession, admin: User, alice: User) -> None:
    with pytest.raises(PermissionDeniedError):
        await _service.create(db_session, _gateway(), owner=None, actor=alice)
    account = await _service.create(db_session, _gateway(), owner=None, actor=admin)
    with pytest.raises(PermissionDeniedError):
        await _service.update(db_session, account, {"name": "Mine now"}, actor=alice)
    assert not await _service.can_manage(db_session, alice, None)
    assert await _service.can_manage(db_session, admin, None)


async def test_people_manage_their_own_accounts_under_self_service(
    db_session: AsyncSession, admin: User, alice: User, bob: User
) -> None:
    account = await _service.create(db_session, _personal(), owner=alice, actor=alice)
    assert (account.owner_user_id, account.reimburse_share) == (alice.id, Decimal("0.5"))

    with pytest.raises(NotFoundError):
        await _service.update(db_session, account, {"name": "Bob's"}, actor=bob)
    with pytest.raises(NotFoundError):
        await _service.create(db_session, _personal("bobs"), owner=bob, actor=alice)

    await _self_service(db_session, False)
    assert not await _service.can_manage(db_session, alice, alice.id)
    with pytest.raises(PermissionDeniedError):
        await _service.update(db_session, account, {"name": "Renamed"}, actor=alice)
    # Administrators still can.
    await _service.update(db_session, account, {"name": "Renamed"}, actor=admin)
    assert account.name == "Renamed"


async def test_service_accounts_own_no_accounts(db_session: AsyncSession, admin: User) -> None:
    agent: User = ServiceAccountFactory.build(login="bill_agent")
    db_session.add(agent)
    await db_session.flush()
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _personal(), owner=agent, actor=admin)
    assert exc.value.code == "ai_billing_account_owner_invalid"


async def test_visibility(db_session: AsyncSession, admin: User, alice: User, bob: User) -> None:
    gateway = await _service.create(db_session, _gateway(), owner=None, actor=admin)
    mine = await _service.create(db_session, _personal(), owner=alice, actor=alice)
    theirs = await _service.create(db_session, _personal(), owner=bob, actor=bob)

    assert [a.id for a in await _service.list_visible(db_session, alice)] == [gateway.id, mine.id]
    assert (await _service.get_visible(db_session, alice, gateway.id)).id == gateway.id
    with pytest.raises(NotFoundError):
        await _service.get_visible(db_session, alice, theirs.id)
    assert (await _service.get_visible(db_session, admin, theirs.id)).id == theirs.id


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"slug": "Not valid"}, "slug"),
        ({"name": ""}, "name"),
        ({"kind": "card"}, "kind"),
        ({"payer": "unknown"}, "payer"),
        ({"reimburse_share": "2"}, "reimburse_share"),
        ({"payer": "employer", "reimburse_share": "0.2"}, "reimburse_share"),
        ({"route_patterns": ["two words"]}, "route_patterns"),
        ({"tool_slugs": ["?"]}, "tool_slugs"),
        ({"default_cost_type": "unknown"}, "default_cost_type"),
    ],
)
async def test_validation(db_session: AsyncSession, alice: User, values: dict, field: str) -> None:
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _personal(**values), owner=alice, actor=alice)
    assert (exc.value.code, exc.value.field, exc.value.status_code) == ("ai_billing_account_invalid", field, 422)


async def test_duplicate_slug_is_a_conflict(db_session: AsyncSession, admin: User, alice: User) -> None:
    await _service.create(db_session, _personal("dup"), owner=alice, actor=alice)
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _personal("DUP"), owner=alice, actor=alice)
    assert (exc.value.code, exc.value.status_code) == ("ai_billing_account_exists", 409)
    # The same slug is free for an instance account and on rename to itself.
    await _service.create(db_session, _gateway("dup"), owner=None, actor=admin)
    other = await _service.create(db_session, _personal("other"), owner=alice, actor=alice)
    with pytest.raises(AppError):
        await _service.update(db_session, other, {"slug": "dup"}, actor=alice)
    await _service.update(db_session, other, {"slug": "other", "name": "Other"}, actor=alice)


async def test_update_rules(db_session: AsyncSession, alice: User) -> None:
    account = await _service.create(db_session, _personal(), owner=alice, actor=alice)
    with pytest.raises(AppError) as exc:
        await _service.update(db_session, account, {"owner_user_id": 1}, actor=alice)
    assert exc.value.field == "owner_user_id"

    # Switching to the employer drops the share.
    await _service.update(db_session, account, {"payer": "employer"}, actor=alice)
    assert (account.payer, account.reimburse_share) == ("employer", None)

    await _service.update(db_session, account, {"route_patterns": "Home-*, lab", "tool_slugs": "codex"}, actor=alice)
    assert (account.route_patterns, account.tool_slugs) == (["home-*", "lab"], ["codex"])

    await _service.deactivate(db_session, account, actor=alice)
    assert not account.is_active


# ---------------------------------------------------------------------------
# Lookups for ingest
# ---------------------------------------------------------------------------


async def test_find_by_slug_prefers_the_users_own_account(
    db_session: AsyncSession, admin: User, alice: User, bob: User
) -> None:
    gateway = await _service.create(db_session, _gateway("shared"), owner=None, actor=admin)
    own = await _service.create(db_session, _personal("shared"), owner=alice, actor=alice)

    assert (await _service.find_by_slug(db_session, "Shared", user_id=alice.id)) is own
    assert (await _service.find_by_slug(db_session, "shared", user_id=bob.id)) is gateway
    assert await _service.find_by_slug(db_session, "missing", user_id=bob.id) is None
    assert await _service.find_by_slug(db_session, "not a slug", user_id=bob.id) is None

    await _service.deactivate(db_session, own, actor=alice)
    assert (await _service.find_by_slug(db_session, "shared", user_id=alice.id)) is gateway
    assert (await _service.find_by_slug(db_session, "shared", user_id=alice.id, active_only=False)) is own


async def test_instance_and_personal_account_lists(
    db_session: AsyncSession, admin: User, alice: User, bob: User
) -> None:
    first = await _service.create(db_session, _gateway("gw-one"), owner=None, actor=admin)
    second = await _service.create(db_session, _gateway("gw-two", is_active=False), owner=None, actor=admin)
    own = await _service.create(db_session, _personal(), owner=alice, actor=alice)
    await _service.create(db_session, _personal(), owner=bob, actor=bob)

    instance_ids = [a.id for a in await _service.instance_accounts(db_session)]
    assert first.id in instance_ids and second.id not in instance_ids
    assert second.id in [a.id for a in await _service.instance_accounts(db_session, active_only=False)]
    assert [a.id for a in await _service.personal_accounts(db_session, alice.id)] == [own.id]


# ---------------------------------------------------------------------------
# Project payer default
# ---------------------------------------------------------------------------


async def test_project_payer_default(db_session: AsyncSession) -> None:
    project: Project = ProjectFactory.build(settings={"similar_issues": False})
    db_session.add(project)
    await db_session.flush()
    assert project_payer_default(project.settings) == "none"

    _service.set_project_payer_default(project, "employer")
    await db_session.flush()
    stored = (await db_session.execute(select(Project.settings).where(Project.id == project.id))).scalar_one()
    assert stored == {"similar_issues": False, PROJECT_PAYER_DEFAULT_KEY: "employer"}

    _service.set_project_payer_default(project, "none")
    await db_session.flush()
    stored = (await db_session.execute(select(Project.settings).where(Project.id == project.id))).scalar_one()
    assert stored == {"similar_issues": False}

    with pytest.raises(AppError) as exc:
        _service.set_project_payer_default(project, "unknown")
    assert exc.value.field == PROJECT_PAYER_DEFAULT_KEY


async def test_subscription_link_column_is_loaded(db_session: AsyncSession, alice: User) -> None:
    sub = await _subscriptions.create(
        db_session, SubscriptionInput(plan_key="chatgpt-plus", starts_on=date(2026, 1, 1)), owner=alice, actor=alice
    )
    await db_session.commit()
    stored = (
        await db_session.execute(select(AiSubscription.billing_account_id).where(AiSubscription.id == sub.id))
    ).scalar_one()
    assert stored is not None
    assert stored == sub.billing_account_id
