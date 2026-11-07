"""The bundled AI model price seed: its file formats, the LiteLLM conversion and the append-only plan.

Two committed files feed ``ai_model_prices`` rows of source ``seed``:

- ``specivo/data/ai_model_prices.json`` -- a trimmed snapshot of LiteLLM's
  ``model_prices_and_context_window.json`` (MIT, Copyright (c) 2023 Berri AI),
  written by ``scripts/refresh_ai_model_prices.py`` with
  :func:`litellm_to_snapshot`. LiteLLM holds current prices only, so the
  snapshot carries the instant it describes (``as_of``).
- ``specivo/data/ai_model_prices_history.json`` -- optional, maintained by
  hand: known earlier periods (``effective_from`` and ``effective_to``), such as
  a provider's price before a cut.

:func:`plan_seed` compares them with the seed rows already stored and returns
what to write. It **appends periods and never rewrites them**:

- a model's **first** period (no seed row, no admin row before ``as_of`` and no
  period in the history file) starts at :data:`FIRST_PERIOD_START`, not at
  ``as_of``: LiteLLM lists current prices, and usage logged before the
  snapshot's date is priced with them rather than left unpriced;
- any other model without an open seed period is inserted from ``as_of``;
- an unchanged price is a no-op;
- a changed price closes the open seed period at ``as_of`` and opens a new one
  from ``as_of``;
- a history period is inserted unless a seed period already covers part of it;
- a deactivated open period, and every ``admin`` row, is left alone;
- a model that disappeared from the snapshot keeps its open period (LiteLLM
  dropping a model does not end its price);
- the earliest seed row of a model that a release before this rule stored from
  an ``as_of`` is moved back to :data:`FIRST_PERIOD_START` (a *rebase*), unless
  an earlier seed or admin row exists or the history file has periods for the
  model.

Prices in both files are strings, per one million tokens. A missing one-hour
cache write price becomes the providers' published one-hour cache write rate,
twice the input price, in both tiers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from specivo.core.ai_pricing import (
    CACHE_WRITE_1H_INPUT_MULTIPLIER,
    MODEL_NAME_MAX_LENGTH,
    ONE_MILLION,
    PRICE_MAX_DECIMAL_PLACES,
    PRICE_MAX_INTEGER_DIGITS,
    PROVIDER_RE,
    normalize_model_name,
)
from specivo.core.money import normalize_currency_code

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_PATH = DATA_DIR / "ai_model_prices.json"
HISTORY_PATH = DATA_DIR / "ai_model_prices_history.json"

SNAPSHOT_FORMAT = 1

#: Where a model's first bundled price period starts: early enough to cover any
#: usage, so the first known price also prices what was logged before the
#: snapshot's ``as_of``.
FIRST_PERIOD_START = datetime(2000, 1, 1, tzinfo=UTC)

#: Every price column of ``ai_model_prices``, base tier then long-context tier.
PRICE_COLUMNS: tuple[str, ...] = (
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "reasoning",
    "long_input",
    "long_output",
    "long_cache_write_5m",
    "long_cache_write_1h",
    "long_cache_read",
)

#: LiteLLM providers kept in the snapshot: the first-party APIs coding agents
#: talk to, mapped to Specivo's provider slugs. Resellers and clouds that mirror
#: these models (Bedrock, Vertex, Azure, OpenRouter...) are left out; an
#: administrator prices them when needed.
LITELLM_PROVIDERS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "gemini",
    "xai": "xai",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "moonshot": "moonshot",
    "zai": "zai",
    "dashscope": "dashscope",
}

#: LiteLLM modes of text models (``responses`` covers models served only on
#: OpenAI's Responses API, such as the Codex models).
LITELLM_MODES = frozenset({"chat", "responses"})

_ABOVE_RE = re.compile(r"^input_cost_per_token_above_(\d+)k_tokens$")


class SeedFormatError(ValueError):
    """A seed file does not have the expected shape."""


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def parse_price(value: object, *, where: str) -> Decimal | None:
    """A per-1M price from a seed file: a non-negative decimal string, or ``None``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SeedFormatError(f"{where}: prices must be decimal strings, got {value!r}")
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation:
        raise SeedFormatError(f"{where}: not a decimal price: {value!r}") from None
    if not price.is_finite() or price < 0:
        raise SeedFormatError(f"{where}: a price must be a non-negative number, got {value!r}")
    if price != price.quantize(Decimal(1).scaleb(-PRICE_MAX_DECIMAL_PLACES)):
        raise SeedFormatError(f"{where}: at most {PRICE_MAX_DECIMAL_PLACES} decimal places, got {value!r}")
    if price >= Decimal(10) ** PRICE_MAX_INTEGER_DIGITS:
        raise SeedFormatError(f"{where}: price too large: {value!r}")
    return price


def parse_instant(value: object, *, where: str) -> datetime:
    """An ISO 8601 instant with an offset (``2026-11-06T06:29:17Z``), as UTC."""
    if not isinstance(value, str):
        raise SeedFormatError(f"{where}: expected an ISO 8601 timestamp, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise SeedFormatError(f"{where}: not an ISO 8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise SeedFormatError(f"{where}: the timestamp needs a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def format_price(value: Decimal | None) -> str | None:
    """A price as a plain decimal string without trailing zeros (``"3"``, ``"0.075"``)."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


def format_instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Seed prices
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedPrice:
    """One model's prices as a seed file states them."""

    provider: str
    model: str
    currency: str
    prices: Mapping[str, Decimal | None]
    long_context_threshold: int | None = None
    aliases: tuple[str, ...] = ()
    note: str | None = None

    def row_prices(self) -> dict[str, Decimal | None]:
        """The prices to store: the stated ones, plus the one-hour cache write rule.

        A missing ``cache_write_1h`` is twice ``input``; a missing
        ``long_cache_write_1h`` is twice ``long_input`` when that is set.
        """
        values = {column: self.prices.get(column) for column in PRICE_COLUMNS}
        if values["cache_write_1h"] is None and values["input"] is not None:
            values["cache_write_1h"] = values["input"] * CACHE_WRITE_1H_INPUT_MULTIPLIER
        if values["long_cache_write_1h"] is None and values["long_input"] is not None:
            values["long_cache_write_1h"] = values["long_input"] * CACHE_WRITE_1H_INPUT_MULTIPLIER
        return values


@dataclass(frozen=True, slots=True)
class HistoryPeriod:
    """A closed historical period of one model's prices."""

    price: SeedPrice
    effective_from: datetime
    effective_to: datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The parsed bundled snapshot."""

    as_of: datetime
    source: Mapping[str, Any]
    prices: tuple[SeedPrice, ...]


def _seed_price(item: object, currency: str, where: str) -> SeedPrice:
    if not isinstance(item, dict):
        raise SeedFormatError(f"{where}: expected an object")
    provider = item.get("provider")
    model = item.get("model")
    if not isinstance(provider, str) or not PROVIDER_RE.match(provider):
        raise SeedFormatError(f"{where}: invalid provider {provider!r}")
    if not isinstance(model, str) or not model.strip():
        raise SeedFormatError(f"{where}: invalid model {model!r}")
    _hint, normalized = normalize_model_name(model)
    if normalized != model or len(model) > MODEL_NAME_MAX_LENGTH:
        raise SeedFormatError(f"{where}: model names must be normalised (lower case, no provider prefix): {model!r}")
    prices = {column: parse_price(item.get(column), where=f"{where} {model} {column}") for column in PRICE_COLUMNS}
    if prices["input"] is None or prices["output"] is None:
        raise SeedFormatError(f"{where}: {model} needs input and output prices")
    threshold = item.get("long_context_threshold")
    if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0):
        raise SeedFormatError(f"{where}: {model} long_context_threshold must be a positive integer")
    if threshold is None and any(prices[c] is not None for c in PRICE_COLUMNS if c.startswith("long_")):
        raise SeedFormatError(f"{where}: {model} has long-context prices without a long_context_threshold")
    aliases = item.get("aliases") or []
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise SeedFormatError(f"{where}: {model} aliases must be a list of strings")
    note = item.get("note")
    item_currency = item.get("currency", currency)
    try:
        code = normalize_currency_code(item_currency)
    except ValueError:
        raise SeedFormatError(f"{where}: {model} has an unknown currency {item_currency!r}") from None
    return SeedPrice(
        provider=provider,
        model=model,
        currency=code,
        prices=prices,
        long_context_threshold=threshold,
        aliases=tuple(sorted({normalize_model_name(a)[1] for a in aliases if a.strip()} - {model})),
        note=note if isinstance(note, str) and note.strip() else None,
    )


def parse_snapshot(document: object) -> Snapshot:
    """Validate and parse the snapshot JSON document."""
    if not isinstance(document, dict):
        raise SeedFormatError("The snapshot must be a JSON object")
    if document.get("format") != SNAPSHOT_FORMAT:
        raise SeedFormatError(f"Unsupported snapshot format {document.get('format')!r}")
    as_of = parse_instant(document.get("as_of"), where="as_of")
    currency = document.get("currency", "USD")
    models = document.get("models")
    if not isinstance(models, list):
        raise SeedFormatError("The snapshot needs a list of models")
    prices = tuple(_seed_price(item, currency, f"models[{i}]") for i, item in enumerate(models))
    seen: set[tuple[str, str]] = set()
    for price in prices:
        key = (price.provider, price.model)
        if key in seen:
            raise SeedFormatError(f"Duplicate model {price.provider}/{price.model} in the snapshot")
        seen.add(key)
    source = document.get("source") or {}
    return Snapshot(as_of=as_of, source=source if isinstance(source, dict) else {}, prices=prices)


def parse_history(document: object) -> tuple[HistoryPeriod, ...]:
    """Validate and parse the history JSON document (``{"periods": [...]}``)."""
    if not isinstance(document, dict):
        raise SeedFormatError("The history file must be a JSON object")
    currency = document.get("currency", "USD")
    periods = document.get("periods") or []
    if not isinstance(periods, list):
        raise SeedFormatError("The history file needs a list of periods")
    result: list[HistoryPeriod] = []
    for i, item in enumerate(periods):
        where = f"periods[{i}]"
        price = _seed_price(item, currency, where)
        start = parse_instant(item.get("effective_from"), where=f"{where} effective_from")
        end = parse_instant(item.get("effective_to"), where=f"{where} effective_to")
        if end <= start:
            raise SeedFormatError(f"{where}: effective_to must be after effective_from")
        result.append(HistoryPeriod(price=price, effective_from=start, effective_to=end))
    by_model: dict[tuple[str, str], list[HistoryPeriod]] = {}
    for period in result:
        by_model.setdefault((period.price.provider, period.price.model), []).append(period)
    for (provider, model), items in by_model.items():
        items.sort(key=lambda p: p.effective_from)
        for before, after in zip(items, items[1:], strict=False):
            if after.effective_from < before.effective_to:
                raise SeedFormatError(f"History periods of {provider}/{model} overlap")
    return tuple(result)


def load_snapshot(path: Path = SNAPSHOT_PATH) -> Snapshot:
    with path.open(encoding="utf-8") as fh:
        return parse_snapshot(json.load(fh))


def load_history(path: Path = HISTORY_PATH) -> tuple[HistoryPeriod, ...]:
    """The history periods; none when the optional file does not exist."""
    if not path.exists():
        return ()
    with path.open(encoding="utf-8") as fh:
        return parse_history(json.load(fh))


# ---------------------------------------------------------------------------
# LiteLLM conversion (used by scripts/refresh_ai_model_prices.py)
# ---------------------------------------------------------------------------


@dataclass
class ConversionReport:
    """What :func:`litellm_to_snapshot` kept, skipped and rounded."""

    kept: int = 0
    skipped_duplicates: list[str] = field(default_factory=list)
    rounded: list[str] = field(default_factory=list)


def _per_million(value: object, key: str, report: ConversionReport) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        per_token = Decimal(str(value))
    except InvalidOperation:
        return None
    if not per_token.is_finite() or per_token < 0:
        return None
    per_million = per_token * ONE_MILLION
    places = Decimal(1).scaleb(-PRICE_MAX_DECIMAL_PLACES)
    rounded = per_million.quantize(places, rounding=ROUND_HALF_UP)
    if rounded != per_million:
        report.rounded.append(key)
    return format_price(rounded)


def _long_threshold(entry: Mapping[str, Any]) -> int | None:
    thresholds = sorted(int(m.group(1)) for k in entry if (m := _ABOVE_RE.match(k)) and entry.get(k) is not None)
    return thresholds[0] if thresholds else None


def litellm_entry(key: str, entry: Mapping[str, Any], report: ConversionReport) -> dict[str, Any] | None:
    """One snapshot model from one LiteLLM entry, or ``None`` when it is not kept.

    Kept: text models (``chat`` or ``responses``) of :data:`LITELLM_PROVIDERS`
    with both an input and an output price, except fine-tunes (``ft:``). Prices
    become per 1M tokens (rounded half up to six places, which is reported).
    The long-context tier is the lowest ``*_above_<N>k_tokens`` tier, with its
    threshold at N × 1000 tokens. Batch, flex and priority prices are not kept.
    """
    provider = LITELLM_PROVIDERS.get(str(entry.get("litellm_provider") or ""))
    if provider is None or entry.get("mode") not in LITELLM_MODES:
        return None
    hint, model = normalize_model_name(key)
    if not model or model.startswith("ft:") or len(model) > MODEL_NAME_MAX_LENGTH:
        return None
    if hint is not None and hint != provider:
        return None

    def price(name: str) -> str | None:
        return _per_million(entry.get(name), f"{key}:{name}", report)

    item: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "input": price("input_cost_per_token"),
        "output": price("output_cost_per_token"),
        "cache_write_5m": price("cache_creation_input_token_cost"),
        "cache_write_1h": price("cache_creation_input_token_cost_above_1hr"),
        "cache_read": price("cache_read_input_token_cost"),
        "reasoning": price("output_cost_per_reasoning_token"),
    }
    if item["input"] is None or item["output"] is None:
        return None
    threshold = _long_threshold(entry)
    if threshold is not None:
        suffix = f"above_{threshold}k_tokens"
        item["long_context_threshold"] = threshold * 1000
        item["long_input"] = price(f"input_cost_per_token_{suffix}")
        item["long_output"] = price(f"output_cost_per_token_{suffix}")
        item["long_cache_write_5m"] = price(f"cache_creation_input_token_cost_{suffix}")
        item["long_cache_write_1h"] = price(f"cache_creation_input_token_cost_above_1hr_{suffix}")
        item["long_cache_read"] = price(f"cache_read_input_token_cost_{suffix}")
    return {k: v for k, v in item.items() if v is not None}


def litellm_to_snapshot(
    raw: Mapping[str, Any], *, as_of: datetime, source: Mapping[str, Any]
) -> tuple[dict[str, Any], ConversionReport]:
    """The snapshot document for LiteLLM's price file *raw*, sorted by provider and model.

    When two LiteLLM keys name the same model of a provider (``deepseek-chat``
    and ``deepseek/deepseek-chat``), the key that is already the plain model
    name wins and the other is reported as a duplicate.
    """
    report = ConversionReport()
    chosen: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for key in sorted(raw):
        entry = raw[key]
        if not isinstance(entry, Mapping):
            continue
        item = litellm_entry(key, entry, report)
        if item is None:
            continue
        identity = (item["provider"], item["model"])
        previous = chosen.get(identity)
        if previous is not None:
            plain_wins = key.lower() == item["model"] and previous[0].lower() != item["model"]
            loser = previous[0] if plain_wins else key
            report.skipped_duplicates.append(loser)
            if not plain_wins:
                continue
        chosen[identity] = (key, item)
    models = [item for _identity, (_key, item) in sorted(chosen.items())]
    report.kept = len(models)
    document = {
        "format": SNAPSHOT_FORMAT,
        "as_of": format_instant(as_of),
        "currency": "USD",
        "unit": "per 1M tokens",
        "source": dict(source),
        "models": models,
    }
    return document, report


# ---------------------------------------------------------------------------
# The append-only plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredSeedRow:
    """A ``source = 'seed'`` row as the planner needs it."""

    id: int
    provider: str
    model: str
    currency: str
    prices: Mapping[str, Decimal | None]
    long_context_threshold: int | None
    aliases: tuple[str, ...]
    effective_from: datetime
    effective_to: datetime | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class PlannedRow:
    """A seed row to insert."""

    price: SeedPrice
    effective_from: datetime
    effective_to: datetime | None
    #: The open row this one supersedes (closed at ``effective_from``).
    supersedes_id: int | None = None
    from_history: bool = False

    def values(self) -> dict[str, Any]:
        """Column values for ``ai_model_prices``."""
        return {
            "provider": self.price.provider,
            "model": self.price.model,
            "aliases": list(self.price.aliases),
            "currency_code": self.price.currency,
            **self.price.row_prices(),
            "long_context_threshold": self.price.long_context_threshold,
            "is_local": False,
            "baseline_model": None,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "source": "seed",
            "is_active": True,
            "note": self.price.note,
        }


@dataclass(frozen=True, slots=True)
class Backfill:
    """A model whose usage before *until* gained a price in this run (entries there need re-pricing)."""

    provider: str
    model: str
    until: datetime


@dataclass
class SeedPlan:
    """What a seed run writes: rebases and closes first (``{row id: instant}``), then inserts."""

    as_of: datetime
    inserts: list[PlannedRow] = field(default_factory=list)
    closes: dict[int, datetime] = field(default_factory=dict)
    #: Seed rows whose ``effective_from`` moves back to :data:`FIRST_PERIOD_START`.
    rebases: dict[int, datetime] = field(default_factory=dict)
    alias_updates: dict[int, tuple[str, ...]] = field(default_factory=dict)
    backfills: list[Backfill] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.inserts or self.closes or self.rebases or self.alias_updates)


def same_price(row: StoredSeedRow, price: SeedPrice) -> bool:
    """Whether a stored row holds exactly the prices *price* would store."""
    wanted = price.row_prices()
    if row.currency != price.currency or row.long_context_threshold != price.long_context_threshold:
        return False
    for column in PRICE_COLUMNS:
        stored, new = row.prices.get(column), wanted[column]
        if (stored is None) != (new is None):
            return False
        if stored is not None and new is not None and stored != new:
            return False
    return True


def _overlaps(start: datetime, end: datetime | None, row: StoredSeedRow) -> bool:
    row_end_after_start = row.effective_to is None or row.effective_to > start
    starts_before_end = end is None or row.effective_from < end
    return row_end_after_start and starts_before_end


def plan_seed(
    stored: Iterable[StoredSeedRow],
    snapshot: Snapshot,
    history: Iterable[HistoryPeriod] = (),
    admin_starts: Mapping[tuple[str, str], datetime] | None = None,
) -> SeedPlan:
    """Compare the stored seed rows with the bundled files; see the module docstring for the rules.

    *admin_starts* maps ``(provider, model)`` to the earliest ``effective_from``
    of its active admin rows: a model priced by an administrator before a
    bundled period keeps that period's start.
    """
    as_of = snapshot.as_of
    history = tuple(history)
    admin_starts = admin_starts or {}
    plan = SeedPlan(as_of=as_of)
    by_model: dict[tuple[str, str], list[StoredSeedRow]] = {}
    for row in stored:
        by_model.setdefault((row.provider, row.model), []).append(row)
    history_models = {(p.price.provider, p.price.model) for p in history}

    for key, rows in sorted(by_model.items()):
        first = min(rows, key=lambda r: (r.effective_from, r.id))
        admin_start = admin_starts.get(key)
        if (
            key in history_models
            or not first.is_active
            or first.effective_from <= FIRST_PERIOD_START
            or (admin_start is not None and admin_start < first.effective_from)
        ):
            continue
        plan.rebases[first.id] = FIRST_PERIOD_START
        plan.backfills.append(Backfill(provider=key[0], model=key[1], until=first.effective_from))

    for price in snapshot.prices:
        key = (price.provider, price.model)
        rows = by_model.get(key, [])
        open_rows = sorted((r for r in rows if r.effective_to is None), key=lambda r: r.effective_from)
        current = open_rows[-1] if open_rows else None
        name = f"{price.provider}/{price.model}"
        if current is None:
            if any(_overlaps(as_of, None, r) for r in rows if r.is_active):
                plan.skipped.append(f"{name}: a seed period already covers {format_instant(as_of)}")
                continue
            admin_start = admin_starts.get(key)
            first_known = not rows and key not in history_models and (admin_start is None or admin_start >= as_of)
            start = FIRST_PERIOD_START if first_known and as_of > FIRST_PERIOD_START else as_of
            plan.inserts.append(PlannedRow(price=price, effective_from=start, effective_to=None))
            if start < as_of:
                plan.backfills.append(Backfill(provider=price.provider, model=price.model, until=as_of))
            continue
        if not current.is_active:
            plan.unchanged += 1
            continue
        if same_price(current, price):
            plan.unchanged += 1
            if tuple(current.aliases) != price.aliases:
                plan.alias_updates[current.id] = price.aliases
            continue
        if current.effective_from >= as_of:
            plan.skipped.append(f"{name}: the stored seed price starts at or after the snapshot's as_of")
            continue
        plan.closes[current.id] = as_of
        plan.inserts.append(PlannedRow(price=price, effective_from=as_of, effective_to=None, supersedes_id=current.id))

    for period in history:
        price = period.price
        name = f"{price.provider}/{price.model}"
        rows = by_model.get((price.provider, price.model), [])
        exact = [
            r
            for r in rows
            if r.effective_from == period.effective_from
            and r.effective_to == period.effective_to
            and same_price(r, price)
        ]
        if exact:
            plan.unchanged += 1
            continue
        planned = [p for p in plan.inserts if (p.price.provider, p.price.model) == (price.provider, price.model)]
        clash_stored = any(
            _overlaps(period.effective_from, period.effective_to, _with_close(r, plan.closes))
            for r in rows
            if r.is_active
        )
        clash_planned = any(
            (p.effective_to is None or p.effective_to > period.effective_from)
            and p.effective_from < period.effective_to
            for p in planned
        )
        if clash_stored or clash_planned:
            plan.skipped.append(
                f"{name}: the history period from {format_instant(period.effective_from)} overlaps a seed period"
            )
            continue
        plan.inserts.append(
            PlannedRow(
                price=price,
                effective_from=period.effective_from,
                effective_to=period.effective_to,
                from_history=True,
            )
        )
    return plan


def _with_close(row: StoredSeedRow, closes: Mapping[int, datetime]) -> StoredSeedRow:
    if row.id not in closes:
        return row
    return StoredSeedRow(
        id=row.id,
        provider=row.provider,
        model=row.model,
        currency=row.currency,
        prices=row.prices,
        long_context_threshold=row.long_context_threshold,
        aliases=row.aliases,
        effective_from=row.effective_from,
        effective_to=closes[row.id],
        is_active=row.is_active,
    )
