"""Exchange rates: storage, manual entry, CSV import, settings, the optional fetch, conversion and the admin page.

Every test here may change the shared exchange rate settings rows, which is why
they stay in one module (``--dist loadfile`` keeps a module on one worker). The
rate feed is always a mocked transport over recorded fixtures; nothing reaches
the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exchange import ConversionMethod, Converted, MissingReason, RateMissing, RatePolicy
from specivo.core.money import Money
from specivo.models.exchange_rate import ExchangeRate
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.exchange_rate_service import (
    EXCHANGE_RATE_SETTING_KEYS,
    ExchangeRateService,
)
from specivo.services.security_audit_service import AuditEvent
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = ExchangeRateService()
DAY = date(2026, 11, 5)
ECB_XML = (
    Path(__file__).resolve().parents[1] / "fixtures" / "exchange_rates" / "ecb-eurofxref-daily-2026-11-05.xml"
).read_bytes()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _clean_settings(db_session: AsyncSession) -> None:
    await db_session.execute(
        text("DELETE FROM settings WHERE key = ANY(:keys)"), {"keys": list(EXCHANGE_RATE_SETTING_KEYS)}
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="xr_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def regular(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="xr_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _csrf(client: AsyncClient, user: User) -> str:
    resp = await client.get("/admin/exchange-rates/", cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError("no csrf_token cookie on the exchange rates page")


async def _post_form(client: AsyncClient, user: User, url: str, data: dict[str, str], **kwargs):
    csrf = await _csrf(client, user)
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
        **kwargs,
    )


async def _audit_rows(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event).order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


async def _add(db: AsyncSession, day: date, base: str, quote: str, rate: str, source: str = "ecb") -> ExchangeRate:
    row = ExchangeRate(rate_date=day, base_code=base, quote_code=quote, rate=Decimal(rate), source=source)
    db.add(row)
    await db.flush()
    return row


async def _count(db: AsyncSession, **filters: str) -> int:
    stmt = select(func.count()).select_from(ExchangeRate)
    for key, value in filters.items():
        stmt = stmt.where(getattr(ExchangeRate, key) == value)
    return int(await db.scalar(stmt) or 0)


def _ecb_transport(calls: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=ECB_XML, headers={"Content-Type": "text/xml"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "quote", "rate", "source"),
    [
        ("USD", "USD", "1", "manual"),
        ("USD", "THB", "0", "manual"),
        ("USD", "THB", "-1", "csv"),
        ("USD", "THB", "1", "bank"),
    ],
)
async def test_database_checks(db_session: AsyncSession, base: str, quote: str, rate: str, source: str) -> None:
    with pytest.raises(IntegrityError, match="ck_exchange_rates"):
        await _add(db_session, DAY, base, quote, rate, source)


async def test_one_rate_per_day_pair_and_source(db_session: AsyncSession) -> None:
    await _add(db_session, DAY, "USD", "THB", "32", "csv")
    await _add(db_session, DAY, "USD", "THB", "33", "manual")
    with pytest.raises(IntegrityError, match="uq_exchange_rates_date_pair_source"):
        await _add(db_session, DAY, "USD", "THB", "34", "csv")


async def test_rates_store_twelve_decimal_places_exactly(db_session: AsyncSession) -> None:
    row = await _add(db_session, DAY, "USD", "IDR", "16712.123456789012", "manual")
    await db_session.commit()
    await db_session.refresh(row)

    assert row.rate == Decimal("16712.123456789012")
    assert type(row.rate) is Decimal


# ---------------------------------------------------------------------------
# Conversion with stored rates
# ---------------------------------------------------------------------------


async def test_convert_direct_inverse_and_through_the_pivot(db_session: AsyncSession) -> None:
    await _add(db_session, DAY, "EUR", "USD", "1.1224")
    await _add(db_session, DAY, "EUR", "THB", "37.533")

    direct = await _service.convert(db_session, Money("10", "EUR"), "THB", DAY)
    inverse = await _service.convert(db_session, Money("37.533", "THB"), "EUR", DAY)
    pivot = await _service.convert(db_session, Money("100", "USD"), "THB", DAY)

    assert isinstance(direct, Converted) and direct.money == Money("375.33", "THB")
    assert isinstance(inverse, Converted) and inverse.rate.method is ConversionMethod.INVERSE
    assert inverse.money == Money("1", "EUR")
    assert isinstance(pivot, Converted) and pivot.rate.method is ConversionMethod.PIVOT
    assert pivot.money.amount == Decimal("3343.99501069")


async def test_pivot_comes_from_the_settings(db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, DAY, "USD", "EUR", "0.9", "manual")
    await _add(db_session, DAY, "USD", "THB", "32", "manual")

    with_eur = await _service.convert(db_session, Money("9", "EUR"), "THB", DAY)
    await _service.update_settings(db_session, admin, pivot="USD")
    with_usd = await _service.convert(db_session, Money("9", "EUR"), "THB", DAY)

    assert isinstance(with_eur, RateMissing)
    assert isinstance(with_usd, Converted)
    assert with_usd.money == Money("320", "THB")


async def test_source_precedence_from_the_database(db_session: AsyncSession) -> None:
    await _add(db_session, DAY, "USD", "THB", "33", "ecb")
    await _add(db_session, DAY, "USD", "THB", "32", "csv")
    only_csv = await _service.convert(db_session, Money("1", "USD"), "THB", DAY)
    await _add(db_session, DAY, "USD", "THB", "31", "manual")
    with_manual = await _service.convert(db_session, Money("1", "USD"), "THB", DAY)

    assert isinstance(only_csv, Converted) and only_csv.money == Money("32", "THB")
    assert isinstance(with_manual, Converted) and with_manual.money == Money("31", "THB")


async def test_max_age_setting_and_stale_reason(db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, date(2026, 10, 20), "USD", "THB", "32")

    stale = await _service.convert(db_session, Money("5", "USD"), "THB", DAY)
    await _service.update_settings(db_session, admin, max_age_days=30)
    fresh = await _service.convert(db_session, Money("5", "USD"), "THB", DAY)
    latest = await _service.convert(db_session, Money("5", "USD"), "THB", date(2020, 1, 1), RatePolicy.latest())

    assert isinstance(stale, RateMissing)
    assert stale.reason is MissingReason.STALE
    assert stale.newest_rate_date == date(2026, 10, 20)
    assert isinstance(fresh, Converted)
    assert isinstance(latest, Converted)


async def test_batch_conversion_needs_a_fixed_number_of_queries(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    for offset in range(10):
        await _add(db_session, date(2026, 10, 20 + offset), "EUR", "USD", f"1.1{offset}")
        await _add(db_session, date(2026, 10, 20 + offset), "EUR", "THB", f"37.{offset}")
    items = [
        (Money(str(i + 1), ("USD", "THB", "EUR", "JPY")[i % 4]), date(2026, 10, 20) + timedelta(days=i % 15))
        for i in range(200)
    ]

    calls = 0
    original = db_session.execute

    async def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(db_session, "execute", counting)
    summary = await _service.summarize(db_session, items, "EUR")

    assert calls <= 3  # the settings, the rates in the window, the newest rate before it
    assert summary.converted_count + sum(g.count for g in summary.unconverted) == 200
    assert {g.currency for g in summary.unconverted} >= {"JPY"}


async def test_summary_never_drops_unconverted_amounts(db_session: AsyncSession) -> None:
    await _add(db_session, DAY, "USD", "THB", "32")
    items = [(Money("10", "USD"), DAY), (Money("100", "THB"), DAY), (Money("7", "JPY"), DAY), (Money("3", "JPY"), DAY)]

    summary = await _service.summarize(db_session, items, "THB")

    assert summary.total == Money("420", "THB")
    assert [(g.currency, g.reason, g.amount, g.count) for g in summary.unconverted] == [
        ("JPY", MissingReason.NO_RATE, Money("10", "JPY"), 2)
    ]


# ---------------------------------------------------------------------------
# Manual rates through the API
# ---------------------------------------------------------------------------


async def test_admin_adds_a_manual_rate_with_audit(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    resp = await client.post(
        "/api/v1/admin/exchange-rates/",
        json={"rate_date": "2026-11-05", "base": "usd", "quote": "THB", "rate": "32.4500"},
        headers=_bearer(admin),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["base"] == "USD" and body["quote"] == "THB" and body["source"] == "manual"
    assert body["rate"] == "32.450000000000"
    assert body["created_by_id"] == admin.id
    rows = await _audit_rows(db_session, AuditEvent.EXCHANGE_RATE_CREATED)
    assert [r.details for r in rows] == [
        {
            "rate_id": body["id"],
            "rate_date": "2026-11-05",
            "base": "USD",
            "quote": "THB",
            "rate": "32.45",
            "source": "manual",
        }
    ]
    assert rows[0].resource_id == body["id"]


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"rate": "0"}, 422, "exchange_rate_invalid"),
        ({"rate": "1.1234567890123"}, 422, "exchange_rate_invalid"),
        ({"rate": "1e3"}, 422, "exchange_rate_invalid"),
        ({"rate": "-2"}, 422, "exchange_rate_invalid"),
        ({"quote": "USD"}, 422, "exchange_rate_same_currency"),
        ({"quote": "XAU"}, 422, "currency_inactive"),
        ({"quote": "ZZZ"}, 422, "currency_unknown"),
    ],
)
async def test_manual_rate_validation(
    client: AsyncClient, db_session: AsyncSession, admin: User, payload: dict, status: int, code: str
) -> None:
    body = {"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "32", **payload}

    resp = await client.post("/api/v1/admin/exchange-rates/", json=body, headers=_bearer(admin))

    assert resp.status_code == status, resp.text
    assert resp.json()["errors"][0]["code"] == code
    assert await _count(db_session) == 0


async def test_rates_must_be_strings_not_json_numbers(client: AsyncClient, admin: User) -> None:
    resp = await client.post(
        "/api/v1/admin/exchange-rates/",
        json={"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": 32.45},
        headers=_bearer(admin),
    )
    assert resp.status_code == 422


async def test_second_manual_rate_for_the_day_is_a_conflict(client: AsyncClient, admin: User) -> None:
    body = {"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "32"}
    first = await client.post("/api/v1/admin/exchange-rates/", json=body, headers=_bearer(admin))
    second = await client.post("/api/v1/admin/exchange-rates/", json=body, headers=_bearer(admin))

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["errors"][0]["code"] == "exchange_rate_exists"


async def test_edit_and_delete_a_manual_rate_with_audit(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    created = await client.post(
        "/api/v1/admin/exchange-rates/",
        json={"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "32"},
        headers=_bearer(admin),
    )
    rate_id = created.json()["id"]

    edited = await client.patch(
        f"/api/v1/admin/exchange-rates/{rate_id}/",
        json={"rate": "32.5", "rate_date": "2026-11-04"},
        headers=_bearer(admin),
    )
    unchanged = await client.patch(
        f"/api/v1/admin/exchange-rates/{rate_id}/", json={"rate": "32.50"}, headers=_bearer(admin)
    )
    deleted = await client.delete(f"/api/v1/admin/exchange-rates/{rate_id}/", headers=_bearer(admin))
    missing = await client.get(f"/api/v1/admin/exchange-rates/{rate_id}/", headers=_bearer(admin))

    assert edited.status_code == 200, edited.text
    assert edited.json()["rate"] == "32.500000000000"
    assert edited.json()["rate_date"] == "2026-11-04"
    assert unchanged.status_code == 200
    assert deleted.status_code == 204
    assert missing.status_code == 404
    updates = await _audit_rows(db_session, AuditEvent.EXCHANGE_RATE_UPDATED)
    assert len(updates) == 1
    assert updates[0].details["changes"] == {
        "rate_date": {"old": "2026-11-05", "new": "2026-11-04"},
        "rate": {"old": "32.000000000000", "new": "32.5"},
    }
    deletes = await _audit_rows(db_session, AuditEvent.EXCHANGE_RATE_DELETED)
    assert [d.details["rate_id"] for d in deletes] == [rate_id]


async def test_imported_and_fetched_rates_cannot_be_edited_but_can_be_deleted(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    row = await _add(db_session, DAY, "EUR", "THB", "37.5", "ecb")
    await db_session.commit()

    edit = await client.patch(f"/api/v1/admin/exchange-rates/{row.id}/", json={"rate": "38"}, headers=_bearer(admin))
    delete = await client.delete(f"/api/v1/admin/exchange-rates/{row.id}/", headers=_bearer(admin))

    assert edit.status_code == 422
    assert edit.json()["errors"][0]["code"] == "exchange_rate_not_editable"
    assert delete.status_code == 204


async def test_list_filters_and_pages(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, date(2026, 11, 3), "EUR", "THB", "37.4", "ecb")
    await _add(db_session, date(2026, 11, 4), "EUR", "USD", "1.12", "ecb")
    await _add(db_session, DAY, "USD", "THB", "32", "manual")
    await db_session.commit()

    everything = await client.get("/api/v1/admin/exchange-rates/", headers=_bearer(admin))
    thb = await client.get("/api/v1/admin/exchange-rates/?currency=THB", headers=_bearer(admin))
    manual = await client.get("/api/v1/admin/exchange-rates/?source=manual", headers=_bearer(admin))
    window = await client.get(
        "/api/v1/admin/exchange-rates/?date_from=2026-11-04&date_to=2026-11-04", headers=_bearer(admin)
    )
    page = await client.get("/api/v1/admin/exchange-rates/?limit=1&offset=1", headers=_bearer(admin))

    assert [r["rate_date"] for r in everything.json()["items"]] == ["2026-11-05", "2026-11-04", "2026-11-03"]
    assert thb.json()["total_count"] == 2
    assert [r["source"] for r in manual.json()["items"]] == ["manual"]
    assert [r["quote"] for r in window.json()["items"]] == ["USD"]
    assert page.json()["total_count"] == 3 and [r["rate_date"] for r in page.json()["items"]] == ["2026-11-04"]


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

_GOOD_CSV = """date,base,quote,rate
2026-11-04,USD,THB,32.40
# a comment line

2026-11-05,usd,thb,32.45
2026-11-05,EUR,THB,37.533
"""


async def test_csv_import_happy_path_with_audit(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    resp = await client.post("/api/v1/admin/exchange-rates/import/", json={"csv": _GOOD_CSV}, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True and body["errors"] == []
    assert (body["rows"], body["created"], body["updated"], body["unchanged"]) == (3, 3, 0, 0)
    assert [p["line"] for p in body["preview"]] == [2, 5, 6]
    assert await _count(db_session, source="csv") == 3
    audit = await _audit_rows(db_session, AuditEvent.EXCHANGE_RATES_IMPORTED)
    assert [a.details for a in audit] == [
        {"rows": 3, "created": 3, "updated": 0, "unchanged": 0, "date_from": "2026-11-04", "date_to": "2026-11-05"}
    ]


async def test_csv_reimport_updates_changed_rates_only(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await client.post("/api/v1/admin/exchange-rates/import/", json={"csv": _GOOD_CSV}, headers=_bearer(admin))
    changed = _GOOD_CSV.replace("37.533", "37.6")

    resp = await client.post("/api/v1/admin/exchange-rates/import/", json={"csv": changed}, headers=_bearer(admin))

    assert (resp.json()["created"], resp.json()["updated"], resp.json()["unchanged"]) == (0, 1, 2)
    stored = await db_session.scalar(
        select(ExchangeRate.rate).where(ExchangeRate.base_code == "EUR", ExchangeRate.source == "csv")
    )
    assert stored == Decimal("37.6")
    assert await _count(db_session, source="csv") == 3


async def test_csv_dry_run_writes_nothing(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    resp = await client.post(
        "/api/v1/admin/exchange-rates/import/", json={"csv": _GOOD_CSV, "dry_run": True}, headers=_bearer(admin)
    )

    assert resp.status_code == 200
    assert resp.json()["applied"] is False and resp.json()["created"] == 3
    assert await _count(db_session) == 0
    assert await _audit_rows(db_session, AuditEvent.EXCHANGE_RATES_IMPORTED) == []


async def test_csv_errors_are_reported_per_line_and_nothing_is_written(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    content = "\n".join(
        [
            "date,base,quote,rate",
            "2026-11-05,USD,THB,32.45",
            "05/11/2026,USD,THB,32.45",
            "2026-11-05,USD,ZZZ,1",
            "2026-11-05,USD,XAU,1",
            "2026-11-05,USD,USD,1",
            "2026-11-05,USD,EUR,0.9.1",
            "2026-11-05,USD,EUR,0",
            "2026-11-05,USD,EUR,1.1234567890123",
            "2026-11-05,USD,THB,32.50",
            "2026-11-05,USD,EUR",
        ]
    )

    resp = await client.post("/api/v1/admin/exchange-rates/import/", json={"csv": content}, headers=_bearer(admin))
    dry = await client.post(
        "/api/v1/admin/exchange-rates/import/", json={"csv": content, "dry_run": True}, headers=_bearer(admin)
    )

    assert resp.status_code == 422
    assert dry.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert [(e["line"], e["code"]) for e in body["errors"]] == [
        (3, "csv_date"),
        (4, "currency_unknown"),
        (5, "currency_inactive"),
        (6, "exchange_rate_same_currency"),
        (7, "exchange_rate_invalid"),
        (8, "exchange_rate_invalid"),
        (9, "exchange_rate_invalid"),
        (10, "csv_duplicate"),
        (11, "csv_columns"),
    ]
    assert dry.json()["errors"] == body["errors"]
    assert await _count(db_session) == 0


async def test_csv_empty_and_not_utf8(db_session: AsyncSession, admin: User) -> None:
    empty = await _service.import_csv(db_session, "date,base,quote,rate\n\n", actor=admin)
    latin = await _service.import_csv(db_session, "2026-11-05,USD,THB,32\n# caf\xe9".encode("latin-1"), actor=admin)

    assert [e.code for e in empty.errors] == ["csv_empty"]
    assert [e.code for e in latin.errors] == ["csv_encoding"]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def test_settings_defaults(client: AsyncClient, admin: User) -> None:
    resp = await client.get("/api/v1/admin/exchange-rates/settings/", headers=_bearer(admin))

    assert resp.json() == {
        "pivot": "EUR",
        "max_age_days": 7,
        "fetch_enabled": False,
        "fetch_url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        "last_fetch": None,
    }


async def test_settings_changes_are_audited(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    resp = await client.patch(
        "/api/v1/admin/exchange-rates/settings/",
        json={"pivot": "usd", "max_age_days": 3, "fetch_enabled": True, "fetch_url": "http://rates.internal/v1/latest"},
        headers=_bearer(admin),
    )
    again = await client.patch(
        "/api/v1/admin/exchange-rates/settings/", json={"fetch_enabled": True}, headers=_bearer(admin)
    )
    off = await client.patch(
        "/api/v1/admin/exchange-rates/settings/", json={"fetch_enabled": False}, headers=_bearer(admin)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["pivot"] == "USD" and resp.json()["max_age_days"] == 3 and resp.json()["fetch_enabled"] is True
    assert again.status_code == 200 and off.json()["fetch_enabled"] is False
    changed = await _audit_rows(db_session, AuditEvent.EXCHANGE_RATE_SETTINGS_CHANGED)
    assert [c.details for c in changed] == [
        {
            "changes": {
                "pivot": {"old": "EUR", "new": "USD"},
                "max_age_days": {"old": 7, "new": 3},
                "fetch_url": {
                    "old": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
                    "new": "http://rates.internal/v1/latest",
                },
            }
        }
    ]
    toggles = await _audit_rows(db_session, AuditEvent.EXCHANGE_RATES_FETCH_TOGGLED)
    assert [t.details["enabled"] for t in toggles] == [True, False]
    assert toggles[0].details["url"] == "http://rates.internal/v1/latest"


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"pivot": "XAU"}, 422),
        ({"max_age_days": 400}, 422),
        ({"max_age_days": -1}, 422),
        ({"fetch_url": "ftp://rates.example.com/x.xml"}, 422),
        ({"fetch_url": "http://169.254.169.254/latest/"}, 422),
    ],
)
async def test_settings_validation(client: AsyncClient, admin: User, payload: dict, status: int) -> None:
    resp = await client.patch("/api/v1/admin/exchange-rates/settings/", json=payload, headers=_bearer(admin))
    assert resp.status_code == status, resp.text


async def test_generic_settings_endpoint_refuses_exchange_rate_keys(client: AsyncClient, admin: User) -> None:
    resp = await client.patch(
        "/api/v1/admin/settings/", json={"exchange_rates_fetch_enabled": "true"}, headers=_bearer(admin)
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Optional fetch
# ---------------------------------------------------------------------------


async def test_fetch_is_off_by_default_and_the_task_does_not_touch_the_network(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.tasks import exchange_rates as task_module

    @asynccontextmanager
    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("specivo.tasks._async.task_session", session_override)
    calls: list[str] = []

    await task_module.fetch_exchange_rates_async(transport=_ecb_transport(calls))

    assert calls == []
    assert await _count(db_session) == 0
    assert (await _service.get_settings(db_session)).last_fetch is None


async def test_enabled_fetch_stores_ecb_rates(
    db_session: AsyncSession, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.tasks import exchange_rates as task_module

    @asynccontextmanager
    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("specivo.tasks._async.task_session", session_override)
    await _service.update_settings(db_session, admin, fetch_enabled=True)
    calls: list[str] = []

    await task_module.fetch_exchange_rates_async(transport=_ecb_transport(calls))
    await task_module.fetch_exchange_rates_async(transport=_ecb_transport(calls))

    assert calls == ["https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"] * 2
    assert await _count(db_session, source="ecb") == 29
    thb = await db_session.scalar(
        select(ExchangeRate).where(ExchangeRate.quote_code == "THB", ExchangeRate.source == "ecb")
    )
    assert thb is not None and (thb.rate_date, thb.base_code, thb.rate) == (DAY, "EUR", Decimal("37.533"))
    last = (await _service.get_settings(db_session)).last_fetch
    assert last is not None and last["ok"] is True and last["stored"] == 29 and last["rate_date"] == "2026-11-05"
    converted = await _service.convert(db_session, Money("100", "USD"), "THB", DAY)
    assert isinstance(converted, Converted) and converted.rate.method is ConversionMethod.PIVOT


async def test_failed_fetch_is_recorded_not_raised(db_session: AsyncSession, admin: User) -> None:
    await _service.update_settings(db_session, admin, fetch_enabled=True)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network is unreachable")

    result = await _service.fetch_and_store(db_session, transport=httpx.MockTransport(offline))

    assert result.ok is False and "Could not reach" in (result.error or "")
    last = (await _service.get_settings(db_session)).last_fetch
    assert last is not None and last["ok"] is False and "Could not reach" in last["error"]
    assert await _count(db_session) == 0


async def test_fetched_rates_never_override_manual_ones(db_session: AsyncSession, admin: User) -> None:
    await _service.create_manual(db_session, rate_date=DAY, base="EUR", quote="THB", rate="40", actor=admin)
    await _service.fetch_and_store(db_session, force=True, transport=_ecb_transport())

    result = await _service.convert(db_session, Money("1", "EUR"), "THB", DAY)

    assert isinstance(result, Converted) and result.money == Money("40", "THB")
    assert await _count(db_session, source="manual") == 1


# ---------------------------------------------------------------------------
# Conversion preview API
# ---------------------------------------------------------------------------


async def test_convert_endpoint_converted_and_unconverted(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await _add(db_session, DAY, "USD", "THB", "32.45", "manual")
    await db_session.commit()

    ok = await client.post(
        "/api/v1/admin/exchange-rates/convert/",
        json={"money": {"amount": "10.00", "currency": "USD"}, "to": "THB", "on": "2026-11-06"},
        headers=_bearer(admin),
    )
    missing = await client.post(
        "/api/v1/admin/exchange-rates/convert/",
        json={"money": {"amount": "10", "currency": "JPY"}, "to": "THB", "on": "2026-11-06", "policy": "latest"},
        headers=_bearer(admin),
    )
    bad_policy = await client.post(
        "/api/v1/admin/exchange-rates/convert/",
        json={"money": {"amount": "10", "currency": "USD"}, "to": "THB", "on": "2026-11-06", "policy": "soon"},
        headers=_bearer(admin),
    )
    too_precise = await client.post(
        "/api/v1/admin/exchange-rates/convert/",
        json={"money": {"amount": "10.001", "currency": "USD"}, "to": "THB", "on": "2026-11-06"},
        headers=_bearer(admin),
    )

    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "converted"
    assert ok.json()["converted"] == {"amount": "324.50000000", "currency": "THB"}
    assert ok.json()["rate"]["rate_date"] == "2026-11-05" and ok.json()["rate"]["method"] == "direct"
    assert ok.json()["policy"] == "entry_date"
    assert missing.json()["status"] == "unconverted" and missing.json()["reason"] == "no_rate"
    assert missing.json()["converted"] is None
    assert bad_policy.status_code == 422 and bad_policy.json()["errors"][0]["code"] == "rate_policy_invalid"
    assert too_precise.status_code == 422


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


async def test_non_admins_are_refused_everywhere(client: AsyncClient, db_session: AsyncSession, regular: User) -> None:
    row = await _add(db_session, DAY, "USD", "THB", "32", "manual")
    await db_session.commit()
    h = _bearer(regular)
    calls = [
        client.get("/api/v1/admin/exchange-rates/", headers=h),
        client.post(
            "/api/v1/admin/exchange-rates/",
            json={"rate_date": "2026-11-05", "base": "EUR", "quote": "THB", "rate": "37"},
            headers=h,
        ),
        client.patch(f"/api/v1/admin/exchange-rates/{row.id}/", json={"rate": "1"}, headers=h),
        client.delete(f"/api/v1/admin/exchange-rates/{row.id}/", headers=h),
        client.post("/api/v1/admin/exchange-rates/import/", json={"csv": _GOOD_CSV}, headers=h),
        client.get("/api/v1/admin/exchange-rates/settings/", headers=h),
        client.patch("/api/v1/admin/exchange-rates/settings/", json={"fetch_enabled": True}, headers=h),
        client.post("/api/v1/admin/exchange-rates/fetch/", headers=h),
        client.post(
            "/api/v1/admin/exchange-rates/convert/",
            json={"money": {"amount": "1", "currency": "USD"}, "to": "THB", "on": "2026-11-05"},
            headers=h,
        ),
    ]
    for call in calls:
        resp = await call
        assert resp.status_code == 403, (resp.request.method, resp.request.url, resp.status_code)

    page = await client.get("/admin/exchange-rates/", cookies=_cookies(regular))
    assert page.status_code == 403
    assert await _count(db_session) == 1
    assert (await _service.get_settings(db_session)).fetch_enabled is False


async def test_unauthenticated_api_is_refused(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/admin/exchange-rates/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------


async def test_admin_page_lists_rates_and_shows_the_attribution(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    await _add(db_session, DAY, "EUR", "THB", "37.533", "ecb")
    await db_session.commit()

    resp = await client.get("/admin/exchange-rates/", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert "1 EUR = 37.533 THB" in resp.text
    assert 'data-testid="ecb-attribution"' in resp.text
    assert "European Central Bank" in resp.text


async def test_admin_page_conversion_preview(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, DAY, "EUR", "THB", "37.533", "ecb")
    await db_session.commit()

    ok = await client.get(
        "/admin/exchange-rates/?amount=10&from=EUR&to=THB&on=2026-11-05&policy=entry_date", cookies=_cookies(admin)
    )
    missing = await client.get(
        "/admin/exchange-rates/?amount=10&from=JPY&to=THB&on=2026-11-05", cookies=_cookies(admin)
    )

    assert ok.status_code == 200
    assert "375.33" in ok.text and "2026-11-05" in ok.text
    assert "could not be converted" in missing.text


async def test_admin_page_adds_edits_and_deletes(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    created = await _post_form(
        client,
        admin,
        "/admin/exchange-rates/",
        {"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "32.45"},
    )
    duplicate = await _post_form(
        client,
        admin,
        "/admin/exchange-rates/",
        {"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "32"},
    )
    row = await db_session.scalar(select(ExchangeRate).where(ExchangeRate.source == "manual"))
    assert row is not None
    edit_page = await client.get(f"/admin/exchange-rates/{row.id}/edit/", cookies=_cookies(admin))
    edited = await _post_form(
        client,
        admin,
        f"/admin/exchange-rates/{row.id}/edit/",
        {"rate_date": "2026-11-05", "base": "USD", "quote": "THB", "rate": "33"},
    )
    deleted = await _post_form(client, admin, f"/admin/exchange-rates/{row.id}/delete/", {})

    assert created.status_code == 303 and "notice=created" in created.headers["location"]
    assert duplicate.status_code == 303 and "error=exchange_rate_exists" in duplicate.headers["location"]
    assert edit_page.status_code == 200 and 'value="32.45"' in edit_page.text
    assert edited.status_code == 303 and "notice=updated" in edited.headers["location"]
    assert deleted.status_code == 303 and "notice=deleted" in deleted.headers["location"]
    assert await _count(db_session) == 0


async def test_admin_page_csv_preview_then_import(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    preview = await _post_form(
        client, admin, "/admin/exchange-rates/import/", {"csv_text": _GOOD_CSV, "action": "preview"}
    )
    assert preview.status_code == 200
    assert "Nothing has been written yet" in preview.text
    assert await _count(db_session) == 0

    upload = await _post_form(
        client,
        admin,
        "/admin/exchange-rates/import/",
        {"action": "import"},
        files={"file": ("rates.csv", _GOOD_CSV.encode(), "text/csv")},
    )
    assert upload.status_code == 200, upload.text
    assert "Imported: 3 new" in upload.text
    assert await _count(db_session, source="csv") == 3

    bad = await _post_form(
        client, admin, "/admin/exchange-rates/import/", {"csv_text": "2026-11-05,USD,ZZZ,1", "action": "import"}
    )
    assert bad.status_code == 422
    assert "Line 1" in bad.text


async def test_admin_page_saves_settings(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    saved = await _post_form(
        client,
        admin,
        "/admin/exchange-rates/settings/",
        {"pivot": "EUR", "max_age_days": "5", "fetch_enabled": "1", "fetch_url": ""},
    )
    bad = await _post_form(
        client, admin, "/admin/exchange-rates/settings/", {"pivot": "EUR", "max_age_days": "five", "fetch_url": ""}
    )

    assert saved.status_code == 303 and "notice=settings_saved" in saved.headers["location"]
    assert bad.status_code == 303 and "error=exchange_rate_max_age_invalid" in bad.headers["location"]
    settings = await _service.get_settings(db_session)
    assert (settings.max_age_days, settings.fetch_enabled) == (5, True)


async def test_admin_page_renders_in_thai(client: AsyncClient, db_session: AsyncSession, admin: User) -> None:
    await _add(db_session, DAY, "EUR", "THB", "37.533", "ecb")
    admin.language = "th"
    await db_session.commit()

    resp = await client.get("/admin/exchange-rates/?amount=10&from=EUR&to=THB&on=2026-11-05", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert "อัตราแลกเปลี่ยน" in resp.text
    assert "฿375.33" in resp.text


async def test_stored_fetch_error_holds_no_response_content(db_session: AsyncSession, admin: User) -> None:
    marker = "INTERNAL-SECRET-7f3a"

    def leaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=f'{{"status": 500, "message": "{marker}"}}'.encode())

    result = await _service.fetch_and_store(db_session, force=True, transport=httpx.MockTransport(leaky))

    last = (await _service.get_settings(db_session)).last_fetch
    assert result.ok is False
    assert last is not None and last["error"] == "The feed returned an error"
    assert marker not in (result.error or "")
