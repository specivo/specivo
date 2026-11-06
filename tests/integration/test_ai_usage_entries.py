"""AI usage entries: visibility, on-behalf authorization, editing rules, costs, snapshots, recompute and audit.

The world: two private projects, ``AIU`` (where everyone below is a member)
and ``AIV`` (nobody is), a public and a private issue in ``AIU``, and test
prices for an invented provider ``testlab`` so the arithmetic is exact:

- ``mgr``: a Manager-like role (``*``, ``ai_usage_visibility = all``);
- ``alice``, ``bob``: Developer-like (``view_ai_usage`` own, ``log_ai_usage``);
- ``carol``: ``view_ai_usage`` with ``all``, cannot see the private issue;
- ``dave``: no AI usage permission at all;
- ``runner``: a service account with ``log_ai_usage_for_others``;
- ``eve``: a deactivated member; ``outsider``: no membership; ``admin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.money import Money
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services import ai_model_price_service as price_module
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.ai_usage_service import PRICE_REFERENCE, AiUsageInput, AiUsageService
from specivo.services.api_key_scope import ProjectScope, bound_scope
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = AiUsageService()
_prices = AiModelPriceService()

MARCH = date(2026, 3, 10)
JULY = date(2026, 7, 5)
JUNE_1 = datetime(2026, 6, 1, tzinfo=UTC)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@dataclass
class World:
    p: Project
    q: Project
    public_issue: Issue
    private_issue: Issue
    users: dict[str, User]

    def __getattr__(self, name: str) -> User:
        try:
            return self.__dict__["users"][name]
        except KeyError:
            raise AttributeError(name) from None


async def _role(db: AsyncSession, name: str, permissions: list[str], visibility: str) -> Role:
    role = Role(
        name=name,
        permissions=permissions,
        builtin=0,
        issues_visibility="default",
        ai_usage_visibility=visibility,
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


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="Aiu Open")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Aiu Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Aiu Normal")
    p = ProjectFactory.build(key="AIU", identifier="aiu", is_public=False)
    q = ProjectFactory.build(key="AIV", identifier="aiv", is_public=False)
    db.add_all([tracker, priority, p, q])
    await db.flush()

    users: dict[str, User] = {}
    for name in ("mgr", "alice", "bob", "carol", "dave", "outsider"):
        users[name] = UserFactory.build(login=f"aiu_{name}", status="active")
    users["eve"] = UserFactory.build(login="aiu_eve", status="deactivated")
    users["runner"] = ServiceAccountFactory.build(login="aiu_runner", status="active")
    users["admin"] = AdminUserFactory.build(login="aiu_admin", status="active")
    db.add_all(users.values())
    await db.flush()

    mgr_role = await _role(db, "Aiu Manager", ["*"], "all")
    dev_role = await _role(db, "Aiu Developer", ["view_issues", "view_ai_usage", "log_ai_usage"], "own")
    viewer_role = await _role(db, "Aiu Viewer", ["view_issues", "view_ai_usage"], "all")
    none_role = await _role(db, "Aiu Nothing", ["view_issues"], "all")
    runner_role = await _role(
        db, "Aiu Runner", ["view_issues", "view_ai_usage", "log_ai_usage", "log_ai_usage_for_others"], "own"
    )
    await _member(db, p, users["mgr"], mgr_role)
    for name in ("alice", "bob", "eve"):
        await _member(db, p, users[name], dev_role)
    await _member(db, p, users["carol"], viewer_role)
    await _member(db, p, users["dave"], none_role)
    await _member(db, p, users["runner"], runner_role)

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
    await db.flush()

    admin = users["admin"]
    for model, prices, start, end in (
        ("lab-model-1", {"input": "2", "output": "10", "cache_read": "0.2"}, utc(2026, 1, 1), JUNE_1),
        ("lab-model-1", {"input": "4", "output": "20", "cache_read": "0.4"}, JUNE_1, None),
        ("lab-partial", {"input": "1", "output": "5"}, utc(2026, 1, 1), None),
    ):
        await _prices.create(
            db,
            PriceInput(
                provider="testlab", model=model, currency="USD", effective_from=start, effective_to=end, prices=prices
            ),
            actor=admin,
        )
    await _prices.create(
        db,
        PriceInput(
            provider="testlab",
            model="lab-local",
            currency="USD",
            effective_from=utc(2026, 1, 1),
            prices={"input": "0.1", "output": "0.1"},
            is_local=True,
            baseline_model="lab-model-1",
        ),
        actor=admin,
    )
    await db.commit()
    clear_role_cache(db)
    return World(p=p, q=q, public_issue=public_issue, private_issue=private_issue, users=users)


def usage(**kw: Any) -> AiUsageInput:
    values: dict[str, Any] = {
        "tool": "claude-code",
        "provider": "testlab",
        "model": "lab-model-1",
        "usage_date": MARCH,
        "tokens": {"input": 1_000_000, "output": 100_000},
    }
    values.update(kw)
    return AiUsageInput(**values)


async def _events(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value))
    return list(rows.scalars().all())


async def _visible_ids(db: AsyncSession, user: User) -> set[int]:
    clause = await _svc.visible_entries_clause_any(db, user)
    stmt = select(AiUsageEntry.id)
    if clause is not None:
        stmt = stmt.where(clause)
    return set((await db.execute(stmt)).scalars().all())


async def _project_visible_ids(db: AsyncSession, user: User, project: Project) -> set[int]:
    clause = await _svc.visible_entries_clause(db, user, project)
    stmt = select(AiUsageEntry.id).where(AiUsageEntry.project_id == project.id)
    if clause is not None:
        stmt = stmt.where(clause)
    return set((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Costs per cost type
# ---------------------------------------------------------------------------


async def test_api_cost_without_reported_cost_is_the_api_equivalent(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, cost_type="api"))
    # 1M input x 2 + 0.1M output x 10 = 3 USD, from the period valid in March.
    assert entry.api_equivalent == Money("3", "USD")
    assert entry.marginal == Money("3", "USD")
    assert entry.cost_basis == "calculated"
    assert entry.priced_at == utc(2026, 3, 10)
    assert entry.price_id is not None


async def test_reported_cost_wins_for_api_spend(db_session: AsyncSession, w: World) -> None:
    reported = Money("1.2345", "EUR")
    entry = await _svc.log(
        db_session, w.alice, usage(project_id=w.p.id, cost_type="api", reported_cost=reported), source="api"
    )
    assert entry.marginal == reported
    assert entry.reported_cost == reported
    assert entry.api_equivalent == Money("3", "USD")
    assert entry.cost_basis == "reported"

    typed = await _svc.log(
        db_session, w.alice, usage(project_id=w.p.id, cost_type="api", reported_cost=Money("2", "THB"))
    )
    assert typed.cost_basis == "manual"
    assert typed.marginal == Money("2", "THB")


async def test_reported_cost_is_only_stored_for_other_cost_types(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(
        db_session, w.alice, usage(project_id=w.p.id, cost_type="free", reported_cost=Money("9", "USD"))
    )
    assert entry.marginal == Money("0", "USD")
    assert entry.reported_cost == Money("9", "USD")
    assert entry.cost_basis == "calculated"


async def test_cost_type_defaults_to_the_covering_subscription(db_session: AsyncSession, w: World) -> None:
    sub = AiSubscription(
        user_id=w.alice.id,
        provider="anthropic",
        plan_key="max",
        plan_name="Max",
        tool_slugs=["claude-code"],
        amount=Decimal("3500"),
        currency_code="THB",
        billing_period="month",
        starts_on=date(2026, 1, 1),
    )
    db_session.add(sub)
    await db_session.flush()
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id))
    assert entry.cost_type == "subscription"
    assert entry.subscription_id == sub.id
    assert entry.marginal == Money("0", "THB")
    assert entry.api_equivalent == Money("3", "USD")

    # Before the plan started nothing covers the usage: unknown, no marginal cost.
    early = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, usage_date=date(2025, 12, 31)))
    assert early.cost_type == "unknown"
    assert early.subscription_id is None
    assert early.marginal is None


async def test_unknown_cost_type_has_no_marginal_cost(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.bob, usage(project_id=w.p.id))
    assert entry.cost_type == "unknown"
    assert entry.marginal is None
    assert entry.api_equivalent == Money("3", "USD")


async def test_local_model_prices_marginal_with_its_row_and_values_it_at_the_baseline(
    db_session: AsyncSession, w: World
) -> None:
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, model="lab-local", cost_type="local"))
    # Local row: 1.1M tokens x 0.1 = 0.11; the baseline lab-model-1 in March: 3.
    assert entry.marginal == Money("0.11", "USD")
    assert entry.api_equivalent == Money("3", "USD")


async def test_unpriced_model_never_costs_zero(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, model="lab-unknown", cost_type="api"))
    assert entry.cost_basis == "unpriced"
    assert entry.price_id is None
    assert entry.api_equivalent is None
    assert entry.marginal is None


async def test_partial_when_a_class_with_tokens_has_no_price(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(
        db_session,
        w.alice,
        usage(project_id=w.p.id, model="lab-partial", tokens={"input": 1_000_000, "cache_read": 500_000}),
    )
    assert entry.cost_basis == "partial"
    assert entry.api_equivalent == Money("1", "USD")


async def test_priced_at_is_started_at_else_the_utc_day_start(db_session: AsyncSession, w: World) -> None:
    started = datetime(2026, 5, 31, 23, 30, tzinfo=UTC)
    entry = await _svc.log(db_session, w.alice, usage(usage_date=date(2026, 5, 31), started_at=started))
    assert entry.priced_at == started
    # 23:30 on May 31 is still the first period: 3 USD, not 6.
    assert entry.api_equivalent == Money("3", "USD")

    june = await _svc.log(db_session, w.alice, usage(usage_date=date(2026, 6, 1)))
    assert june.priced_at == JUNE_1
    assert june.api_equivalent == Money("6", "USD")


async def test_started_at_must_fall_on_the_utc_bucket_day(db_session: AsyncSession, w: World) -> None:
    from zoneinfo import ZoneInfo

    # 06:00 in Bangkok on March 10 is 23:00 UTC on March 9: another bucket.
    bangkok = datetime(2026, 3, 10, 6, 0, tzinfo=ZoneInfo("Asia/Bangkok"))
    with pytest.raises(AppError) as exc:
        await _svc.log(db_session, w.alice, usage(started_at=bangkok))
    assert exc.value.field == "started_at"
    entry = await _svc.log(db_session, w.alice, usage(usage_date=date(2026, 3, 9), started_at=bangkok))
    assert entry.priced_at == utc(2026, 3, 9, 23, 0)


async def test_token_bounds_are_enforced(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(AppError):
        await _svc.log(db_session, w.alice, usage(tokens={"input": -1}))
    with pytest.raises(AppError):
        await _svc.log(db_session, w.alice, usage(tokens={"prompt_text": 5}))
    with pytest.raises(AppError):
        await _svc.log(db_session, w.alice, usage(usage_date=date(2019, 12, 31)))


# ---------------------------------------------------------------------------
# Price snapshots and the range recompute
# ---------------------------------------------------------------------------


async def test_adding_or_editing_prices_never_changes_existing_entries(db_session: AsyncSession, w: World) -> None:
    priced = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, cost_type="api"))
    unpriced = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, model="lab-later", cost_type="api"))
    before = (priced.price_id, priced.marginal, unpriced.price_id, unpriced.cost_basis)

    await _prices.create(
        db_session,
        PriceInput(
            provider="testlab",
            model="lab-later",
            currency="USD",
            effective_from=utc(2026, 1, 1),
            effective_to=utc(2026, 4, 1),
            prices={"input": "1", "output": "1"},
        ),
        actor=w.admin,
    )
    await _prices.update(db_session, priced.price_id, actor=w.admin, note="Announced late", aliases=["lab-one"])
    await db_session.commit()
    await db_session.refresh(priced)
    await db_session.refresh(unpriced)
    assert (priced.price_id, priced.marginal, unpriced.price_id, unpriced.cost_basis) == before
    assert unpriced.revision == 1


async def test_recompute_prices_each_entry_at_its_own_instant(db_session: AsyncSession, w: World) -> None:
    march = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, model="lab-split", cost_type="api"))
    july = await _svc.log(
        db_session, w.alice, usage(project_id=w.p.id, model="lab-split", cost_type="api", usage_date=JULY)
    )
    other = await _svc.log(db_session, w.bob, usage(project_id=w.p.id, cost_type="api"))
    assert march.cost_basis == july.cost_basis == "unpriced"

    # The price changed on June 1; both periods are entered afterwards.
    for start, end, prices in (
        (utc(2026, 1, 1), JUNE_1, {"input": "1", "output": "10"}),
        (JUNE_1, None, {"input": "3", "output": "30"}),
    ):
        await _prices.create(
            db_session,
            PriceInput(
                provider="testlab",
                model="lab-split",
                currency="USD",
                effective_from=start,
                effective_to=end,
                prices=prices,
            ),
            actor=w.admin,
        )

    assert await _svc.count_in_range(db_session, start=utc(2026, 1, 1), end=utc(2027, 1, 1), model="lab-split") == 2
    outcome = await _svc.recompute_range(
        db_session,
        actor_id=w.admin.id,
        start=utc(2026, 1, 1),
        end=utc(2027, 1, 1),
        provider="testlab",
        model="lab-split",
    )
    assert (outcome.entries, outcome.changed) == (2, 2)
    assert march.api_equivalent == Money("2", "USD")  # 1 + 0.1 x 10
    assert july.api_equivalent == Money("6", "USD")  # 3 + 0.1 x 30
    assert march.price_id != july.price_id
    assert march.revision == july.revision == 2
    assert other.revision == 1  # another model, left alone
    assert outcome.before == {"marginal": {}, "api_equivalent": {}}
    assert outcome.after == {"marginal": {"USD": "8.00000000"}, "api_equivalent": {"USD": "8.00000000"}}

    events = await _events(db_session, AuditEvent.AI_USAGE_COSTS_RECOMPUTED)
    assert len(events) == 1
    details = events[0].details
    assert events[0].user_id == w.admin.id
    assert details["entries"] == 2 and details["changed"] == 2
    assert details["models"] == ["lab-split"]
    assert details["after"]["api_equivalent"] == {"USD": "8.00000000"}

    # Nothing to change the second time: no revision bump.
    again = await _svc.recompute_range(
        db_session, actor_id=w.admin.id, start=utc(2026, 1, 1), end=utc(2027, 1, 1), model="lab-split"
    )
    assert again.changed == 0
    assert march.revision == 2


async def test_recompute_refuses_an_empty_range(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(AppError) as exc:
        await _svc.recompute_range(db_session, actor_id=w.admin.id, start=utc(2026, 2, 1), end=utc(2026, 1, 1))
    assert exc.value.code == "ai_usage_invalid"


async def test_referenced_prices_cannot_be_deleted(db_session: AsyncSession, w: World) -> None:
    assert PRICE_REFERENCE in price_module._REFERENCES
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, model="lab-partial"))
    with pytest.raises(AppError) as exc:
        await _prices.delete(db_session, entry.price_id, actor=w.admin)
    assert exc.value.code == "ai_model_price_in_use"


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def entries(db_session: AsyncSession, w: World) -> dict[str, AiUsageEntry]:
    db = db_session
    key, _raw = await ApiKeyService().create_key(db, user_id=w.alice.id, name="aiu-key", check_scope_access=False)
    found = {
        "alice": await _svc.log(db, w.alice, usage(project_id=w.p.id)),
        "alice_public_issue": await _svc.log(db, w.alice, usage(project_id=w.p.id, issue_id=w.public_issue.id)),
        "bob": await _svc.log(db, w.bob, usage(project_id=w.p.id)),
        "bob_private_issue": await _svc.log(
            db, w.mgr, usage(project_id=w.p.id, issue_id=w.private_issue.id), target=w.bob
        ),
        "alice_unassigned": await _svc.log(db, w.alice, usage()),
        "alice_unassigned_by_key": await _svc.log(db, w.alice, usage(), api_key_id=key.id),
        "runner_for_alice": await _svc.upsert(
            db, w.runner, usage(project_id=w.p.id, idempotency_key="k-1"), target=w.alice
        ),
        "bob_unassigned_by_admin": await _svc.log(db, w.admin, usage(), target=w.bob),
    }
    result = {name: (e.entry if hasattr(e, "entry") else e) for name, e in found.items()}
    result["_key"] = key  # type: ignore[assignment]
    await db.commit()
    clear_role_cache(db)
    return result


def _ids(entries: dict[str, Any], *names: str) -> set[int]:
    return {entries[n].id for n in names}


@pytest.mark.parametrize(
    ("viewer", "expected"),
    [
        # Own entries always, unassigned ones included, and who reported them.
        ("alice", ("alice", "alice_public_issue", "alice_unassigned", "alice_unassigned_by_key", "runner_for_alice")),
        # Own entries even on an issue the reader cannot see.
        ("bob", ("bob", "bob_private_issue", "bob_unassigned_by_admin")),
        # "all" in the project, but not on the hidden issue, and no unassigned entries of others.
        ("carol", ("alice", "alice_public_issue", "bob", "runner_for_alice")),
        # No view_ai_usage and nothing of one's own.
        ("dave", ()),
        ("outsider", ()),
        # A reporter sees what it reported.
        ("runner", ("runner_for_alice",)),
        # Manager: every entry in the project, the private issue included (they can see it).
        ("mgr", ("alice", "alice_public_issue", "bob", "bob_private_issue", "runner_for_alice")),
        (
            "admin",
            (
                "alice",
                "alice_public_issue",
                "bob",
                "bob_private_issue",
                "alice_unassigned",
                "alice_unassigned_by_key",
                "runner_for_alice",
                "bob_unassigned_by_admin",
            ),
        ),
    ],
)
async def test_visibility_matrix(
    db_session: AsyncSession, w: World, entries: dict[str, Any], viewer: str, expected: tuple[str, ...]
) -> None:
    user = w.users[viewer]
    assert await _visible_ids(db_session, user) == _ids(entries, *expected)
    in_project = {entries[n].id for n in expected if entries[n].project_id == w.p.id}
    assert await _project_visible_ids(db_session, user, w.p) == in_project


async def test_dave_with_own_visibility_role_still_sees_his_own(
    db_session: AsyncSession, w: World, entries: dict[str, Any]
) -> None:
    # dave cannot log in AIU, but an entry logged on his behalf is his to see.
    entry = await _svc.log(db_session, w.runner, usage(project_id=w.p.id), target=w.dave)
    assert await _visible_ids(db_session, w.dave) == {entry.id}


async def test_limited_key_sees_unassigned_entries_only_where_it_reported(
    db_session: AsyncSession, w: World, entries: dict[str, Any]
) -> None:
    key = entries["_key"]
    with bound_scope(ProjectScope(frozenset({w.p.id}), key_id=key.id)):
        visible = await _visible_ids(db_session, w.alice)
    assert visible == _ids(entries, "alice", "alice_public_issue", "alice_unassigned_by_key", "runner_for_alice")

    with bound_scope(ProjectScope(frozenset({w.q.id}), key_id=key.id)):
        assert await _visible_ids(db_session, w.alice) == _ids(entries, "alice_unassigned_by_key")
        # An admin behind a limited key is limited too.
        assert await _visible_ids(db_session, w.admin) == _ids(entries, "alice_unassigned_by_key")


async def test_get_visible_answers_404_for_hidden_entries(
    db_session: AsyncSession, w: World, entries: dict[str, Any]
) -> None:
    assert (await _svc.get_visible(db_session, entries["bob"].id, w.carol)).id == entries["bob"].id
    with pytest.raises(NotFoundError):
        await _svc.get_visible(db_session, entries["bob_private_issue"].id, w.carol)
    with pytest.raises(NotFoundError):
        await _svc.get_visible(db_session, entries["alice_unassigned"].id, w.mgr)


async def test_list_entries_filters_and_pages(db_session: AsyncSession, w: World, entries: dict[str, Any]) -> None:
    page, total = await _svc.list_entries(db_session, w.mgr, project=w.p, limit=2)
    assert total == 5 and len(page) == 2
    _page, alice_total = await _svc.list_entries(db_session, w.mgr, project=w.p, user_id=w.alice.id)
    assert alice_total == 3


# ---------------------------------------------------------------------------
# On-behalf authorization
# ---------------------------------------------------------------------------


async def test_on_behalf_allowed_for_an_active_member(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.runner, usage(project_id=w.p.id), target=w.alice, source="api")
    assert (entry.user_id, entry.reporter_user_id, entry.source) == (w.alice.id, w.runner.id, "api")
    events = await _events(db_session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER)
    assert len(events) == 1
    assert events[0].user_id == w.runner.id
    assert events[0].project_id == w.p.id
    assert events[0].details["target_login"] == "aiu_alice"
    assert events[0].details["project_key"] == "AIU"


async def test_logging_for_oneself_is_not_audited(db_session: AsyncSession, w: World) -> None:
    await _svc.log(db_session, w.alice, usage(project_id=w.p.id))
    assert await _events(db_session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER) == []


async def test_on_behalf_refusals(db_session: AsyncSession, w: World) -> None:
    # Without log_ai_usage_for_others.
    with pytest.raises(PermissionDeniedError):
        await _svc.log(db_session, w.alice, usage(project_id=w.p.id), target=w.bob)
    # The target is not a member of the project.
    with pytest.raises(AppError) as exc:
        await _svc.log(db_session, w.runner, usage(project_id=w.p.id), target=w.outsider)
    assert exc.value.code == "ai_usage_target_not_member"
    # A deactivated target.
    with pytest.raises(AppError) as exc:
        await _svc.log(db_session, w.runner, usage(project_id=w.p.id), target=w.eve)
    assert exc.value.code == "ai_usage_target_invalid"
    # Unassigned usage for someone else: administrators only.
    with pytest.raises(PermissionDeniedError):
        await _svc.log(db_session, w.runner, usage(), target=w.alice)
    # A project the caller cannot reach.
    with pytest.raises(NotFoundError):
        await _svc.log(db_session, w.alice, usage(project_id=w.q.id))
    # Own usage without log_ai_usage.
    with pytest.raises(PermissionDeniedError):
        await _svc.log(db_session, w.dave, usage(project_id=w.p.id))
    # An issue of the project the caller cannot see.
    with pytest.raises(NotFoundError):
        await _svc.log(db_session, w.alice, usage(project_id=w.p.id, issue_id=w.private_issue.id))


async def test_own_unassigned_usage_needs_no_permission(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.dave, usage())
    assert entry.project_id is None and entry.attribution_method == "none"


async def test_administrator_logs_unassigned_usage_for_others(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.admin, usage(), target=w.outsider)
    assert entry.user_id == w.outsider.id


# ---------------------------------------------------------------------------
# Upserts, exclusion, locked fields, deletes
# ---------------------------------------------------------------------------


async def test_upsert_replaces_totals_and_keeps_the_exclusion(db_session: AsyncSession, w: World) -> None:
    created = await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="b-1"), target=w.alice)
    assert created.status == "created"
    entry = created.entry
    await _svc.set_excluded(db_session, w.alice, entry, True)
    assert entry.is_excluded

    same = await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="b-1"), target=w.alice)
    assert same.status == "unchanged" and same.entry.revision == 1

    bigger = usage(project_id=w.p.id, idempotency_key="b-1", tokens={"input": 2_000_000, "output": 100_000})
    updated = await _svc.upsert(db_session, w.runner, bigger, target=w.alice)
    assert updated.status == "updated"
    assert updated.entry.id == entry.id
    assert entry.input == 2_000_000
    assert entry.api_equivalent == Money("5", "USD")
    assert entry.revision == 2
    assert entry.is_excluded and entry.excluded_by_id == w.alice.id

    events = await _events(db_session, AuditEvent.AI_USAGE_EXCLUDED)
    assert len(events) == 1 and events[0].details["entry_id"] == entry.id
    await _svc.set_excluded(db_session, w.alice, entry, False)
    assert len(await _events(db_session, AuditEvent.AI_USAGE_INCLUDED)) == 1


async def test_upsert_keeps_an_attribution_once_set(db_session: AsyncSession, w: World) -> None:
    first = await _svc.upsert(db_session, w.alice, usage(idempotency_key="b-2"))
    assert first.entry.project_id is None
    assigned = await _svc.upsert(db_session, w.alice, usage(project_id=w.p.id, idempotency_key="b-2"))
    assert assigned.entry.project_id == w.p.id and assigned.status == "updated"
    await _svc.update(db_session, w.alice, assigned.entry, {"issue_id": w.public_issue.id})
    resent = await _svc.upsert(db_session, w.alice, usage(project_id=w.p.id, idempotency_key="b-2"))
    assert resent.status == "unchanged"
    assert resent.entry.issue_id == w.public_issue.id


async def test_key_of_another_reporter_is_a_conflict(db_session: AsyncSession, w: World) -> None:
    await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="b-3"), target=w.alice)
    with pytest.raises(AppError) as exc:
        await _svc.upsert(db_session, w.alice, usage(project_id=w.p.id, idempotency_key="b-3"))
    assert exc.value.code == "key_conflict"
    # The same key for another user is another bucket.
    other = await _svc.upsert(db_session, w.bob, usage(project_id=w.p.id, idempotency_key="b-3"))
    assert other.status == "created"


async def test_reporter_entries_need_a_key_and_lock_their_usage(db_session: AsyncSession, w: World) -> None:
    with pytest.raises(AppError):
        await _svc.upsert(db_session, w.alice, usage())
    with pytest.raises(AppError):
        await _svc.log(db_session, w.alice, usage(), source="reporter")

    entry = (await _svc.upsert(db_session, w.alice, usage(idempotency_key="b-4"))).entry
    with pytest.raises(AppError) as exc:
        await _svc.update(db_session, w.alice, entry, {"tokens": {"input": 1}})
    assert exc.value.code == "ai_usage_field_locked"
    with pytest.raises(AppError) as exc:
        await _svc.update(db_session, w.alice, entry, {"model": "lab-partial"})
    assert exc.value.code == "ai_usage_field_locked"

    await _svc.update(
        db_session, w.alice, entry, {"project_id": w.p.id, "issue_id": w.public_issue.id, "comments": "Login form"}
    )
    assert (entry.project_id, entry.issue_id, entry.comments) == (w.p.id, w.public_issue.id, "Login form")
    assert entry.attribution_method == "explicit"
    assert entry.revision == 2


async def test_manual_entries_can_change_their_usage_and_are_repriced(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id, cost_type="api"))
    await _svc.update(db_session, w.alice, entry, {"usage_date": JULY})
    assert entry.priced_at == utc(2026, 7, 5)
    assert entry.api_equivalent == Money("6", "USD")
    assert entry.revision == 2


async def test_who_may_edit(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id))
    with pytest.raises(PermissionDeniedError):
        await _svc.update(db_session, w.bob, entry, {"comments": "Not mine"})
    with pytest.raises(PermissionDeniedError):
        await _svc.set_excluded(db_session, w.carol, entry, True)

    await _svc.update(db_session, w.mgr, entry, {"comments": "Reviewed", "issue_id": w.public_issue.id})
    events = await _events(db_session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER)
    assert len(events) == 1
    changes = events[0].details["changes"]
    assert changes["comments"] == {"changed": True}
    assert changes["issue_id"] == {"old": None, "new": w.public_issue.id}
    assert "Reviewed" not in str(events[0].details)

    # The entry's own user editing writes no audit event.
    await _svc.update(db_session, w.alice, entry, {"comments": "Mine"})
    assert len(await _events(db_session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER)) == 1


async def test_moving_an_entry_needs_log_ai_usage_there(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.alice, usage())
    with pytest.raises(NotFoundError):
        await _svc.update(db_session, w.alice, entry, {"project_id": w.q.id})
    dave_entry = await _svc.log(db_session, w.dave, usage())
    with pytest.raises(PermissionDeniedError):
        await _svc.update(db_session, w.dave, dave_entry, {"project_id": w.p.id})
    await _svc.update(db_session, w.alice, entry, {"project_id": w.p.id})
    assert entry.project_id == w.p.id
    # Only administrators make another user's entry unassigned.
    with pytest.raises(PermissionDeniedError):
        await _svc.update(db_session, w.mgr, entry, {"project_id": None})


async def test_reporter_entries_are_excluded_not_deleted(db_session: AsyncSession, w: World) -> None:
    entry = (
        await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="b-5"), target=w.alice)
    ).entry
    with pytest.raises(PermissionDeniedError):
        await _svc.delete(db_session, w.alice, entry)
    with pytest.raises(PermissionDeniedError):
        await _svc.delete(db_session, w.mgr, entry)
    entry_id = entry.id
    await _svc.delete(db_session, w.admin, entry)
    assert await db_session.get(AiUsageEntry, entry_id) is None
    events = await _events(db_session, AuditEvent.AI_USAGE_DELETED)
    assert len(events) == 1 and events[0].resource_id == entry_id
    assert events[0].details["tokens"]["input"] == 1_000_000


async def test_manual_entries_are_deleted_by_their_user(db_session: AsyncSession, w: World) -> None:
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id))
    with pytest.raises(PermissionDeniedError):
        await _svc.delete(db_session, w.bob, entry)
    await _svc.delete(db_session, w.alice, entry)
    assert len(await _events(db_session, AuditEvent.AI_USAGE_DELETED)) == 1


async def test_unknown_tool_is_added_inactive(db_session: AsyncSession, w: World) -> None:
    from specivo.models.ai_subscription import AiTool

    entry = await _svc.log(db_session, w.alice, usage(tool="lab-cli"))
    tool = await db_session.get(AiTool, "lab-cli")
    assert entry.tool_slug == "lab-cli" and tool is not None and not tool.is_active


# ---------------------------------------------------------------------------
# Regression: replays and moves are authorized against the stored entry and the destination
# ---------------------------------------------------------------------------


async def _third_project(db: AsyncSession, w: World, roles: dict[str, list[str]]) -> Project:
    """Project ``AIW``, where each named user holds a role granting the given permissions."""
    project = ProjectFactory.build(key="AIW", identifier="aiw", is_public=False)
    db.add(project)
    await db.flush()
    for name, permissions in roles.items():
        role = await _role(db, f"Aiw {name}", permissions, "own")
        await _member(db, project, w.users[name], role)
    await db.commit()
    clear_role_cache(db)
    return project


async def test_replay_cannot_move_or_rewrite_an_entry_in_another_project(db_session: AsyncSession, w: World) -> None:
    other = await _third_project(
        db_session,
        w,
        {
            "runner": ["view_issues", "log_ai_usage", "log_ai_usage_for_others"],
            "alice": ["view_issues", "log_ai_usage"],
        },
    )
    entry = (
        await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="r-1"), target=w.alice)
    ).entry

    # Naming a project where the runner may log does not move the stored entry.
    with pytest.raises(AppError) as exc:
        await _svc.upsert(
            db_session,
            w.runner,
            usage(project_id=other.id, idempotency_key="r-1", tokens={"input": 5}),
            target=w.alice,
        )
    assert exc.value.code == "ai_usage_attribution_conflict"
    assert entry.project_id == w.p.id and entry.input == 1_000_000

    # Once the runner lost its rights in the entry's project, no replay rewrites it,
    # with or without naming a project.
    member_ids = select(Member.id).where(Member.project_id == w.p.id, Member.user_id == w.runner.id)
    await db_session.execute(MemberRole.__table__.delete().where(MemberRole.member_id.in_(member_ids)))
    await db_session.commit()
    clear_role_cache(db_session)
    for project_id in (None, w.p.id):
        with pytest.raises(PermissionDeniedError):
            await _svc.upsert(
                db_session,
                w.runner,
                usage(project_id=project_id, idempotency_key="r-1", tokens={"input": 5}),
                target=w.alice,
            )
    await db_session.refresh(entry)
    assert entry.input == 1_000_000 and entry.revision == 1


async def test_replay_assigning_an_unassigned_entry_is_checked_in_that_project(
    db_session: AsyncSession, w: World
) -> None:
    entry = (await _svc.upsert(db_session, w.dave, usage(idempotency_key="r-2"))).entry
    # dave holds no log_ai_usage in AIU: the replay cannot attribute his entry there.
    with pytest.raises(PermissionDeniedError):
        await _svc.upsert(db_session, w.dave, usage(project_id=w.p.id, idempotency_key="r-2"))
    assert entry.project_id is None


async def test_replay_by_another_account_is_audited(db_session: AsyncSession, w: World) -> None:
    await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="r-3"), target=w.alice)
    await _svc.upsert(db_session, w.runner, usage(project_id=w.p.id, idempotency_key="r-3"), target=w.alice)
    assert len(await _events(db_session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER)) == 1  # unchanged: no event
    await _svc.upsert(
        db_session,
        w.runner,
        usage(project_id=w.p.id, idempotency_key="r-3", tokens={"input": 7}),
        target=w.alice,
    )
    events = await _events(db_session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER)
    assert len(events) == 2
    replaced = next(e for e in events if e.details.get("status") == "updated")
    assert replaced.details["revision"] == 2 and replaced.details["target_login"] == "aiu_alice"


async def test_moving_someone_elses_entry_needs_log_for_others_in_the_destination(
    db_session: AsyncSession, w: World
) -> None:
    other = await _third_project(
        db_session,
        w,
        {"mgr": ["view_issues", "log_ai_usage", "manage_ai_usage"], "alice": ["view_issues", "log_ai_usage"]},
    )
    entry = await _svc.log(db_session, w.alice, usage(project_id=w.p.id))
    with pytest.raises(PermissionDeniedError):
        await _svc.update(db_session, w.mgr, entry, {"project_id": other.id})
    assert entry.project_id == w.p.id

    role = await _role(db_session, "Aiw Lead", ["log_ai_usage_for_others"], "own")
    member = (
        await db_session.execute(select(Member).where(Member.project_id == other.id, Member.user_id == w.mgr.id))
    ).scalar_one()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    clear_role_cache(db_session)
    await _svc.update(db_session, w.mgr, entry, {"project_id": other.id})
    assert entry.project_id == other.id
