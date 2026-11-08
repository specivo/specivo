"""AI model prices: the price history, the resolver and the bundled seed.

- **Prices** (``ai_model_prices``) are a history: each row is one model's
  per-1M-token prices in one currency during ``[effective_from,
  effective_to)``. A price is never edited in place. Entering a new price
  "from an instant" closes the open period of the same source at that
  instant and inserts the new one, in one transaction; earlier periods can be
  entered too.
- **Resolution** (:meth:`AiModelPriceService.resolve`) normalises the reported
  model name, matches the model and then its aliases, and picks the active row
  valid at the instant (UTC), an ``admin`` row before a ``seed`` row. No match
  is ``None`` (unpriced): never a zero, never the latest price.
- **Seed** (:meth:`AiModelPriceService.load_seed`) loads the bundled LiteLLM
  snapshot, append-only; see :mod:`specivo.core.ai_pricing_seed`.
- **References.** Rows that usage entries price with cannot be deleted, nor
  have their period end moved before those entries. Tables that reference
  prices register themselves with :func:`register_price_reference`.

Every administrator change is audited (``ai_model_price_*`` events).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import (
    MODEL_NAME_MAX_LENGTH,
    PRICE_MAX_DECIMAL_PLACES,
    PRICE_MAX_INTEGER_DIGITS,
    PROVIDER_RE,
    normalize_model_name,
    normalize_provider,
)
from specivo.core.ai_pricing_seed import PRICE_COLUMNS, HistoryPeriod, Snapshot, format_price
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.ai_model_price import AI_MODEL_PRICE_SOURCES, AiModelPrice
from specivo.services.ai_model_price_seed import SeedOutcome, apply_seed
from specivo.services.currency_service import CurrencyService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    from fastapi import Request

    from specivo.models.user import User

logger = logging.getLogger(__name__)

_audit = SecurityAuditService()

#: A price as people type it: up to 12 digits before and 6 after the point.
_PRICE_RE = re.compile(rf"^\d{{1,{PRICE_MAX_INTEGER_DIGITS}}}(\.\d{{1,{PRICE_MAX_DECIMAL_PLACES}}})?$")
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

MAX_ALIASES = 20
NOTE_MAX_LENGTH = 1000

LONG_PRICE_COLUMNS: tuple[str, ...] = tuple(c for c in PRICE_COLUMNS if c.startswith("long_"))

#: Fields an update may change in place.
MUTABLE_FIELDS: tuple[str, ...] = ("effective_to", "is_active", "aliases", "note")


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


# ---------------------------------------------------------------------------
# References from usage entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceReference:
    """A table whose rows were priced with an ``ai_model_prices`` row.

    ``price_column`` holds the price id, ``instant_column`` the instant the
    row was priced at (``priced_at``).
    """

    table: str
    price_column: str = "price_id"
    instant_column: str = "priced_at"

    def __post_init__(self) -> None:
        for name in (self.table, self.price_column, self.instant_column):
            if not _IDENTIFIER_RE.match(name):
                raise ValueError(f"Not a plain SQL identifier: {name!r}")


_REFERENCES: list[PriceReference] = []


def register_price_reference(reference: PriceReference) -> None:
    """Declare that *reference* points at prices (``ai_usage_entries.price_id`` does)."""
    if reference not in _REFERENCES:
        _REFERENCES.append(reference)


def unregister_price_reference(reference: PriceReference) -> None:
    if reference in _REFERENCES:
        _REFERENCES.remove(reference)


@dataclass(frozen=True, slots=True)
class PriceUsage:
    """How many rows price with one price row, and the latest instant among them."""

    count: int = 0
    latest: datetime | None = None


async def price_usage_of(session: AsyncSession, price_id: int) -> PriceUsage:
    count = 0
    latest: datetime | None = None
    for ref in _REFERENCES:
        row = (
            await session.execute(
                text(
                    f"SELECT count(*), max({ref.instant_column}) FROM {ref.table} "  # noqa: S608 - registered identifiers
                    f"WHERE {ref.price_column} = :price_id"
                ),
                {"price_id": price_id},
            )
        ).one()
        count += int(row[0])
        if row[1] is not None and (latest is None or row[1] > latest):
            latest = row[1]
    return PriceUsage(count=count, latest=latest)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _invalid(message: str, field_name: str, code: str = "ai_model_price_invalid") -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field_name)


def parse_unit_price(value: object, field_name: str) -> Decimal | None:
    """A per-1M price someone typed: a non-negative decimal string, at most 6 decimal places.

    Empty means "not priced" (``None``). Floats are refused.
    """
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise _invalid("Prices must be given as decimal strings", field_name)
    if isinstance(value, Decimal):
        value = format(value, "f")
    text_value = str(value).strip() if isinstance(value, (str, int)) else None
    if text_value is None:
        raise _invalid("Prices must be given as decimal strings", field_name)
    if text_value == "":
        return None
    if "." in text_value:
        head, tail = text_value.split(".", 1)
        tail = tail.rstrip("0")
        text_value = f"{head}.{tail}" if tail else head
    if not _PRICE_RE.match(text_value):
        raise _invalid(
            "Enter a price per 1M tokens as a non-negative decimal number with at most 6 decimal places, such as 3.75",
            field_name,
        )
    return Decimal(text_value)


def as_utc(value: datetime, field_name: str = "at") -> datetime:
    """*value* in UTC; a naive datetime is refused (an instant needs an offset)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invalid("Give the instant with a time zone offset, for example 2026-11-06T00:00:00Z", field_name)
    return value.astimezone(UTC)


def model_name_for(model: str, provider: str) -> str:
    """The normalised model name, also without a leading ``<provider>/`` of the given provider."""
    _hint, name = normalize_model_name(model)
    prefix = f"{provider}/"
    while provider and name.startswith(prefix) and len(name) > len(prefix):
        name = name[len(prefix) :]
    return name


def normalize_aliases(values: object, model: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,]+", values) if v]
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
        raise _invalid("Aliases must be a list of model names", "aliases")
    result: list[str] = []
    for value in values:
        _hint, name = normalize_model_name(value)
        if not name or len(name) > MODEL_NAME_MAX_LENGTH:
            raise _invalid(f"Invalid alias {value!r}", "aliases")
        if name != model and name not in result:
            result.append(name)
    if len(result) > MAX_ALIASES:
        raise _invalid(f"At most {MAX_ALIASES} aliases", "aliases")
    return sorted(result)


@dataclass
class PriceInput:
    """A new price period as an administrator enters it (raw values; validated by the service)."""

    provider: str
    model: str
    currency: str
    effective_from: datetime
    prices: Mapping[str, object] = field(default_factory=dict)
    effective_to: datetime | None = None
    long_context_threshold: int | None = None
    aliases: object = None
    is_local: bool = False
    baseline_model: str | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    """The price valid for a model at an instant."""

    price: AiModelPrice
    matched_by: str  # "model" or "alias"
    model: str  # the normalised name that was looked up
    at: datetime


@dataclass(frozen=True, slots=True)
class CreatedPrice:
    price: AiModelPrice
    closed: AiModelPrice | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AiModelPriceService:
    """Price history, resolution and the bundled seed."""

    # -- reading ----------------------------------------------------------------

    async def get(self, session: AsyncSession, price_id: int) -> AiModelPrice:
        row = await session.get(AiModelPrice, price_id)
        if row is None:
            raise NotFoundError("AI model price not found")
        return row

    async def list_prices(
        self,
        session: AsyncSession,
        *,
        provider: str | None = None,
        q: str | None = None,
        source: str | None = None,
        status: str = "active",
        valid_at: datetime | None = None,
        currency: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AiModelPrice], int]:
        """Price periods filtered and paged; returns ``(page, total)``.

        *q* matches the model name or an alias (substring, case-insensitive).
        *status* is ``active``, ``inactive`` or ``all``. *valid_at* keeps the
        periods containing that instant.
        """
        conditions: list[Any] = []
        if provider:
            conditions.append(AiModelPrice.provider == normalize_provider(provider))
        if q and q.strip():
            needle = f"%{_escape_like(q.strip().lower())}%"
            conditions.append(
                or_(
                    AiModelPrice.model.like(needle, escape="\\"),
                    func.array_to_string(AiModelPrice.aliases, " ").like(needle, escape="\\"),
                )
            )
        if source in AI_MODEL_PRICE_SOURCES:
            conditions.append(AiModelPrice.source == source)
        if status == "active":
            conditions.append(AiModelPrice.is_active.is_(True))
        elif status == "inactive":
            conditions.append(AiModelPrice.is_active.is_(False))
        if currency:
            conditions.append(AiModelPrice.currency_code == currency.strip().upper())
        if valid_at is not None:
            at = as_utc(valid_at, "valid_at")
            conditions.append(_contains(at))
        stmt = select(AiModelPrice)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        ordered = stmt.order_by(
            AiModelPrice.provider,
            AiModelPrice.model,
            AiModelPrice.effective_from.desc(),
            AiModelPrice.source,
            AiModelPrice.id,
        )
        rows = (await session.execute(ordered.offset(offset).limit(limit))).scalars().all()
        return list(rows), int(total)

    async def model_suggestions(self, session: AsyncSession, limit: int = 500) -> list[tuple[str, str]]:
        """``(provider, model)`` of the active price rows, for model pickers; each pair once."""
        rows = await session.execute(
            select(AiModelPrice.provider, AiModelPrice.model)
            .where(AiModelPrice.is_active.is_(True))
            .distinct()
            .order_by(AiModelPrice.provider, AiModelPrice.model)
            .limit(limit)
        )
        return [(provider, model) for provider, model in rows.all()]

    async def providers(self, session: AsyncSession) -> list[str]:
        rows = await session.execute(select(AiModelPrice.provider).distinct().order_by(AiModelPrice.provider))
        return [r[0] for r in rows.all()]

    async def history(self, session: AsyncSession, provider: str, model: str) -> list[AiModelPrice]:
        """Every period of one model, newest first (admin before seed at the same start)."""
        provider_slug = normalize_provider(provider) or ""
        name = model_name_for(model, provider_slug)
        stmt = (
            select(AiModelPrice)
            .where(AiModelPrice.provider == provider_slug, AiModelPrice.model == name)
            .order_by(AiModelPrice.effective_from.desc(), AiModelPrice.source, AiModelPrice.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    # -- resolution -------------------------------------------------------------

    async def resolve(
        self,
        session: AsyncSession,
        model: str,
        at: datetime,
        provider: str | None = None,
    ) -> ResolvedPrice | None:
        """The active price of *model* valid at *at*, or ``None`` (unpriced).

        The name is normalised (case, provider prefixes such as
        ``anthropic/``). An explicit *provider* restricts the match; a prefix in
        the name only breaks ties. The model name matches before an alias, and
        an ``admin`` row before a ``seed`` row. *at* must carry a time zone; it
        is compared in UTC.
        """
        instant = as_utc(at)
        provider_slug = normalize_provider(provider)
        hint, name = normalize_model_name(model)
        if provider_slug:
            name = model_name_for(model, provider_slug)
        if not name:
            return None
        stmt = select(AiModelPrice).where(
            AiModelPrice.is_active.is_(True),
            or_(AiModelPrice.model == name, AiModelPrice.aliases.contains([name])),
            _contains(instant),
        )
        if provider_slug:
            stmt = stmt.where(AiModelPrice.provider == provider_slug)
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return None

        def rank(row: AiModelPrice) -> tuple[int, int, int, str, int]:
            return (
                0 if row.model == name else 1,
                0 if row.source == "admin" else 1,
                0 if hint is not None and row.provider == hint else 1,
                row.provider,
                row.id,
            )

        best = min(rows, key=rank)
        return ResolvedPrice(price=best, matched_by="model" if best.model == name else "alias", model=name, at=instant)

    # -- administrator changes ----------------------------------------------------

    async def _validated(self, session: AsyncSession, data: PriceInput) -> dict[str, Any]:
        provider = normalize_provider(data.provider) or ""
        if not PROVIDER_RE.match(provider):
            raise _invalid(
                "Enter a provider such as anthropic or openai (lower case letters, digits, . _ -)", "provider"
            )
        model = model_name_for(data.model or "", provider)
        if not model or len(model) > MODEL_NAME_MAX_LENGTH:
            raise _invalid(f"Enter a model name of at most {MODEL_NAME_MAX_LENGTH} characters", "model")
        currency = (await CurrencyService().require_selectable(session, data.currency, field="currency")).code
        unknown = set(data.prices) - set(PRICE_COLUMNS)
        if unknown:
            raise _invalid(f"Unknown price fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        prices = {column: parse_unit_price(data.prices.get(column), column) for column in PRICE_COLUMNS}
        for required in ("input", "output"):
            if prices[required] is None:
                raise _invalid("Input and output prices are required", required)
        threshold = data.long_context_threshold
        if threshold is not None and (isinstance(threshold, bool) or threshold <= 0):
            raise _invalid("The long context threshold must be a positive number of tokens", "long_context_threshold")
        if threshold is None and any(prices[c] is not None for c in LONG_PRICE_COLUMNS):
            raise _invalid("Long context prices need a long context threshold", "long_context_threshold")
        start = as_utc(data.effective_from, "effective_from")
        end = as_utc(data.effective_to, "effective_to") if data.effective_to is not None else None
        if end is not None and end <= start:
            raise _invalid("The end of a period must be after its start", "effective_to")
        baseline: str | None = None
        if data.baseline_model and data.baseline_model.strip():
            baseline = normalize_model_name(data.baseline_model)[1][:MODEL_NAME_MAX_LENGTH]
        note = (data.note or "").strip() or None
        if note is not None and len(note) > NOTE_MAX_LENGTH:
            raise _invalid(f"A note can have at most {NOTE_MAX_LENGTH} characters", "note")
        return {
            "provider": provider,
            "model": model,
            "currency_code": currency,
            **prices,
            "long_context_threshold": threshold,
            "aliases": normalize_aliases(data.aliases, model),
            "is_local": bool(data.is_local),
            "baseline_model": baseline,
            "effective_from": start,
            "effective_to": end,
            "note": note,
        }

    async def create(
        self,
        session: AsyncSession,
        data: PriceInput,
        *,
        actor: User,
        request: Request | None = None,
    ) -> CreatedPrice:
        """Add an administrator price period; audited.

        An open-ended period (no ``effective_to``) starting after the open
        admin period of the same model closes that one at its start: "new price
        from <instant>". Any other overlap with an active admin period is a
        409 ``ai_model_price_overlap``. Closing a period before usage entries
        priced with it is a 409 ``ai_model_price_in_use``.
        """
        values = await self._validated(session, data)
        start: datetime = values["effective_from"]
        end: datetime | None = values["effective_to"]
        existing = list(
            (
                await session.execute(
                    select(AiModelPrice)
                    .where(
                        AiModelPrice.provider == values["provider"],
                        AiModelPrice.model == values["model"],
                        AiModelPrice.source == "admin",
                        AiModelPrice.is_active.is_(True),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        to_close: AiModelPrice | None = None
        conflicts: list[AiModelPrice] = []
        for row in existing:
            if not _periods_overlap(row.effective_from, row.effective_to, start, end):
                continue
            if end is None and row.effective_to is None and row.effective_from < start:
                to_close = row
            else:
                conflicts.append(row)
        if conflicts:
            raise AppError(
                code="ai_model_price_overlap",
                message="The period overlaps another active price of this model. "
                "Deactivate or shorten that price first, or choose another period.",
                status_code=409,
                field="effective_from",
                details={"conflicting_price_ids": [r.id for r in conflicts]},
            )
        if to_close is not None:
            await self._ensure_end_allowed(session, to_close, start)

        row = AiModelPrice(**values, source="admin", is_active=True, created_by_id=actor.id)
        # Close and insert together, in a savepoint: a concurrent overlapping
        # insert (the exclusion constraint) undoes both and leaves the
        # request's transaction usable.
        # An explicit savepoint rather than ``async with``: ending it must leave
        # the session free to begin another one straight away.
        savepoint = await session.begin_nested()
        try:
            if to_close is not None:
                to_close.effective_to = start
                await session.flush()
            session.add(row)
            await session.flush()
            if to_close is not None:
                to_close.superseded_by_id = row.id
                await session.flush()
        except IntegrityError as exc:
            await savepoint.rollback()
            raise _integrity_error(exc) from None
        if savepoint.is_active:
            await savepoint.commit()
        details = price_details(row)
        if to_close is not None:
            details["closed_price_id"] = to_close.id
        await self._audit(session, AuditEvent.AI_MODEL_PRICE_CREATED, actor, request, details, resource_id=row.id)
        return CreatedPrice(price=row, closed=to_close)

    async def update(
        self,
        session: AsyncSession,
        price_id: int,
        *,
        actor: User,
        request: Request | None = None,
        effective_to: datetime | None = UNSET,
        is_active: bool | None = None,
        aliases: object = UNSET,
        note: str | None = UNSET,
    ) -> AiModelPrice:
        """Change what may change in place: the period end, the active flag, aliases and the note; audited.

        Prices, the currency and the start are never edited (enter a new
        price instead). Moving the end before an entry priced with this row
        is a 409 ``ai_model_price_in_use``; an end that makes the period
        overlap another, or reactivating an overlapping row, is a 409
        ``ai_model_price_overlap``.
        """
        row = await self.get(session, price_id)
        changes: dict[str, dict[str, Any]] = {}
        values: dict[str, Any] = {}
        if effective_to is not UNSET:
            new_end = as_utc(effective_to, "effective_to") if effective_to is not None else None
            if new_end is not None and new_end <= row.effective_from:
                raise _invalid("The end of a period must be after its start", "effective_to")
            if new_end != row.effective_to:
                if new_end is not None:
                    await self._ensure_end_allowed(session, row, new_end)
                changes["effective_to"] = {"old": _iso(row.effective_to), "new": _iso(new_end)}
                values["effective_to"] = new_end
        if is_active is not None and is_active != row.is_active:
            changes["is_active"] = {"old": row.is_active, "new": is_active}
            values["is_active"] = is_active
        if aliases is not UNSET:
            new_aliases = normalize_aliases(aliases, row.model)
            if new_aliases != list(row.aliases or []):
                changes["aliases"] = {"old": list(row.aliases or []), "new": new_aliases}
                values["aliases"] = new_aliases
        if note is not UNSET:
            new_note = (note or "").strip() or None
            if new_note is not None and len(new_note) > NOTE_MAX_LENGTH:
                raise _invalid(f"A note can have at most {NOTE_MAX_LENGTH} characters", "note")
            if new_note != row.note:
                changes["note"] = {"old": row.note, "new": new_note}
                values["note"] = new_note
        if not changes:
            return row
        # Change the row only inside the savepoint (beginning one flushes the
        # session), so a refused change leaves the transaction usable.
        savepoint = await session.begin_nested()
        try:
            for name, value in values.items():
                setattr(row, name, value)
            await session.flush()
        except IntegrityError as exc:
            await savepoint.rollback()
            raise _integrity_error(exc) from None
        if savepoint.is_active:
            await savepoint.commit()
        await self._audit(
            session,
            AuditEvent.AI_MODEL_PRICE_UPDATED,
            actor,
            request,
            {**price_details(row), "changes": changes},
            resource_id=row.id,
        )
        return row

    async def delete(
        self, session: AsyncSession, price_id: int, *, actor: User, request: Request | None = None
    ) -> None:
        """Delete an administrator price period no usage entry references; audited.

        Seed periods are deactivated instead (a later seed run would bring a
        deleted one back), and so are referenced periods (409
        ``ai_model_price_in_use``).
        """
        row = await self.get(session, price_id)
        if row.source != "admin":
            raise AppError(
                code="ai_model_price_not_deletable",
                message="Bundled prices cannot be deleted; deactivate the price instead",
                status_code=422,
            )
        usage = await price_usage_of(session, row.id)
        if usage.count:
            raise AppError(
                code="ai_model_price_in_use",
                message=f"{usage.count} usage entries are priced with this price; deactivate it instead",
                status_code=409,
            )
        details = price_details(row)
        successors = (
            (await session.execute(select(AiModelPrice).where(AiModelPrice.superseded_by_id == row.id))).scalars().all()
        )
        for predecessor in successors:
            predecessor.superseded_by_id = None
        await session.delete(row)
        await session.flush()
        await self._audit(session, AuditEvent.AI_MODEL_PRICE_DELETED, actor, request, details, resource_id=price_id)

    async def _ensure_end_allowed(self, session: AsyncSession, row: AiModelPrice, end: datetime) -> None:
        usage = await price_usage_of(session, row.id)
        if usage.latest is not None and usage.latest >= end:
            raise AppError(
                code="ai_model_price_in_use",
                message="Usage entries priced with this price lie at or after that instant. "
                "Enter the new price from a later instant, or recompute those entries first.",
                status_code=409,
                field="effective_to",
                details={"latest_priced_at": usage.latest.isoformat()},
            )

    # -- seed -----------------------------------------------------------------------

    async def load_seed(
        self,
        session: AsyncSession,
        *,
        actor: User | None = None,
        request: Request | None = None,
        snapshot: Snapshot | None = None,
        history: tuple[HistoryPeriod, ...] | None = None,
    ) -> SeedOutcome:
        """Load the bundled prices (append-only, idempotent); audited when something changed."""
        outcome = await session.run_sync(
            lambda sync_session: apply_seed(sync_session.connection(), snapshot=snapshot, history=history)
        )
        # The seed wrote through Core: expire loaded price rows (only those, so
        # the caller's other objects stay loaded).
        for obj in list(session.identity_map.values()):
            if isinstance(obj, AiModelPrice):
                session.expire(obj)
        if outcome.changed:
            await self._audit(
                session, AuditEvent.AI_MODEL_PRICES_SEEDED, actor, request, outcome.details(), resource_id=None
            )
        await self._reprice_backfills(session, outcome, actor)
        return outcome

    @staticmethod
    async def _reprice_backfills(session: AsyncSession, outcome: SeedOutcome, actor: User | None) -> None:
        """Re-price stored entries a seed run just gave a price (before a model's first period's old start).

        Entries keep the price (or its absence) they were stored with, so the
        entries of each backfilled model dated before its old start are priced
        again, each at its own instant. Models without such entries cost one
        count query.
        """
        if not outcome.backfills:
            return
        from specivo.core.ai_pricing_seed import FIRST_PERIOD_START
        from specivo.services.ai_usage_service import AiUsageService

        usage = AiUsageService()
        for backfill in outcome.backfills:
            if backfill.until <= FIRST_PERIOD_START:
                continue
            found = await usage.count_in_range(
                session, start=FIRST_PERIOD_START, end=backfill.until, model=backfill.model
            )
            if not found:
                continue
            await usage.recompute_range(
                session,
                actor_id=actor.id if actor is not None else None,
                start=FIRST_PERIOD_START,
                end=backfill.until,
                model=backfill.model,
            )

    # -- helpers ----------------------------------------------------------------------

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
            await _audit.log_ai_model_price_change(
                session,
                event,
                user_id=actor.id if actor is not None else None,
                details=details,
                resource_id=resource_id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)


def _contains(at: datetime) -> Any:
    return and_(
        AiModelPrice.effective_from <= at,
        or_(AiModelPrice.effective_to.is_(None), AiModelPrice.effective_to > at),
    )


def _periods_overlap(a_start: datetime, a_end: datetime | None, b_start: datetime, b_end: datetime | None) -> bool:
    return (a_end is None or a_end > b_start) and (b_end is None or a_start < b_end)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _integrity_error(exc: IntegrityError) -> AppError:
    message = str(exc.orig) if exc.orig is not None else str(exc)
    if "ex_ai_model_prices_no_overlap" in message:
        return AppError(
            code="ai_model_price_overlap",
            message="The period overlaps another active price of this model from the same source",
            status_code=409,
        )
    if "ai_model_prices_immutable" in message:
        return AppError(
            code="ai_model_price_immutable",
            message="Prices are never edited in place; enter a new price instead",
            status_code=422,
        )
    return AppError(code="ai_model_price_invalid", message="The price could not be saved", status_code=422)


def price_details(row: AiModelPrice) -> dict[str, Any]:
    """Audit details of a price row (ids, names, codes and amounts)."""
    return {
        "price_id": row.id,
        "provider": row.provider,
        "model": row.model,
        "currency": row.currency_code,
        "effective_from": _iso(row.effective_from),
        "effective_to": _iso(row.effective_to),
        "source": row.source,
        "is_active": row.is_active,
        "prices": {
            column: format_price(getattr(row, column)) for column in PRICE_COLUMNS if getattr(row, column) is not None
        },
    }
