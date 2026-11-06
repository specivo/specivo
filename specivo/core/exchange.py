"""Currency conversion over a set of daily exchange rates, without a database.

:class:`RateBook` holds rate observations (``1 base = rate quote`` on a day,
from a source) and answers "which rate converts A into B on this day under
this policy". :mod:`specivo.services.exchange_rate_service` loads the rows a
batch of conversions needs in a couple of queries and resolves every amount
here, so reports never query per amount.

Rules (also in the guide, "Exchange rates"):

- **Policy.** ``entry_date`` (default): the newest rate on or before the
  amount's date, at most ``max_age_days`` old. ``latest``: the newest rate
  there is, whatever its date. A fixed date: the newest rate on or before that
  date, at most ``max_age_days`` old, for every amount.
- **Lookup.** The pair itself (``A -> B``) or its inverse (``B -> A``, used as
  ``1 / rate``); then, when neither has a usable rate, triangulation through the
  pivot currency (``A -> pivot -> B``, each leg resolved the same way).
- **Choosing among rates.** The newest day wins. On the same day a ``manual``
  rate beats a ``csv`` one, which beats a fetched (``ecb``) one; then the
  pair as stored beats its inverse.
- **Missing.** No usable rate gives :class:`RateMissing` with the reason
  ``stale`` (rates exist, but all older than the maximum age) or ``no_rate``.
  Callers keep such amounts in an "unconverted" list; they are never dropped
  and never counted as zero.
- **Exact.** Rates and amounts are ``Decimal``. Inverting and multiplying
  rates runs at 40 significant digits; a converted amount is rounded half up to
  8 decimal places, the scale of stored amounts (``NUMERIC(20, 8)``). Display
  rounding to the currency's minor units is the caller's (``Money.quantize``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

from specivo.core.money import Money

#: Rank of each source when several hold a rate for the same day and pair (lower wins).
SOURCE_PRECEDENCE: dict[str, int] = {"manual": 0, "csv": 1, "ecb": 2}

#: Default maximum age of a rate under the ``entry_date`` and fixed-date policies.
DEFAULT_MAX_AGE_DAYS = 7

#: Default pivot currency for triangulation: the ECB publishes everything against the euro.
DEFAULT_PIVOT = "EUR"

#: Significant digits used for inverting and multiplying rates.
RATE_PRECISION = 40

#: Decimal places a converted amount keeps (the scale of stored amounts).
CONVERTED_PLACES = 8
_CONVERTED_EXPONENT = Decimal(1).scaleb(-CONVERTED_PLACES)


class PolicyKind(StrEnum):
    ENTRY_DATE = "entry_date"
    LATEST = "latest"
    FIXED = "fixed"


class MissingReason(StrEnum):
    #: No rate connects the two currencies on or before the day.
    NO_RATE = "no_rate"
    #: Rates exist, but the newest usable one is older than the maximum age.
    STALE = "stale"


class ConversionMethod(StrEnum):
    IDENTITY = "identity"
    DIRECT = "direct"
    INVERSE = "inverse"
    PIVOT = "pivot"


@dataclass(frozen=True, slots=True)
class RatePolicy:
    """Which rate date an amount is converted at.

    Build one with :meth:`entry_date`, :meth:`latest`, :meth:`fixed` or
    :meth:`parse` (``"entry_date"``, ``"latest"`` or an ISO date).
    """

    kind: PolicyKind = PolicyKind.ENTRY_DATE
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    fixed_on: date | None = None

    def __post_init__(self) -> None:
        if self.max_age_days < 0:
            raise ValueError("max_age_days must not be negative")
        if (self.kind is PolicyKind.FIXED) != (self.fixed_on is not None):
            raise ValueError("a fixed-date policy needs fixed_on, and only it")

    @classmethod
    def entry_date(cls, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> RatePolicy:
        return cls(PolicyKind.ENTRY_DATE, max_age_days)

    @classmethod
    def latest(cls) -> RatePolicy:
        return cls(PolicyKind.LATEST, DEFAULT_MAX_AGE_DAYS)

    @classmethod
    def fixed(cls, on: date, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> RatePolicy:
        return cls(PolicyKind.FIXED, max_age_days, on)

    @classmethod
    def parse(cls, value: str | None, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> RatePolicy:
        """``"entry_date"`` (or empty), ``"latest"``, or a ``YYYY-MM-DD`` fixed date; else ``ValueError``."""
        text = (value or "").strip().lower()
        if text in ("", PolicyKind.ENTRY_DATE):
            return cls.entry_date(max_age_days)
        if text == PolicyKind.LATEST:
            return cls.latest()
        try:
            return cls.fixed(date.fromisoformat(text), max_age_days)
        except ValueError:
            raise ValueError("rate policy must be 'entry_date', 'latest' or a date (YYYY-MM-DD)") from None

    def __str__(self) -> str:
        return self.fixed_on.isoformat() if self.fixed_on is not None else self.kind.value

    def window(self, on: date) -> tuple[date | None, date | None]:
        """``(earliest, latest)`` rate dates usable for an amount dated *on*; ``None`` is open."""
        if self.kind is PolicyKind.LATEST:
            return None, None
        target = self.fixed_on if self.fixed_on is not None else on
        return target - timedelta(days=self.max_age_days), target


@dataclass(frozen=True, slots=True)
class RateObservation:
    """A stored rate: ``1 base = rate quote`` on ``rate_date``, from ``source``."""

    rate_date: date
    base: str
    quote: str
    rate: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class RateLeg:
    """One step of a conversion, in the direction it is used (``1 base = rate quote``)."""

    base: str
    quote: str
    rate: Decimal
    rate_date: date
    source: str
    #: True when the stored rate was the other way round and is used as ``1 / rate``.
    inverted: bool


@dataclass(frozen=True, slots=True)
class RateUsed:
    """The effective rate that converted ``from_currency`` into ``to_currency``.

    ``rate_date`` is the date of the rate, or of the older leg when going
    through the pivot; ``None`` for the identity (same currency).
    """

    from_currency: str
    to_currency: str
    rate: Decimal
    rate_date: date | None
    method: ConversionMethod
    legs: tuple[RateLeg, ...] = ()


@dataclass(frozen=True, slots=True)
class Converted:
    """An amount converted at :attr:`rate`."""

    original: Money
    money: Money
    rate: RateUsed
    on: date

    ok = True


@dataclass(frozen=True, slots=True)
class RateMissing:
    """An amount that could not be converted, and why.

    ``newest_rate_date`` is the date of the newest rate found for a ``stale``
    reason (too old to use), else ``None``.
    """

    original: Money
    to_currency: str
    on: date
    reason: MissingReason
    newest_rate_date: date | None = None

    ok = False


ConversionResult = Converted | RateMissing


def _invert(rate: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        return Decimal(1) / rate


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        return left * right


class RateBook:
    """Rate observations indexed by pair, answering conversions in memory."""

    def __init__(self, observations: Iterable[RateObservation] = ()) -> None:
        self._by_pair: dict[tuple[str, str], list[RateObservation]] = defaultdict(list)
        self._sorted = True
        for observation in observations:
            self.add(observation)

    def add(self, observation: RateObservation) -> None:
        if observation.rate <= 0:
            raise ValueError("exchange rates must be positive")
        if observation.source not in SOURCE_PRECEDENCE:
            raise ValueError(f"unknown rate source {observation.source!r}")
        self._by_pair[(observation.base, observation.quote)].append(observation)
        self._sorted = False

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_pair.values())

    def _sort(self) -> None:
        if self._sorted:
            return
        for rows in self._by_pair.values():
            # Newest day first; on one day the preferred source first.
            rows.sort(key=lambda r: (-r.rate_date.toordinal(), SOURCE_PRECEDENCE[r.source]))
        self._sorted = True

    def _first_in_window(
        self, base: str, quote: str, earliest: date | None, latest: date | None
    ) -> RateObservation | None:
        for row in self._by_pair.get((base, quote), ()):
            if latest is not None and row.rate_date > latest:
                continue
            if earliest is not None and row.rate_date < earliest:
                return None  # sorted newest first: everything after is older still
            return row
        return None

    def best_leg(self, base: str, quote: str, earliest: date | None, latest: date | None) -> RateLeg | None:
        """The rate for ``base -> quote`` within ``[earliest, latest]``, stored either way round."""
        self._sort()
        direct = self._first_in_window(base, quote, earliest, latest)
        inverse = self._first_in_window(quote, base, earliest, latest)
        if direct is not None and inverse is not None:
            # Newest day, then source precedence; the stored direction wins a tie.
            if (inverse.rate_date, -SOURCE_PRECEDENCE[inverse.source]) > (
                direct.rate_date,
                -SOURCE_PRECEDENCE[direct.source],
            ):
                direct = None
            else:
                inverse = None
        if direct is not None:
            return RateLeg(base, quote, direct.rate, direct.rate_date, direct.source, inverted=False)
        if inverse is not None:
            return RateLeg(base, quote, _invert(inverse.rate), inverse.rate_date, inverse.source, inverted=True)
        return None

    def _resolve_window(
        self, from_code: str, to_code: str, pivot: str, earliest: date | None, latest: date | None
    ) -> RateUsed | None:
        leg = self.best_leg(from_code, to_code, earliest, latest)
        if leg is not None:
            method = ConversionMethod.INVERSE if leg.inverted else ConversionMethod.DIRECT
            return RateUsed(from_code, to_code, leg.rate, leg.rate_date, method, (leg,))
        if pivot in (from_code, to_code):
            return None
        first = self.best_leg(from_code, pivot, earliest, latest)
        if first is None:
            return None
        second = self.best_leg(pivot, to_code, earliest, latest)
        if second is None:
            return None
        return RateUsed(
            from_code,
            to_code,
            _multiply(first.rate, second.rate),
            min(first.rate_date, second.rate_date),
            ConversionMethod.PIVOT,
            (first, second),
        )

    def resolve(
        self, from_code: str, to_code: str, on: date, policy: RatePolicy, pivot: str = DEFAULT_PIVOT
    ) -> RateUsed | tuple[MissingReason, date | None]:
        """The rate converting *from_code* into *to_code* for an amount dated *on*.

        Returns the :class:`RateUsed`, or ``(reason, newest_rate_date)`` when
        there is none.
        """
        if from_code == to_code:
            return RateUsed(from_code, to_code, Decimal(1), None, ConversionMethod.IDENTITY)
        earliest, latest = policy.window(on)
        used = self._resolve_window(from_code, to_code, pivot, earliest, latest)
        if used is not None:
            return used
        if earliest is not None:
            older = self._resolve_window(from_code, to_code, pivot, None, latest)
            if older is not None:
                return MissingReason.STALE, older.rate_date
        return MissingReason.NO_RATE, None

    def convert(
        self, money: Money, to_currency: str, on: date, policy: RatePolicy, pivot: str = DEFAULT_PIVOT
    ) -> ConversionResult:
        """Convert *money* dated *on* into *to_currency*, or say why it cannot be."""
        resolved = self.resolve(money.currency, to_currency, on, policy, pivot)
        if isinstance(resolved, RateUsed):
            return Converted(money, convert_amount(money, resolved), resolved, on)
        reason, newest = resolved
        return RateMissing(money, to_currency, on, reason, newest)


def convert_amount(money: Money, used: RateUsed) -> Money:
    """*money* times the effective rate, rounded half up to :data:`CONVERTED_PLACES` places."""
    if used.method is ConversionMethod.IDENTITY:
        return money
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        amount = (money.amount * used.rate).quantize(_CONVERTED_EXPONENT, rounding=ROUND_HALF_UP)
    return Money(amount, used.to_currency)


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnconvertedGroup:
    """Amounts in one currency that share one reason for not being converted."""

    currency: str
    reason: MissingReason
    amount: Money
    count: int
    #: Newest rate date seen for ``stale`` amounts (the most recent of the group).
    newest_rate_date: date | None = None


@dataclass(frozen=True, slots=True)
class ConversionSummary:
    """Totals in one currency plus everything that could not be converted.

    ``total`` covers only the converted amounts. ``unconverted`` lists the
    rest per original currency and reason, at their original amounts: reports
    show them next to the total ("plus unconverted amounts") and never drop
    them. ``rates_used`` lists each distinct effective rate, so reports can
    say which rate dates they used.
    """

    currency: str
    total: Money
    converted_count: int
    unconverted: tuple[UnconvertedGroup, ...] = ()
    rates_used: tuple[RateUsed, ...] = field(default=())

    @property
    def has_unconverted(self) -> bool:
        return bool(self.unconverted)

    @property
    def rate_dates(self) -> tuple[date, ...]:
        return tuple(sorted({r.rate_date for r in self.rates_used if r.rate_date is not None}))


def summarize(results: Sequence[ConversionResult], currency: str) -> ConversionSummary:
    """Fold conversion results into a :class:`ConversionSummary` in *currency*."""
    total = Money.zero(currency)
    converted = 0
    groups: dict[tuple[str, MissingReason], list[RateMissing]] = defaultdict(list)
    rates: dict[tuple[str, str, Decimal, date | None, ConversionMethod], RateUsed] = {}
    for result in results:
        if isinstance(result, Converted):
            if result.money.currency != currency:
                raise ValueError(f"converted into {result.money.currency}, summarising in {currency}")
            total = total + result.money
            converted += 1
            used = result.rate
            if used.method is not ConversionMethod.IDENTITY:
                key = (used.from_currency, used.to_currency, used.rate, used.rate_date, used.method)
                rates.setdefault(key, used)
        else:
            groups[(result.original.currency, result.reason)].append(result)
    unconverted = []
    for (code, reason), items in sorted(groups.items()):
        amount = sum((i.original for i in items), Money.zero(code))
        newest = max((i.newest_rate_date for i in items if i.newest_rate_date is not None), default=None)
        unconverted.append(UnconvertedGroup(code, reason, amount, len(items), newest))
    ordered_rates = sorted(
        rates.values(), key=lambda r: (r.from_currency, r.to_currency, r.rate_date or date.min, r.method)
    )
    return ConversionSummary(currency, total, converted, tuple(unconverted), tuple(ordered_rates))
