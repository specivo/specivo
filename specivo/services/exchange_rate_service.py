"""Exchange rates: storing them, importing them, fetching them, and converting with them.

- **Rates** (``exchange_rates``): one daily rate per pair and source. Admins
  add, edit and delete ``manual`` rates, import ``csv`` files
  (``date,base,quote,rate``, all-or-nothing, with a dry run) and may switch on
  a daily fetch of the ECB euro reference rates (``ecb``; off by default).
- **Settings** (rows in ``settings``): the pivot currency for triangulation
  (``exchange_rate_pivot``, EUR), the maximum age of a rate
  (``exchange_rate_max_age_days``, 7), the fetch switch and URL, and the
  outcome of the last fetch.
- **Conversion**: :meth:`ExchangeRateService.convert`,
  :meth:`~ExchangeRateService.convert_many` and
  :meth:`~ExchangeRateService.summarize` load the rates a batch needs in at
  most two queries and resolve them with :class:`specivo.core.exchange.RateBook`
  (rules in that module). An amount without a usable rate comes back as
  :class:`~specivo.core.exchange.RateMissing`, never as zero.

Every change an administrator makes is audited (``exchange_rate_*`` events).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.currency_data import CURRENCY_CODE_RE
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.exchange import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_PIVOT,
    ConversionResult,
    ConversionSummary,
    PolicyKind,
    RateBook,
    RateObservation,
    RatePolicy,
    summarize,
)
from specivo.core.money import Money, format_decimal_plain, normalize_currency_code
from specivo.models.currency import Currency
from specivo.models.exchange_rate import EXCHANGE_RATE_SOURCES, ExchangeRate
from specivo.models.setting import Setting
from specivo.services.currency_service import CurrencyService
from specivo.services.exchange_rate_feed import DEFAULT_FEED_URL, FeedError, fetch_feed, validate_feed_url
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    import httpx
    from fastapi import Request

    from specivo.models.user import User

logger = logging.getLogger(__name__)

PIVOT_SETTING_KEY = "exchange_rate_pivot"
MAX_AGE_SETTING_KEY = "exchange_rate_max_age_days"
FETCH_ENABLED_SETTING_KEY = "exchange_rates_fetch_enabled"
FETCH_URL_SETTING_KEY = "exchange_rates_fetch_url"
LAST_FETCH_SETTING_KEY = "exchange_rates_last_fetch"

#: Settings managed here; the generic settings API refuses them.
EXCHANGE_RATE_SETTING_KEYS = frozenset(
    {PIVOT_SETTING_KEY, MAX_AGE_SETTING_KEY, FETCH_ENABLED_SETTING_KEY, FETCH_URL_SETTING_KEY, LAST_FETCH_SETTING_KEY}
)

MAX_AGE_LIMIT_DAYS = 366

#: A rate as people type it: up to 12 digits before and 12 after the point (``NUMERIC(24, 12)``).
_RATE_RE = re.compile(r"^\d{1,12}(\.\d{1,12})?$")

#: CSV import limits.
CSV_MAX_BYTES = 2 * 1024 * 1024
CSV_MAX_ROWS = 20_000
CSV_PREVIEW_ROWS = 50
CSV_HEADER = ("date", "base", "quote", "rate")

_WRITE_CHUNK = 1000

_audit = SecurityAuditService()


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def parse_rate(value: object, *, field: str = "rate") -> Decimal:
    """A positive rate from a decimal string (or ``Decimal``/``int``); 422 ``exchange_rate_invalid`` otherwise.

    At most 12 digits before and 12 after the decimal point; never rounded.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise _invalid_rate("Rates must be given as decimal strings", field)
    if isinstance(value, Decimal):
        text = format_decimal_plain(value)
    else:
        text = str(value).strip() if isinstance(value, (str, int)) else ""
    if "." in text:
        head, tail = text.split(".", 1)
        tail = tail.rstrip("0")
        text = f"{head}.{tail}" if tail else head
    if not _RATE_RE.match(text):
        raise _invalid_rate("Enter a positive decimal number with at most 12 decimal places, such as 36.5", field)
    rate = Decimal(text)
    if rate <= 0:
        raise _invalid_rate("A rate must be greater than zero", field)
    return rate


def _invalid_rate(message: str, field: str) -> AppError:
    return AppError(code="exchange_rate_invalid", message=message, status_code=422, field=field)


def parse_policy(value: str | None, max_age_days: int) -> RatePolicy:
    """A :class:`RatePolicy` from ``entry_date`` / ``latest`` / a date; 422 ``rate_policy_invalid`` otherwise."""
    try:
        return RatePolicy.parse(value, max_age_days)
    except ValueError as exc:
        raise AppError(code="rate_policy_invalid", message=str(exc), status_code=422, field="policy") from None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeRateSettings:
    pivot: str
    max_age_days: int
    fetch_enabled: bool
    fetch_url: str
    #: ``{"at": ISO timestamp, "ok": bool, "stored": int, "rate_date": ISO date | None, "error": str | None}``
    last_fetch: dict[str, Any] | None

    @property
    def fetch_url_is_default(self) -> bool:
        return self.fetch_url == DEFAULT_FEED_URL


@dataclass(frozen=True, slots=True)
class CsvRow:
    line: int
    rate_date: date
    base: str
    quote: str
    rate: Decimal
    #: ``create``, ``update`` (an imported rate for the day changes) or ``unchanged``.
    action: str = "create"


@dataclass(frozen=True, slots=True)
class CsvLineError:
    line: int
    code: str
    message: str


@dataclass
class CsvImportResult:
    """What an import did, or would do (``dry_run``). Nothing is written when there are errors."""

    dry_run: bool
    rows: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[CsvLineError] = field(default_factory=list)
    preview: list[CsvRow] = field(default_factory=list)
    applied: bool = False


@dataclass(frozen=True, slots=True)
class FetchResult:
    ok: bool
    stored: int = 0
    skipped: int = 0
    rate_date: date | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ExchangeRateService:
    """Reads, changes and applies exchange rates."""

    # -- settings ---------------------------------------------------------------

    async def get_settings(self, session: AsyncSession) -> ExchangeRateSettings:
        rows = dict(
            (
                await session.execute(
                    select(Setting.key, Setting.value).where(Setting.key.in_(EXCHANGE_RATE_SETTING_KEYS))
                )
            ).all()
        )
        pivot = (rows.get(PIVOT_SETTING_KEY) or "").strip().upper()
        if not CURRENCY_CODE_RE.match(pivot):
            pivot = DEFAULT_PIVOT
        try:
            max_age = int(rows.get(MAX_AGE_SETTING_KEY) or DEFAULT_MAX_AGE_DAYS)
        except ValueError:
            max_age = DEFAULT_MAX_AGE_DAYS
        if not 0 <= max_age <= MAX_AGE_LIMIT_DAYS:
            max_age = DEFAULT_MAX_AGE_DAYS
        last: dict[str, Any] | None = None
        raw_last = rows.get(LAST_FETCH_SETTING_KEY)
        if raw_last:
            try:
                parsed = json.loads(raw_last)
                last = parsed if isinstance(parsed, dict) else None
            except ValueError:
                last = None
        return ExchangeRateSettings(
            pivot=pivot,
            max_age_days=max_age,
            # Off unless an administrator switched it on.
            fetch_enabled=rows.get(FETCH_ENABLED_SETTING_KEY) == "true",
            fetch_url=(rows.get(FETCH_URL_SETTING_KEY) or "").strip() or DEFAULT_FEED_URL,
            last_fetch=last,
        )

    async def update_settings(
        self,
        session: AsyncSession,
        actor: User,
        request: Request | None = None,
        *,
        pivot: str | None = None,
        max_age_days: int | None = None,
        fetch_enabled: bool | None = None,
        fetch_url: str | None = None,
    ) -> ExchangeRateSettings:
        """Change the given settings (``None``: keep); audited per change.

        The pivot must be an enabled currency; the maximum age is 0 to 366
        days; the fetch URL an http(s) URL (empty: the ECB default).
        """
        current = await self.get_settings(session)
        writes: dict[str, str] = {}
        changes: dict[str, dict[str, Any]] = {}
        if pivot is not None:
            code = pivot.strip().upper()
            if code != current.pivot:
                code = (await CurrencyService().require_selectable(session, code, field="pivot")).code
                writes[PIVOT_SETTING_KEY] = code
                changes["pivot"] = {"old": current.pivot, "new": code}
        if max_age_days is not None:
            if not 0 <= max_age_days <= MAX_AGE_LIMIT_DAYS:
                raise AppError(
                    code="exchange_rate_max_age_invalid",
                    message=f"The maximum age must be between 0 and {MAX_AGE_LIMIT_DAYS} days",
                    status_code=422,
                    field="max_age_days",
                )
            if max_age_days != current.max_age_days:
                writes[MAX_AGE_SETTING_KEY] = str(max_age_days)
                changes["max_age_days"] = {"old": current.max_age_days, "new": max_age_days}
        if fetch_url is not None:
            url = fetch_url.strip() or DEFAULT_FEED_URL
            try:
                url = validate_feed_url(url)
            except ValueError as exc:
                raise AppError(
                    code="exchange_rate_fetch_url_invalid", message=str(exc), status_code=422, field="fetch_url"
                ) from None
            if url != current.fetch_url:
                writes[FETCH_URL_SETTING_KEY] = url
                changes["fetch_url"] = {"old": current.fetch_url, "new": url}
        toggled = fetch_enabled is not None and fetch_enabled != current.fetch_enabled
        if toggled:
            writes[FETCH_ENABLED_SETTING_KEY] = "true" if fetch_enabled else "false"
        for key, value in writes.items():
            stmt = pg_insert(Setting).values(key=key, value=value)
            await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
        await session.flush()
        if changes:
            await self._audit(session, AuditEvent.EXCHANGE_RATE_SETTINGS_CHANGED, actor, request, {"changes": changes})
        if toggled:
            await self._audit(
                session,
                AuditEvent.EXCHANGE_RATES_FETCH_TOGGLED,
                actor,
                request,
                {"enabled": bool(fetch_enabled), "url": writes.get(FETCH_URL_SETTING_KEY, current.fetch_url)},
            )
        return await self.get_settings(session)

    async def default_policy(self, session: AsyncSession) -> RatePolicy:
        """``entry_date`` with the instance's maximum age."""
        return RatePolicy.entry_date((await self.get_settings(session)).max_age_days)

    # -- reading rates --------------------------------------------------------------

    async def list_rates(
        self,
        session: AsyncSession,
        *,
        base: str | None = None,
        quote: str | None = None,
        currency: str | None = None,
        source: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ExchangeRate], int]:
        """Rates newest first, filtered; returns ``(page, total)``.

        *currency* matches either side of the pair.
        """
        stmt = select(ExchangeRate)
        conditions = []
        if base:
            conditions.append(ExchangeRate.base_code == base.strip().upper())
        if quote:
            conditions.append(ExchangeRate.quote_code == quote.strip().upper())
        if currency:
            code = currency.strip().upper()
            conditions.append(or_(ExchangeRate.base_code == code, ExchangeRate.quote_code == code))
        if source:
            conditions.append(ExchangeRate.source == source)
        if date_from:
            conditions.append(ExchangeRate.rate_date >= date_from)
        if date_to:
            conditions.append(ExchangeRate.rate_date <= date_to)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        ordered = stmt.order_by(
            ExchangeRate.rate_date.desc(), ExchangeRate.base_code, ExchangeRate.quote_code, ExchangeRate.source
        )
        rows = (await session.execute(ordered.offset(offset).limit(limit))).scalars().all()
        return list(rows), int(total)

    async def get_rate(self, session: AsyncSession, rate_id: int) -> ExchangeRate:
        rate = await session.get(ExchangeRate, rate_id)
        if rate is None:
            raise NotFoundError("Exchange rate not found")
        return rate

    # -- manual rates ---------------------------------------------------------------

    async def _pair(self, session: AsyncSession, base: str, quote: str) -> tuple[str, str]:
        currencies = CurrencyService()
        base_code = (await currencies.require_selectable(session, base, field="base")).code
        quote_code = (await currencies.require_selectable(session, quote, field="quote")).code
        if base_code == quote_code:
            raise AppError(
                code="exchange_rate_same_currency",
                message="The two currencies of a rate must differ",
                status_code=422,
                field="quote",
            )
        return base_code, quote_code

    async def _ensure_free(
        self, session: AsyncSession, rate_date: date, base: str, quote: str, exclude_id: int | None = None
    ) -> None:
        stmt = select(ExchangeRate.id).where(
            ExchangeRate.rate_date == rate_date,
            ExchangeRate.base_code == base,
            ExchangeRate.quote_code == quote,
            ExchangeRate.source == "manual",
        )
        if exclude_id is not None:
            stmt = stmt.where(ExchangeRate.id != exclude_id)
        if await session.scalar(stmt) is not None:
            raise AppError(
                code="exchange_rate_exists",
                message=f"A manual rate for {base}/{quote} on {rate_date.isoformat()} already exists; edit it instead",
                status_code=409,
                field="rate_date",
            )

    async def create_manual(
        self,
        session: AsyncSession,
        *,
        rate_date: date,
        base: str,
        quote: str,
        rate: object,
        actor: User,
        request: Request | None = None,
    ) -> ExchangeRate:
        """Add a manual rate ``1 base = rate quote`` on *rate_date*; audited.

        Both currencies must be enabled; one manual rate per day and pair (409
        ``exchange_rate_exists``).
        """
        value = parse_rate(rate)
        base_code, quote_code = await self._pair(session, base, quote)
        await self._ensure_free(session, rate_date, base_code, quote_code)
        row = ExchangeRate(
            rate_date=rate_date,
            base_code=base_code,
            quote_code=quote_code,
            rate=value,
            source="manual",
            created_by_id=actor.id,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent insert of the same day and pair: a 409, and the
            # request's transaction is rolled back with it.
            raise AppError(
                code="exchange_rate_exists",
                message="A manual rate for this day and pair already exists",
                status_code=409,
                field="rate_date",
            ) from None
        await self._audit(
            session, AuditEvent.EXCHANGE_RATE_CREATED, actor, request, _rate_details(row), resource_id=row.id
        )
        return row

    async def update_manual(
        self,
        session: AsyncSession,
        rate_id: int,
        *,
        actor: User,
        request: Request | None = None,
        rate_date: date | None = None,
        base: str | None = None,
        quote: str | None = None,
        rate: object | None = None,
    ) -> ExchangeRate:
        """Edit a manual rate (``None``: keep); audited with the changed fields.

        Imported and fetched rates are not edited (422
        ``exchange_rate_not_editable``): re-import the file, or add a manual
        rate, which takes precedence on its day.
        """
        row = await self.get_rate(session, rate_id)
        if row.source != "manual":
            raise AppError(
                code="exchange_rate_not_editable",
                message="Only manual rates can be edited. Add a manual rate for the day instead; it takes precedence.",
                status_code=422,
            )
        new_date = rate_date or row.rate_date
        new_base, new_quote = row.base_code, row.quote_code
        if (base is not None and base.strip().upper() != row.base_code) or (
            quote is not None and quote.strip().upper() != row.quote_code
        ):
            new_base, new_quote = await self._pair(session, base or row.base_code, quote or row.quote_code)
        new_rate = parse_rate(rate) if rate is not None else row.rate
        changes: dict[str, dict[str, str]] = {}
        if new_date != row.rate_date:
            changes["rate_date"] = {"old": row.rate_date.isoformat(), "new": new_date.isoformat()}
        if new_base != row.base_code:
            changes["base"] = {"old": row.base_code, "new": new_base}
        if new_quote != row.quote_code:
            changes["quote"] = {"old": row.quote_code, "new": new_quote}
        if new_rate != row.rate:
            changes["rate"] = {"old": format_decimal_plain(row.rate), "new": format_decimal_plain(new_rate)}
        if not changes:
            return row
        await self._ensure_free(session, new_date, new_base, new_quote, exclude_id=row.id)
        row.rate_date, row.base_code, row.quote_code, row.rate = new_date, new_base, new_quote, new_rate
        await session.flush()
        await self._audit(
            session,
            AuditEvent.EXCHANGE_RATE_UPDATED,
            actor,
            request,
            {**_rate_details(row), "changes": changes},
            resource_id=row.id,
        )
        return row

    async def delete_rate(
        self, session: AsyncSession, rate_id: int, *, actor: User, request: Request | None = None
    ) -> None:
        """Delete a rate of any source; audited. A fetched rate comes back on the next fetch of its day."""
        row = await self.get_rate(session, rate_id)
        details = _rate_details(row)
        await session.delete(row)
        await session.flush()
        await self._audit(session, AuditEvent.EXCHANGE_RATE_DELETED, actor, request, details, resource_id=rate_id)

    # -- CSV import -----------------------------------------------------------------

    async def import_csv(
        self,
        session: AsyncSession,
        content: str | bytes,
        *,
        actor: User,
        request: Request | None = None,
        dry_run: bool = False,
    ) -> CsvImportResult:
        """Import ``date,base,quote,rate`` lines as ``csv`` rates.

        The header line is optional; blank lines and lines starting with ``#``
        are skipped. Dates are ``YYYY-MM-DD``, codes ISO 4217 codes of enabled
        currencies, rates positive decimals with a ``.`` and at most 12
        decimal places. A file with any error writes nothing and reports every
        bad line. An imported rate already stored for the same day and pair is
        replaced. With *dry_run* nothing is written either way.
        """
        result = CsvImportResult(dry_run=dry_run)
        raw = content.encode() if isinstance(content, str) else content
        if len(raw) > CSV_MAX_BYTES:
            result.errors.append(
                CsvLineError(0, "csv_too_large", f"The file is larger than {CSV_MAX_BYTES // (1024 * 1024)} MB")
            )
            return result
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            result.errors.append(CsvLineError(0, "csv_encoding", "The file is not UTF-8 text"))
            return result

        currencies = {c.code: c.is_active for c in (await session.execute(select(Currency))).scalars().all()}
        rows: list[CsvRow] = []
        seen: dict[tuple[date, str, str], int] = {}
        for line_no, fields in enumerate(csv.reader(io.StringIO(text)), start=1):
            cells = [c.strip() for c in fields]
            if not any(cells) or cells[0].startswith("#"):
                continue
            if not rows and not result.errors and tuple(c.lower() for c in cells) == CSV_HEADER:
                continue
            if len(rows) + len(result.errors) >= CSV_MAX_ROWS:
                result.errors.append(CsvLineError(line_no, "csv_too_many_rows", f"More than {CSV_MAX_ROWS} lines"))
                break
            row_or_error = _parse_csv_line(line_no, cells, currencies)
            if isinstance(row_or_error, CsvLineError):
                result.errors.append(row_or_error)
                continue
            key = (row_or_error.rate_date, row_or_error.base, row_or_error.quote)
            if key in seen:
                result.errors.append(CsvLineError(line_no, "csv_duplicate", f"Same day and pair as line {seen[key]}"))
                continue
            seen[key] = line_no
            rows.append(row_or_error)

        result.rows = len(rows) + len(result.errors)
        if not rows and not result.errors:
            result.errors.append(CsvLineError(0, "csv_empty", "The file holds no rates"))
            return result

        existing = await self._existing_csv_rates(session, rows)
        classified: list[CsvRow] = []
        for row in rows:
            stored = existing.get((row.rate_date, row.base, row.quote))
            action = "create" if stored is None else ("unchanged" if stored == row.rate else "update")
            classified.append(CsvRow(row.line, row.rate_date, row.base, row.quote, row.rate, action))
        result.created = sum(1 for r in classified if r.action == "create")
        result.updated = sum(1 for r in classified if r.action == "update")
        result.unchanged = sum(1 for r in classified if r.action == "unchanged")
        result.preview = classified[:CSV_PREVIEW_ROWS]
        if result.errors or dry_run:
            return result

        to_write = [r for r in classified if r.action != "unchanged"]
        for start in range(0, len(to_write), _WRITE_CHUNK):
            chunk = to_write[start : start + _WRITE_CHUNK]
            stmt = pg_insert(ExchangeRate).values(
                [
                    {
                        "rate_date": r.rate_date,
                        "base_code": r.base,
                        "quote_code": r.quote,
                        "rate": r.rate,
                        "source": "csv",
                        "created_by_id": actor.id,
                    }
                    for r in chunk
                ]
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_exchange_rates_date_pair_source",
                set_={"rate": stmt.excluded.rate, "created_by_id": stmt.excluded.created_by_id},
            )
            await session.execute(stmt)
        await session.flush()
        result.applied = True
        dates = [r.rate_date for r in classified]
        await self._audit(
            session,
            AuditEvent.EXCHANGE_RATES_IMPORTED,
            actor,
            request,
            {
                "rows": len(classified),
                "created": result.created,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "date_from": min(dates).isoformat(),
                "date_to": max(dates).isoformat(),
            },
        )
        return result

    async def _existing_csv_rates(
        self, session: AsyncSession, rows: Sequence[CsvRow]
    ) -> dict[tuple[date, str, str], Decimal]:
        found: dict[tuple[date, str, str], Decimal] = {}
        keys = [(r.rate_date, r.base, r.quote) for r in rows]
        for start in range(0, len(keys), _WRITE_CHUNK):
            chunk = keys[start : start + _WRITE_CHUNK]
            stmt = select(
                ExchangeRate.rate_date, ExchangeRate.base_code, ExchangeRate.quote_code, ExchangeRate.rate
            ).where(
                ExchangeRate.source == "csv",
                tuple_(ExchangeRate.rate_date, ExchangeRate.base_code, ExchangeRate.quote_code).in_(chunk),
            )
            for rate_date, base, quote, rate in (await session.execute(stmt)).all():
                found[(rate_date, base, quote)] = rate
        return found

    # -- optional fetch ---------------------------------------------------------------

    async def fetch_and_store(
        self,
        session: AsyncSession,
        *,
        force: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> FetchResult:
        """Fetch the configured feed and store its rates as ``ecb`` rates.

        Does nothing unless the fetch is enabled, or *force* (an
        administrator's "Fetch now"). Codes the instance does not know are
        skipped. A rate already stored for the day is updated (the ECB
        occasionally corrects one). The outcome is saved for the admin page;
        a failure never raises.
        """
        settings = await self.get_settings(session)
        if not settings.fetch_enabled and not force:
            return FetchResult(ok=False, error="disabled")
        try:
            feed = await fetch_feed(settings.fetch_url, transport=transport)
        except FeedError as exc:
            logger.warning("Exchange rate fetch from %s failed: %s", settings.fetch_url, exc)
            result = FetchResult(ok=False, error=str(exc)[:300])
            await self._save_last_fetch(session, result)
            return result

        known = set((await session.execute(select(Currency.code))).scalars().all())
        values: list[dict[str, Any]] = []
        newest: date | None = None
        skipped = 0
        for item in feed:
            if item.base not in known or item.quote not in known:
                skipped += 1
                continue
            values.append(
                {
                    "rate_date": item.rate_date,
                    "base_code": item.base,
                    "quote_code": item.quote,
                    "rate": item.rate,
                    "source": "ecb",
                    "created_by_id": None,
                }
            )
            newest = item.rate_date if newest is None or item.rate_date > newest else newest
        for start in range(0, len(values), _WRITE_CHUNK):
            stmt = pg_insert(ExchangeRate).values(values[start : start + _WRITE_CHUNK])
            stmt = stmt.on_conflict_do_update(
                constraint="uq_exchange_rates_date_pair_source", set_={"rate": stmt.excluded.rate}
            )
            await session.execute(stmt)
        result = FetchResult(
            ok=True,
            stored=len(values),
            skipped=skipped,
            rate_date=newest,
        )
        await self._save_last_fetch(session, result)
        logger.info("Exchange rate fetch stored %d rates (%d skipped)", result.stored, skipped)
        return result

    async def _save_last_fetch(self, session: AsyncSession, result: FetchResult) -> None:
        value = json.dumps(
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "ok": result.ok,
                "stored": result.stored,
                "skipped": result.skipped,
                "rate_date": result.rate_date.isoformat() if result.rate_date else None,
                "error": result.error,
            }
        )
        stmt = pg_insert(Setting).values(key=LAST_FETCH_SETTING_KEY, value=value)
        await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
        await session.flush()

    # -- conversion -------------------------------------------------------------------

    async def convert(
        self,
        session: AsyncSession,
        money: Money,
        to_currency: str,
        on: date,
        policy: RatePolicy | None = None,
    ) -> ConversionResult:
        """Convert *money* dated *on* into *to_currency*.

        Returns :class:`~specivo.core.exchange.Converted` (the amount, the
        effective rate and its date) or :class:`~specivo.core.exchange.RateMissing`
        (``no_rate`` / ``stale``). *policy* defaults to ``entry_date`` with the
        instance's maximum age.
        """
        return (await self.convert_many(session, [(money, on)], to_currency, policy))[0]

    async def convert_many(
        self,
        session: AsyncSession,
        items: Sequence[tuple[Money, date]],
        to_currency: str,
        policy: RatePolicy | None = None,
    ) -> list[ConversionResult]:
        """Convert every ``(money, date)`` into *to_currency*, in order, with at most two queries."""
        to_code = normalize_currency_code(to_currency)
        settings = await self.get_settings(session)
        policy = policy or RatePolicy.entry_date(settings.max_age_days)
        book = await self.load_rate_book(session, items, to_code, policy, settings.pivot)
        return [book.convert(money, to_code, on, policy, settings.pivot) for money, on in items]

    async def summarize(
        self,
        session: AsyncSession,
        items: Sequence[tuple[Money, date]],
        to_currency: str,
        policy: RatePolicy | None = None,
    ) -> ConversionSummary:
        """The report contract: converted total, unconverted amounts per currency and reason, rates used."""
        to_code = normalize_currency_code(to_currency)
        return summarize(await self.convert_many(session, items, to_code, policy), to_code)

    async def load_rate_book(
        self,
        session: AsyncSession,
        items: Sequence[tuple[Money, date]],
        to_currency: str,
        policy: RatePolicy,
        pivot: str,
    ) -> RateBook:
        """Load the rates converting *items* into *to_currency* needs (pairs through *to_currency* or *pivot*)."""
        sources = {money.currency for money, _ in items} - {to_currency}
        book = RateBook()
        if not sources:
            return book
        involved = sources | {to_currency, pivot}
        hubs = {to_currency, pivot}
        pair_filter = and_(
            ExchangeRate.base_code.in_(involved),
            ExchangeRate.quote_code.in_(involved),
            or_(ExchangeRate.base_code.in_(hubs), ExchangeRate.quote_code.in_(hubs)),
        )
        columns = (
            ExchangeRate.rate_date,
            ExchangeRate.base_code,
            ExchangeRate.quote_code,
            ExchangeRate.rate,
            ExchangeRate.source,
        )
        newest_per_pair = (
            select(*columns)
            .ext(distinct_on(ExchangeRate.base_code, ExchangeRate.quote_code, ExchangeRate.source))
            .order_by(
                ExchangeRate.base_code, ExchangeRate.quote_code, ExchangeRate.source, ExchangeRate.rate_date.desc()
            )
        )
        statements = []
        if policy.kind is PolicyKind.LATEST:
            statements.append(newest_per_pair.where(pair_filter))
        else:
            windows = [policy.window(on) for money, on in items if money.currency != to_currency]
            earliest = min(w[0] for w in windows if w[0] is not None)
            latest = max(w[1] for w in windows if w[1] is not None)
            statements.append(select(*columns).where(pair_filter, ExchangeRate.rate_date.between(earliest, latest)))
            # The newest rate before every window, to tell "stale" from "no rate".
            statements.append(newest_per_pair.where(pair_filter, ExchangeRate.rate_date < earliest))
        for stmt in statements:
            for rate_date, base, quote, rate, source in (await session.execute(stmt)).all():
                book.add(RateObservation(rate_date, base, quote, rate, source))
        return book

    # -- helpers ------------------------------------------------------------------------

    async def _audit(
        self,
        session: AsyncSession,
        event: AuditEvent,
        actor: User | None,
        request: Request | None,
        details: dict[str, Any],
        resource_id: int | None = None,
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_exchange_rate_change(
                session,
                event,
                user_id=actor.id if actor is not None else None,
                details=details,
                resource_id=resource_id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)


def _rate_details(row: ExchangeRate) -> dict[str, Any]:
    return {
        "rate_id": row.id,
        "rate_date": row.rate_date.isoformat(),
        "base": row.base_code,
        "quote": row.quote_code,
        "rate": format_decimal_plain(Decimal(row.rate).normalize()) if row.rate is not None else None,
        "source": row.source,
    }


def _parse_csv_line(line_no: int, cells: list[str], currencies: dict[str, bool]) -> CsvRow | CsvLineError:
    if len(cells) != 4:
        return CsvLineError(line_no, "csv_columns", f"Expected 4 columns (date,base,quote,rate), found {len(cells)}")
    day_text, base_text, quote_text, rate_text = cells
    try:
        rate_date = date.fromisoformat(day_text)
    except ValueError:
        return CsvLineError(line_no, "csv_date", f"{day_text!r} is not a date (YYYY-MM-DD)")
    codes = []
    for text in (base_text, quote_text):
        code = text.upper()
        if code not in currencies:
            return CsvLineError(line_no, "currency_unknown", f"Unknown currency {text!r}")
        if not currencies[code]:
            return CsvLineError(line_no, "currency_inactive", f"Currency {code} is not enabled on this instance")
        codes.append(code)
    if codes[0] == codes[1]:
        return CsvLineError(line_no, "exchange_rate_same_currency", "The two currencies must differ")
    try:
        rate = parse_rate(rate_text)
    except AppError as exc:
        return CsvLineError(line_no, "exchange_rate_invalid", f"{rate_text!r}: {exc.message}")
    except InvalidOperation:
        return CsvLineError(line_no, "exchange_rate_invalid", f"{rate_text!r} is not a number")
    return CsvRow(line_no, rate_date, codes[0], codes[1], rate)


__all__ = [
    "EXCHANGE_RATE_SETTING_KEYS",
    "EXCHANGE_RATE_SOURCES",
    "CsvImportResult",
    "CsvLineError",
    "CsvRow",
    "ExchangeRateService",
    "ExchangeRateSettings",
    "FetchResult",
    "parse_policy",
    "parse_rate",
]
