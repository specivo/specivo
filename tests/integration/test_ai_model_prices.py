"""AI model prices: the price history, its constraints, resolution, the seed loader, the API and the admin pages.

The test database already holds the bundled seed (migration 0061). Tests that
need their own models use the providers ``testprov`` and ``seedtest`` (never in
the bundled snapshot) or ``spv-test-`` model names, so they never collide with
the bundled rows. Nothing reaches the network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import TokenCounts, price_usage
from specivo.core.ai_pricing_seed import (
    PRICE_COLUMNS,
    HistoryPeriod,
    SeedPrice,
    Snapshot,
    load_snapshot,
)
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError
from specivo.core.money import Money
from specivo.models.ai_model_price import AiModelPrice
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_model_price_service import (
    AiModelPriceService,
    PriceInput,
    PriceReference,
    register_price_reference,
    unregister_price_reference,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.security_audit_service import AuditEvent
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = AiModelPriceService()
BANGKOK = ZoneInfo("Asia/Bangkok")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
T2 = datetime(2026, 9, 1, tzinfo=UTC)
TICK = timedelta(microseconds=1)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mp_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def regular(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="mp_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _csrf(client: AsyncClient, user: User) -> str:
    resp = await client.get("/admin/ai-model-prices/", cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError("no csrf_token cookie on the AI model prices page")


async def _post_form(client: AsyncClient, user: User, url: str, data: dict[str, str]):
    csrf = await _csrf(client, user)
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


async def _add(
    db: AsyncSession,
    *,
    model: str = "alpha-1",
    provider: str = "testprov",
    start: datetime = T0,
    end: datetime | None = None,
    source: str = "seed",
    input: str = "3",
    output: str = "15",
    currency: str = "USD",
    aliases: list[str] | None = None,
    active: bool = True,
    **prices: str,
) -> AiModelPrice:
    row = AiModelPrice(
        provider=provider,
        model=model,
        aliases=aliases or [],
        currency_code=currency,
        input=Decimal(input),
        output=Decimal(output),
        effective_from=start,
        effective_to=end,
        source=source,
        is_active=active,
        **{k: Decimal(v) for k, v in prices.items()},
    )
    db.add(row)
    await db.flush()
    return row


async def _refused(db: AsyncSession, action: Callable[[], Awaitable[object]], match: str | None = None) -> None:
    """Run *action* in a savepoint and expect the database to refuse it; the session stays usable."""
    savepoint = await db.begin_nested()
    with pytest.raises(IntegrityError, match=match):
        await action()
    await savepoint.rollback()


async def _audit_rows(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event).order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


def _input(**overrides: object) -> PriceInput:
    values: dict = {
        "provider": "testprov",
        "model": "alpha-1",
        "currency": "USD",
        "effective_from": T1,
        "prices": {"input": "3", "output": "15"},
    }
    values.update(overrides)
    return PriceInput(**values)


# ---------------------------------------------------------------------------
# Resolution at an instant
# ---------------------------------------------------------------------------


async def test_resolution_around_a_price_change_instant(db_session: AsyncSession) -> None:
    old = await _add(db_session, start=T0, end=T1, input="3")
    new = await _add(db_session, start=T1, input="2.5")

    async def at(instant: datetime) -> int | None:
        resolved = await _service.resolve(db_session, "alpha-1", instant, provider="testprov")
        return resolved.price.id if resolved else None

    assert await at(T1) == new.id  # the start is inside its own period
    assert await at(T1 - TICK) == old.id
    assert await at(T1 + TICK) == new.id
    # The same instants given in Asia/Bangkok (UTC+7) resolve identically.
    bangkok_change = T1.astimezone(BANGKOK)
    assert bangkok_change.hour == 7
    assert await at(bangkok_change) == new.id
    assert await at(bangkok_change - timedelta(seconds=1)) == old.id
    resolved = await _service.resolve(db_session, "alpha-1", bangkok_change, provider="testprov")
    assert resolved is not None and resolved.at == T1 and resolved.at.tzinfo == UTC


async def test_an_instant_before_the_first_period_is_unpriced(db_session: AsyncSession) -> None:
    await _add(db_session, start=T1, input="2.5")
    assert await _service.resolve(db_session, "alpha-1", T1 - TICK, provider="testprov") is None
    assert await _service.resolve(db_session, "alpha-1", T0, provider="testprov") is None


async def test_an_instant_after_a_closed_last_period_is_unpriced(db_session: AsyncSession) -> None:
    await _add(db_session, start=T0, end=T1)
    assert await _service.resolve(db_session, "alpha-1", T1, provider="testprov") is None


async def test_admin_wins_over_seed_where_periods_overlap(db_session: AsyncSession) -> None:
    seed = await _add(db_session, start=T0, source="seed", input="3")
    admin_row = await _add(db_session, start=T1, end=T2, source="admin", input="2")

    async def at(instant: datetime) -> int | None:
        resolved = await _service.resolve(db_session, "alpha-1", instant, provider="testprov")
        return resolved.price.id if resolved else None

    assert await at(T0) == seed.id
    assert await at(T1) == admin_row.id
    assert await at(T2 - TICK) == admin_row.id
    assert await at(T2) == seed.id


async def test_inactive_rows_are_never_used(db_session: AsyncSession) -> None:
    await _add(db_session, start=T0, active=False)
    assert await _service.resolve(db_session, "alpha-1", T1, provider="testprov") is None


async def test_names_are_normalised_and_aliases_match_after_the_model(db_session: AsyncSession) -> None:
    exact = await _add(db_session, provider="anthropic", model="spv-test-claude", start=T0)
    dated = await _add(
        db_session,
        provider="anthropic",
        model="spv-test-claude-20260101",
        aliases=["spv-test-claude-latest", "spv-test-claude"],
        start=T0,
        source="admin",
    )

    by_prefix = await _service.resolve(db_session, " Anthropic/SPV-Test-Claude ", T1)
    assert by_prefix is not None and by_prefix.price.id == exact.id and by_prefix.matched_by == "model"
    # An admin row whose alias equals the name loses to the exact model name.
    by_alias = await _service.resolve(db_session, "spv-test-claude-latest", T1)
    assert by_alias is not None and by_alias.price.id == dated.id and by_alias.matched_by == "alias"
    # An explicit provider restricts the match.
    assert await _service.resolve(db_session, "spv-test-claude", T1, provider="openai") is None
    assert await _service.resolve(db_session, "spv-test-claude", T1, provider="Anthropic") is not None


async def test_a_naive_instant_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(AppError):
        await _service.resolve(db_session, "alpha-1", datetime(2026, 6, 1))  # noqa: DTZ001


async def test_the_bundled_seed_is_valid_from_its_as_of(db_session: AsyncSession) -> None:
    snapshot = load_snapshot()
    sample = next(p for p in snapshot.prices if p.provider == "anthropic")
    priced = await _service.resolve(db_session, f"anthropic/{sample.model}", snapshot.as_of + timedelta(hours=1))
    assert priced is not None and priced.price.source == "seed"
    assert priced.price.effective_from <= snapshot.as_of
    # Usage before the first bundled period is unpriced, never the latest price.
    first = (
        await db_session.execute(
            select(func.min(AiModelPrice.effective_from)).where(
                AiModelPrice.provider == "anthropic", AiModelPrice.model == sample.model
            )
        )
    ).scalar_one()
    assert await _service.resolve(db_session, sample.model, first - TICK, provider="anthropic") is None


async def test_a_resolved_row_prices_usage(db_session: AsyncSession) -> None:
    await _add(db_session, start=T0, cache_write_5m="3.75", cache_write_1h="6", cache_read="0.3")
    resolved = await _service.resolve(db_session, "alpha-1", T1, provider="testprov")
    assert resolved is not None
    cost = price_usage(TokenCounts(input=1_000_000, cache_read=1_000_000), None, resolved.price.to_price())
    assert cost.money == Money("3.3", "USD")


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


async def test_the_exclusion_constraint_rejects_overlapping_periods_of_one_source(db_session: AsyncSession) -> None:
    await _add(db_session, start=T0, end=T2)
    await _refused(db_session, lambda: _add(db_session, start=T1), "ex_ai_model_prices_no_overlap")
    # Adjacent periods do not overlap ([from, to) ranges), other sources and inactive rows may.
    await _add(db_session, start=T2)
    await _add(db_session, start=T1, source="admin")
    await _add(db_session, start=T1, active=False)


async def test_prices_and_the_start_cannot_be_updated_in_place(db_session: AsyncSession) -> None:
    row = await _add(db_session, start=T0)
    for values in ({"input": Decimal("1")}, {"effective_from": T1}, {"currency_code": "EUR"}, {"model": "beta-1"}):
        statement = update(AiModelPrice).where(AiModelPrice.id == row.id).values(**values)
        await _refused(db_session, lambda s=statement: db_session.execute(s), "never edited in place")
    await db_session.execute(
        update(AiModelPrice)
        .where(AiModelPrice.id == row.id)
        .values(effective_to=T2, is_active=False, note="ended", aliases=["alpha"])
    )


async def test_a_referenced_price_cannot_be_deleted_or_ended_before_its_entries(
    db_session: AsyncSession, admin: User
) -> None:
    # Usage entries do not exist yet; a stand-in table references prices the way they will.
    # It is created inside the test transaction, so the rollback removes it.
    await db_session.execute(
        text(
            "CREATE TABLE spv_test_priced_usage ("
            "id serial PRIMARY KEY, price_id integer NOT NULL REFERENCES ai_model_prices(id) ON DELETE RESTRICT, "
            "priced_at timestamptz NOT NULL)"
        )
    )
    reference = PriceReference(table="spv_test_priced_usage")
    register_price_reference(reference)
    try:
        row = await _add(db_session, source="admin", start=T0)
        await db_session.execute(
            text("INSERT INTO spv_test_priced_usage (price_id, priced_at) VALUES (:id, :at)"),
            {"id": row.id, "at": T1},
        )

        with pytest.raises(AppError) as deleted:
            await _service.delete(db_session, row.id, actor=admin)
        assert deleted.value.code == "ai_model_price_in_use"
        await _refused(
            db_session, lambda: db_session.execute(text("DELETE FROM ai_model_prices WHERE id = :id"), {"id": row.id})
        )

        for end in (T1, T1 - TICK):
            with pytest.raises(AppError) as ended:
                await _service.update(db_session, row.id, actor=admin, effective_to=end)
            assert ended.value.code == "ai_model_price_in_use"
        # A new price from an instant at or before the entry would close it too early.
        with pytest.raises(AppError) as superseded:
            await _service.create(db_session, _input(effective_from=T1), actor=admin)
        assert superseded.value.code == "ai_model_price_in_use"

        # Amounts are never editable; ending after the entry and deactivating are fine.
        await _refused(
            db_session,
            lambda: db_session.execute(
                update(AiModelPrice).where(AiModelPrice.id == row.id).values(output=Decimal("1"))
            ),
            "never edited in place",
        )
        await _service.update(db_session, row.id, actor=admin, effective_to=T1 + TICK)
        await _service.update(db_session, row.id, actor=admin, is_active=False)
        await db_session.refresh(row)
        assert row.effective_to == T1 + TICK and not row.is_active
    finally:
        unregister_price_reference(reference)


# ---------------------------------------------------------------------------
# Administrator changes (service)
# ---------------------------------------------------------------------------


async def test_a_new_price_from_an_instant_closes_the_open_admin_period(db_session: AsyncSession, admin: User) -> None:
    first = await _service.create(db_session, _input(effective_from=T0), actor=admin)
    second = await _service.create(
        db_session, _input(effective_from=T1, prices={"input": "2.5", "output": "12"}), actor=admin
    )

    assert second.closed is not None and second.closed.id == first.price.id
    await db_session.refresh(first.price)
    assert first.price.effective_to == T1
    assert first.price.superseded_by_id == second.price.id
    assert first.price.input == Decimal("3")  # never rewritten
    assert second.price.effective_to is None and second.price.created_by_id == admin.id
    audit = await _audit_rows(db_session, AuditEvent.AI_MODEL_PRICE_CREATED)
    assert [r.resource_id for r in audit] == [first.price.id, second.price.id]
    assert audit[1].details["closed_price_id"] == first.price.id
    assert audit[1].details["prices"] == {"input": "2.5", "output": "12"}
    assert audit[1].details["effective_from"] == T1.isoformat()


async def test_past_periods_can_be_entered_but_not_over_another(db_session: AsyncSession, admin: User) -> None:
    await _service.create(db_session, _input(effective_from=T1), actor=admin)
    past = await _service.create(
        db_session, _input(effective_from=T0, effective_to=T1, prices={"input": "4", "output": "20"}), actor=admin
    )
    assert past.closed is None
    resolved = await _service.resolve(db_session, "alpha-1", T0 + timedelta(days=1), provider="testprov")
    assert resolved is not None and resolved.price.id == past.price.id

    for period in (
        {"effective_from": T0 + timedelta(days=10), "effective_to": T1 + TICK},  # reaches into the open period
        {"effective_from": T0 - timedelta(days=1), "effective_to": T0 + TICK},  # overlaps the past one
        {"effective_from": T1},  # the same start as the open period
    ):
        with pytest.raises(AppError) as overlap:
            await _service.create(db_session, _input(**period), actor=admin)
        assert overlap.value.code == "ai_model_price_overlap"


@pytest.mark.parametrize("currency", ["CNY", "EUR", "THB"])
async def test_prices_in_any_active_currency(db_session: AsyncSession, admin: User, currency: str) -> None:
    created = await _service.create(
        db_session,
        _input(
            model=f"spv-test-{currency.lower()}",
            currency=currency.lower(),
            prices={"input": "2", "output": "8", "cache_read": "0.5"},
        ),
        actor=admin,
    )
    assert created.price.currency_code == currency
    cost = price_usage(TokenCounts(input=500_000, output=250_000), None, created.price.to_price())
    assert cost.money == Money("3", currency)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"currency": "XAU"}, "currency_inactive"),
        ({"currency": "ZZZ"}, "currency_unknown"),
        ({"prices": {"input": "3"}}, "ai_model_price_invalid"),
        ({"prices": {"input": "3", "output": "-1"}}, "ai_model_price_invalid"),
        ({"prices": {"input": "3", "output": "0.0000001"}}, "ai_model_price_invalid"),
        ({"prices": {"input": "3", "output": "15", "long_input": "6"}}, "ai_model_price_invalid"),
        ({"prices": {"input": "3", "output": "15", "audio": "1"}}, "ai_model_price_invalid"),
        ({"effective_from": datetime(2026, 6, 1)}, "ai_model_price_invalid"),  # noqa: DTZ001
        ({"effective_to": T0}, "ai_model_price_invalid"),
        ({"provider": "Not A Slug!"}, "ai_model_price_invalid"),
        ({"model": "   "}, "ai_model_price_invalid"),
    ],
)
async def test_invalid_prices_are_refused(db_session: AsyncSession, admin: User, overrides: dict, code: str) -> None:
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _input(**overrides), actor=admin)
    assert exc.value.code == code


async def test_seed_prices_are_deactivated_not_deleted(db_session: AsyncSession, admin: User) -> None:
    row = await _add(db_session, start=T0)
    with pytest.raises(AppError) as exc:
        await _service.delete(db_session, row.id, actor=admin)
    assert exc.value.code == "ai_model_price_not_deletable"
    await _service.update(db_session, row.id, actor=admin, is_active=False)
    assert (await _audit_rows(db_session, AuditEvent.AI_MODEL_PRICE_UPDATED))[0].details["changes"] == {
        "is_active": {"old": True, "new": False}
    }


async def test_reactivating_an_overlapping_row_is_refused(db_session: AsyncSession, admin: User) -> None:
    off = await _add(db_session, start=T0, active=False)
    await _add(db_session, start=T1)
    with pytest.raises(AppError) as exc:
        await _service.update(db_session, off.id, actor=admin, is_active=True)
    assert exc.value.code == "ai_model_price_overlap"
    # The request's transaction stays usable after the refused change.
    assert await _service.resolve(db_session, "alpha-1", T1, provider="testprov") is not None


# ---------------------------------------------------------------------------
# Seed loader against the database
# ---------------------------------------------------------------------------


def _seed_price(model: str = "gamma-1", **prices: str) -> SeedPrice:
    values = {"input": "1", "output": "5", "cache_write_5m": "1.25", **prices}
    return SeedPrice(
        provider="seedtest",
        model=model,
        currency="USD",
        prices={c: (Decimal(values[c]) if c in values else None) for c in PRICE_COLUMNS},
    )


def _snap(*prices: SeedPrice, as_of: datetime = T1) -> Snapshot:
    return Snapshot(as_of=as_of, source={}, prices=tuple(prices))


async def _seed_rows(db: AsyncSession, model: str = "gamma-1") -> list[AiModelPrice]:
    db.expire_all()
    result = await db.execute(
        select(AiModelPrice)
        .where(AiModelPrice.provider == "seedtest", AiModelPrice.model == model)
        .order_by(AiModelPrice.effective_from, AiModelPrice.source)
    )
    return list(result.scalars().all())


async def test_a_first_load_inserts_from_as_of_and_a_rerun_is_a_no_op(db_session: AsyncSession, admin: User) -> None:
    first = await _service.load_seed(db_session, actor=admin, snapshot=_snap(_seed_price()), history=())
    assert (first.inserted, first.closed, first.changed) == (1, 0, True)
    [row] = await _seed_rows(db_session)
    assert (row.source, row.effective_from, row.effective_to, row.is_active) == ("seed", T1, None, True)
    assert row.cache_write_1h == Decimal("2")  # missing 1h cache write: 2x input
    assert row.created_by_id is None

    again = await _service.load_seed(db_session, actor=admin, snapshot=_snap(_seed_price()), history=())
    assert (again.inserted, again.closed, again.unchanged, again.changed) == (0, 0, 1, False)
    assert len(await _seed_rows(db_session)) == 1
    audit = await _audit_rows(db_session, AuditEvent.AI_MODEL_PRICES_SEEDED)
    assert len(audit) == 1 and audit[0].details["inserted"] == 1


async def test_a_changed_price_closes_and_opens_periods(db_session: AsyncSession) -> None:
    await _service.load_seed(db_session, snapshot=_snap(_seed_price(), as_of=T0), history=())
    outcome = await _service.load_seed(db_session, snapshot=_snap(_seed_price(input="0.8"), as_of=T1), history=())
    assert (outcome.inserted, outcome.closed) == (1, 1)
    old, new = await _seed_rows(db_session)
    assert (old.effective_from, old.effective_to, old.input) == (T0, T1, Decimal("1"))
    assert (new.effective_from, new.effective_to, new.input) == (T1, None, Decimal("0.8"))
    assert old.superseded_by_id == new.id
    assert (await _service.resolve(db_session, "gamma-1", T1 - TICK, provider="seedtest")).price.id == old.id  # type: ignore[union-attr]
    assert (await _service.resolve(db_session, "gamma-1", T1, provider="seedtest")).price.id == new.id  # type: ignore[union-attr]


async def test_the_seed_never_touches_admin_rows(db_session: AsyncSession, admin: User) -> None:
    await _service.load_seed(db_session, snapshot=_snap(_seed_price(), as_of=T0), history=())
    mine = await _service.create(
        db_session,
        _input(provider="seedtest", model="gamma-1", effective_from=T0, prices={"input": "0.5", "output": "2"}),
        actor=admin,
    )
    await _service.load_seed(db_session, snapshot=_snap(_seed_price(input="0.9"), as_of=T2), history=())

    rows = await _seed_rows(db_session)
    admin_rows = [r for r in rows if r.source == "admin"]
    assert [(r.id, r.effective_from, r.effective_to, r.input) for r in admin_rows] == [
        (mine.price.id, T0, None, Decimal("0.5"))
    ]
    assert len([r for r in rows if r.source == "seed"]) == 2
    resolved = await _service.resolve(db_session, "gamma-1", T2, provider="seedtest")
    assert resolved is not None and resolved.price.id == mine.price.id


async def test_history_periods_are_loaded_once(db_session: AsyncSession) -> None:
    history = (HistoryPeriod(price=_seed_price(input="2"), effective_from=T0, effective_to=T1),)
    first = await _service.load_seed(db_session, snapshot=_snap(_seed_price()), history=history)
    assert (first.inserted, first.history_inserted) == (1, 1)
    again = await _service.load_seed(db_session, snapshot=_snap(_seed_price()), history=history)
    assert not again.changed
    rows = await _seed_rows(db_session)
    assert [(r.effective_from, r.effective_to, r.input) for r in rows] == [
        (T0, T1, Decimal("2")),
        (T1, None, Decimal("1")),
    ]
    resolved = await _service.resolve(db_session, "gamma-1", T0 + timedelta(days=3), provider="seedtest")
    assert resolved is not None and resolved.price.input == Decimal("2")


async def test_loading_the_bundled_snapshot_again_changes_nothing(db_session: AsyncSession) -> None:
    outcome = await _service.load_seed(db_session)
    assert not outcome.changed
    assert outcome.unchanged >= len(load_snapshot().prices)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


async def test_admin_crud_through_the_api(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    h = _bearer(admin)
    body = {
        "provider": "TestProv",
        "model": "testprov/Alpha-1",
        "currency": "EUR",
        "prices": {"input": "2.80", "output": "14", "cache_read": "0.28"},
        "effective_from": "2026-06-01T07:00:00+07:00",
        "aliases": ["alpha-latest"],
        "note": "Price list of June",
    }
    created = await client.post("/api/v1/admin/ai-model-prices/", json=body, headers=h)
    assert created.status_code == 201, created.text
    price = created.json()
    assert price["provider"] == "testprov" and price["model"] == "alpha-1" and price["currency"] == "EUR"
    assert price["prices"]["input"] == "2.8" and price["prices"]["reasoning"] is None
    assert price["effective_from"].startswith("2026-06-01T00:00:00") and price["source"] == "admin"
    assert price["closed_price_id"] is None

    changed = await client.post(
        "/api/v1/admin/ai-model-prices/",
        json={**body, "prices": {"input": "2.5", "output": "12"}, "effective_from": "2026-09-01T00:00:00Z"},
        headers=h,
    )
    assert changed.status_code == 201 and changed.json()["closed_price_id"] == price["id"]

    listed = await client.get("/api/v1/admin/ai-model-prices/?provider=testprov&q=alpha", headers=h)
    assert listed.json()["total_count"] == 2
    now_only = await client.get(
        "/api/v1/admin/ai-model-prices/?provider=testprov&valid_at=2026-07-01T00:00:00Z", headers=h
    )
    assert [p["id"] for p in now_only.json()["items"]] == [price["id"]]
    by_alias = await client.get("/api/v1/admin/ai-model-prices/?q=alpha-latest", headers=h)
    assert by_alias.json()["total_count"] == 2
    history = await client.get("/api/v1/admin/ai-model-prices/history/?provider=testprov&model=alpha-1", headers=h)
    assert [p["id"] for p in history.json()] == [changed.json()["id"], price["id"]]
    assert "testprov" in (await client.get("/api/v1/admin/ai-model-prices/providers/", headers=h)).json()

    patched = await client.patch(
        f"/api/v1/admin/ai-model-prices/{changed.json()['id']}/",
        json={"note": "Cut", "aliases": ["alpha-cheap"], "is_active": False},
        headers=h,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["note"] == "Cut" and patched.json()["aliases"] == ["alpha-cheap"]
    assert patched.json()["is_active"] is False
    refused = await client.patch(
        f"/api/v1/admin/ai-model-prices/{price['id']}/", json={"prices": {"input": "1"}}, headers=h
    )
    assert refused.status_code == 422

    deleted = await client.delete(f"/api/v1/admin/ai-model-prices/{changed.json()['id']}/", headers=h)
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/admin/ai-model-prices/{changed.json()['id']}/", headers=h)
    assert gone.status_code == 404

    assert len(await _audit_rows(db_session, AuditEvent.AI_MODEL_PRICE_CREATED)) == 2
    updates = await _audit_rows(db_session, AuditEvent.AI_MODEL_PRICE_UPDATED)
    assert set(updates[0].details["changes"]) == {"note", "aliases", "is_active"}
    deletes = await _audit_rows(db_session, AuditEvent.AI_MODEL_PRICE_DELETED)
    assert [d.resource_id for d in deletes] == [changed.json()["id"]]


async def test_api_errors(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    h = _bearer(admin)
    base = {
        "provider": "testprov",
        "model": "alpha-1",
        "currency": "USD",
        "prices": {"input": "3", "output": "15"},
        "effective_from": "2026-06-01T00:00:00Z",
    }
    naive = await client.post(
        "/api/v1/admin/ai-model-prices/", json={**base, "effective_from": "2026-06-01T00:00:00"}, headers=h
    )
    assert naive.status_code == 422
    floats = await client.post(
        "/api/v1/admin/ai-model-prices/", json={**base, "prices": {"input": 3.5, "output": "15"}}, headers=h
    )
    assert floats.status_code == 422
    assert (await client.post("/api/v1/admin/ai-model-prices/", json=base, headers=h)).status_code == 201
    overlap = await client.post(
        "/api/v1/admin/ai-model-prices/", json={**base, "effective_to": "2026-07-01T00:00:00Z"}, headers=h
    )
    assert overlap.status_code == 409 and overlap.json()["errors"][0]["code"] == "ai_model_price_overlap"
    seed = await _add(db_session, model="spv-test-seed", start=T0)
    await db_session.commit()
    not_deletable = await client.delete(f"/api/v1/admin/ai-model-prices/{seed.id}/", headers=h)
    assert not_deletable.status_code == 422


async def test_resolve_endpoint(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, start=T1)
    await db_session.commit()
    h = _bearer(admin)
    priced = await client.get(
        "/api/v1/admin/ai-model-prices/resolve/",
        params={"model": "Alpha-1", "provider": "testprov", "at": "2026-06-01T07:00:00+07:00"},
        headers=h,
    )
    assert priced.json()["status"] == "priced" and priced.json()["matched_by"] == "model"
    assert priced.json()["price"]["prices"]["input"] == "3"
    unpriced = await client.get(
        "/api/v1/admin/ai-model-prices/resolve/",
        params={"model": "alpha-1", "provider": "testprov", "at": "2026-06-01T06:59:59+07:00"},
        headers=h,
    )
    assert unpriced.json() == {
        "status": "unpriced",
        "model": "alpha-1",
        "at": "2026-05-31T23:59:59Z",
        "matched_by": None,
        "price": None,
    }


async def test_seed_refresh_through_the_api_keeps_admin_rows(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    sample = load_snapshot().prices[0]
    h = _bearer(admin)
    mine = await client.post(
        "/api/v1/admin/ai-model-prices/",
        json={
            "provider": sample.provider,
            "model": sample.model,
            "currency": "THB",
            "prices": {"input": "100", "output": "500"},
            "effective_from": "2026-01-01T00:00:00Z",
        },
        headers=h,
    )
    assert mine.status_code == 201, mine.text
    seeded = await client.post("/api/v1/admin/ai-model-prices/seed/", headers=h)
    assert seeded.status_code == 200
    assert seeded.json()["inserted"] == 0 and seeded.json()["closed"] == 0
    after = await client.get(f"/api/v1/admin/ai-model-prices/{mine.json()['id']}/", headers=h)
    expected = {k: v for k, v in mine.json().items() if k != "closed_price_id"}
    assert after.json() == expected
    assert after.json()["prices"] == mine.json()["prices"] and after.json()["effective_to"] is None


async def test_non_admins_are_refused(client: AsyncClient, db_session: AsyncSession, regular: User) -> None:
    h = _bearer(regular)
    row = await _add(db_session, start=T0)
    await db_session.commit()
    body = {
        "provider": "testprov",
        "model": "alpha-1",
        "currency": "USD",
        "prices": {"input": "3", "output": "15"},
        "effective_from": "2026-06-01T00:00:00Z",
    }
    calls = [
        client.get("/api/v1/admin/ai-model-prices/", headers=h),
        client.post("/api/v1/admin/ai-model-prices/", json=body, headers=h),
        client.get("/api/v1/admin/ai-model-prices/resolve/?model=alpha-1", headers=h),
        client.get("/api/v1/admin/ai-model-prices/history/?provider=testprov&model=alpha-1", headers=h),
        client.get("/api/v1/admin/ai-model-prices/providers/", headers=h),
        client.post("/api/v1/admin/ai-model-prices/seed/", headers=h),
        client.patch(f"/api/v1/admin/ai-model-prices/{row.id}/", json={"note": "x"}, headers=h),
        client.delete(f"/api/v1/admin/ai-model-prices/{row.id}/", headers=h),
    ]
    for call in calls:
        resp = await call
        assert resp.status_code == 403, resp.text
    assert (await client.get("/api/v1/admin/ai-model-prices/", headers=h)).status_code == 403
    assert (await client.get("/api/v1/admin/ai-model-prices/")).status_code == 401
    assert (await client.get("/admin/ai-model-prices/", cookies=_cookies(regular))).status_code == 403
    await db_session.refresh(row)
    assert row.note is None and row.is_active


# ---------------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------------


async def test_the_price_list_page(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, model="spv-test-listed", start=T0, cache_read="0.3")
    await db_session.commit()
    resp = await client.get("/admin/ai-model-prices/?provider=testprov", cookies=_cookies(admin))
    assert resp.status_code == 200
    assert "spv-test-listed" in resp.text and 'data-testid="litellm-attribution"' in resp.text
    assert "Berri AI" in resp.text
    dashboard = await client.get("/admin/", cookies=_cookies(admin))
    assert "/admin/ai-model-prices/" in dashboard.text


async def test_entering_and_managing_prices_on_the_model_page(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    created = await _post_form(
        client,
        admin,
        "/admin/ai-model-prices/",
        {
            "provider": "testprov",
            "model": "spv-test-web",
            "currency": "CNY",
            "effective_from": "2026-01-01T00:00",
            "input": "2",
            "output": "8",
            "long_context_threshold": "128000",
            "long_input": "4",
        },
    )
    assert created.status_code == 303, created.text
    assert "notice=created" in created.headers["location"]
    page = await client.get(created.headers["location"], cookies=_cookies(admin))
    assert page.status_code == 200 and "spv-test-web" in page.text and "CNY" in page.text

    changed = await _post_form(
        client,
        admin,
        "/admin/ai-model-prices/",
        {
            "provider": "testprov",
            "model": "spv-test-web",
            "return_to": "model",
            "currency": "CNY",
            "effective_from": "2026-06-01T00:00",
            "input": "1.5",
            "output": "6",
        },
    )
    assert "notice=closed" in changed.headers["location"]
    rows = await _service.history(db_session, "testprov", "spv-test-web")
    assert [(r.effective_from, r.effective_to) for r in rows] == [(T1, None), (T0, T1)]

    bad = await _post_form(
        client,
        admin,
        "/admin/ai-model-prices/",
        {
            "provider": "testprov",
            "model": "spv-test-web",
            "return_to": "model",
            "currency": "CNY",
            "effective_from": "2026-06-01T00:00",
            "input": "1",
            "output": "x",
        },
    )
    assert "error=ai_model_price_invalid" in bad.headers["location"]

    newest = rows[0]
    off = await _post_form(client, admin, f"/admin/ai-model-prices/{newest.id}/active/", {"active": "0"})
    assert "notice=deactivated" in off.headers["location"]
    await db_session.refresh(newest)
    assert not newest.is_active
    removed = await _post_form(client, admin, f"/admin/ai-model-prices/{newest.id}/delete/", {})
    assert "notice=deleted" in removed.headers["location"]


async def test_the_seed_button(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    resp = await _post_form(client, admin, "/admin/ai-model-prices/seed/", {})
    assert resp.status_code == 303 and "notice=seed_unchanged" in resp.headers["location"]
