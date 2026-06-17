"""Occurrence-expansion engine for recurring patterns.

Pure, DB-free, async-free. Given a :class:`RecurrenceSpec` (plain Python values
adapted from an ORM pattern by the service layer) and a UTC window, it returns
the list of occurrence instants (tz-aware UTC) that fall inside the window.

Design notes
------------
**Timezone / DST correctness.** A pattern's ``dtstart`` is a wall-clock anchor
in ``spec.timezone``. We run the entire ``dateutil`` expansion in *naive local
wall-clock space* (dateutil rrule is naive-friendly), then attach the zone and
convert each result to UTC. This keeps e.g. "09:00 local daily" pinned at 09:00
local across DST transitions, so the stored UTC instant shifts by an hour at the
boundary — which is the desired behaviour for a human-facing schedule.

**Ambiguous / nonexistent wall times.** After expansion we localise each naive
local datetime:

- *Nonexistent* time (spring-forward gap, e.g. 02:30 on the US "spring forward"
  day): there is no such instant, so we shift *forward* to the first valid
  instant after the gap (the wall clock effectively jumps to 03:30).
- *Ambiguous* time (fall-back overlap, e.g. 01:30 occurring twice): we pick
  ``fold=0``, i.e. the *earlier* of the two instants.

Both policies are deterministic and tested.

**EXDATE vs COUNT.** EXDATE removal must not consume a ``COUNT``: a ``count=5``
rule with one excluded date still yields five occurrences (window permitting).
If we baked ``COUNT`` into the ``dateutil`` rrule, the rrule would stop after
five *generated* instants and any exdated one would simply shrink the result.
So for the discrete-field path we build the rrule WITHOUT ``COUNT``, iterate it,
skip exdated instants (a skip does NOT advance the live counter), and stop only
after we have emitted ``count`` live (non-excluded) occurrences. This makes a
``count=5`` rule with one exdate continue to a sixth generated instant to reach
five live ones.

(The ``rrule_raw`` path is the exception: any ``COUNT`` inside the raw RRULE
string is applied natively by dateutil, since we cannot cleanly strip it; there
the count counts generated occurrences. This is documented and tested.)

**Working-day adjustment.** Applied AFTER rrule expansion (no RRULE token
expresses it). For each occurrence whose date is a non-working weekday or a
listed holiday, shift per ``working_day_adjustment`` (``next`` / ``previous`` /
``nearest`` — tie goes to ``next``), preserving the time of day, re-checking
after each hop so consecutive non-working days are skipped. If two occurrences
land on the same adjusted instant, the first wins and the duplicate is dropped
(deterministic dedupe).

**Window semantics.** ``window_start`` is inclusive, ``window_end`` is inclusive.
The returned list is sorted ascending and contains tz-aware UTC datetimes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from dateutil import rrule as dr

# dateutil's rrule frequency argument is a Literal[0..6]; we cast at the call site.
_Freq = Literal[0, 1, 2, 3, 4, 5, 6]

# Map our lowercase freq strings to dateutil frequency constants.
_FREQ_MAP: dict[str, int] = {
    "daily": dr.DAILY,
    "weekly": dr.WEEKLY,
    "monthly": dr.MONTHLY,
    "yearly": dr.YEARLY,
}

# Two-letter weekday tokens -> dateutil weekday objects (MO..SU).
_WEEKDAY_MAP: dict[str, dr.weekday] = {
    "MO": dr.MO,
    "TU": dr.TU,
    "WE": dr.WE,
    "TH": dr.TH,
    "FR": dr.FR,
    "SA": dr.SA,
    "SU": dr.SU,
}

# Safety valve: never generate more than this many raw candidates before
# windowing, to keep a pathological infinite rule from running away. Working-day
# look-ahead windows are bounded (creation_lead_time_days), so this is generous.
_MAX_CANDIDATES = 100_000


@dataclass(frozen=True)
class RecurrenceSpec:
    """Plain-value snapshot of a pattern's recurrence rule.

    Decoupled from SQLAlchemy: the service layer builds this from an ORM
    ``RecurringPattern`` (or :func:`spec_from_mapping` builds it from a dict).

    Fields mirror the RFC 5545 subset plus the tracker-style extensions:

    - ``freq``: one of ``daily`` / ``weekly`` / ``monthly`` / ``yearly``.
    - ``interval``: positive recurrence interval (RRULE ``INTERVAL``).
    - ``byday``: weekday tokens, optionally ordinal-prefixed
      (``["MO", "WE"]`` or ``["1MO", "-1FR"]``).
    - ``bymonthday`` / ``bymonth`` / ``bysetpos``: RRULE ``BY*`` parts.
    - ``count`` / ``until``: mutually exclusive end conditions. ``until`` is a
      tz-aware datetime (UTC or any zone; compared after UTC conversion).
    - ``dtstart``: tz-aware series anchor, interpreted as wall-clock in
      ``timezone``.
    - ``timezone``: IANA zone name (e.g. ``"America/New_York"``).
    - ``working_day_adjustment``: ``none`` / ``nearest`` / ``next`` / ``previous``.
    - ``working_days``: ISO weekday ints (1=Mon .. 7=Sun) considered working days.
    - ``holiday_calendar``: ISO date strings treated as non-working.
    - ``rrule_raw``: a full RRULE string; when set it takes precedence over the
      discrete ``BY*``/freq/interval fields (``dtstart`` and ``timezone`` still
      apply).
    """

    freq: str
    dtstart: datetime
    timezone: str = "UTC"
    interval: int = 1
    byday: list[str] | None = None
    bymonthday: list[int] | None = None
    bymonth: list[int] | None = None
    bysetpos: list[int] | None = None
    count: int | None = None
    until: datetime | None = None
    working_day_adjustment: str = "none"
    working_days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    holiday_calendar: list[str] | None = None
    rrule_raw: str | None = None


def spec_from_mapping(d: dict) -> RecurrenceSpec:
    """Build a :class:`RecurrenceSpec` from a plain mapping.

    Convenience for callers (and tests) that hold a dict rather than an ORM
    object. Still imports no models. Keys mirror :class:`RecurrenceSpec` field
    names; ``rrule_interval`` is accepted as an alias for ``interval`` and
    ``rrule_count`` for ``count`` to match the ORM column names.
    """
    interval = d.get("interval", d.get("rrule_interval", 1))
    count = d.get("count", d.get("rrule_count"))
    working_days = d.get("working_days")
    return RecurrenceSpec(
        freq=d["freq"],
        dtstart=d["dtstart"],
        timezone=d.get("timezone", "UTC"),
        interval=interval if interval is not None else 1,
        byday=d.get("byday"),
        bymonthday=d.get("bymonthday"),
        bymonth=d.get("bymonth"),
        bysetpos=d.get("bysetpos"),
        count=count,
        until=d.get("until"),
        working_day_adjustment=d.get("working_day_adjustment", "none"),
        working_days=list(working_days) if working_days else [1, 2, 3, 4, 5],
        holiday_calendar=d.get("holiday_calendar"),
        rrule_raw=d.get("rrule_raw"),
    )


def _parse_byday(tokens: Iterable[str]) -> list[dr.weekday]:
    """Parse BYDAY tokens into dateutil weekday objects.

    ``"MO"`` -> Monday (no ordinal). ``"1MO"`` -> first Monday of the period.
    ``"-1FR"`` -> last Friday of the period.
    """
    out: list[dr.weekday] = []
    for raw in tokens:
        token = raw.strip().upper()
        if not token:
            raise ValueError("byday entry is empty")
        # Split optional leading signed ordinal from the trailing weekday code.
        i = 0
        if token[0] in "+-":
            i = 1
        while i < len(token) and token[i].isdigit():
            i += 1
        ordinal_part, day_part = token[:i], token[i:]
        wd = _WEEKDAY_MAP.get(day_part)
        if wd is None:
            raise ValueError(f"invalid byday weekday code: {raw!r}")
        if ordinal_part in ("", "+", "-"):
            if ordinal_part:  # a lone sign with no number
                raise ValueError(f"invalid byday ordinal: {raw!r}")
            out.append(wd)
        else:
            out.append(wd(int(ordinal_part)))
    return out


def _validate(spec: RecurrenceSpec) -> None:
    """Defensive coherence checks; raise ``ValueError`` on an incoherent spec."""
    if spec.dtstart.tzinfo is None:
        raise ValueError("spec.dtstart must be timezone-aware")
    if spec.rrule_raw:
        # The raw RRULE path bypasses discrete-field validation; dtstart/tz still
        # required (checked above). count/until live inside the raw string.
        return
    if spec.freq not in _FREQ_MAP:
        raise ValueError(f"invalid freq: {spec.freq!r}")
    if spec.interval is None or spec.interval < 1:
        raise ValueError("interval must be a positive integer")
    if spec.count is not None and spec.until is not None:
        raise ValueError("count and until are mutually exclusive")
    if spec.count is not None and spec.count < 1:
        raise ValueError("count must be a positive integer")
    if spec.bysetpos and not (spec.byday or spec.bymonthday or spec.bymonth):
        # BYSETPOS selects from a set produced by other BY* parts; on its own it
        # is meaningless.
        raise ValueError("bysetpos requires byday/bymonthday/bymonth")


def _to_naive_local(dt: datetime, tz: ZoneInfo) -> datetime:
    """Return the naive wall-clock value of ``dt`` as seen in ``tz``."""
    if dt.tzinfo is None:
        # Already naive — treat as local wall-clock.
        return dt
    return dt.astimezone(tz).replace(tzinfo=None)


def _localize_to_utc(naive_local: datetime, tz: ZoneInfo) -> datetime:
    """Convert a naive local wall-clock datetime to a tz-aware UTC instant.

    Policy for irregular wall times:

    - Nonexistent (spring-forward gap): shift forward to the first valid instant.
    - Ambiguous (fall-back overlap): pick ``fold=0`` (the earlier instant).
    """
    aware = naive_local.replace(tzinfo=tz, fold=0)

    # Detect a nonexistent time: localising then round-tripping through UTC and
    # back does not reproduce the same wall clock when the time fell in a gap.
    roundtrip = aware.astimezone(UTC).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != naive_local:
        # The wall time does not exist; advance until we hit a real instant. The
        # gap is at most a couple of hours, so step minute-by-minute from the
        # gap start to find the first valid local time.
        candidate = naive_local
        for _ in range(0, 24 * 60):
            candidate = candidate + timedelta(minutes=1)
            c_aware = candidate.replace(tzinfo=tz, fold=0)
            if c_aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == candidate:
                return c_aware.astimezone(UTC)
        # Fallback (should never happen): use the original despite the gap.
        return aware.astimezone(UTC)

    return aware.astimezone(UTC)


def _build_rrule(spec: RecurrenceSpec, naive_dtstart: datetime) -> dr.rrule:
    """Build a naive ``dateutil.rrule`` from the spec (no exclusions applied)."""
    if spec.rrule_raw:
        parsed = dr.rrulestr(spec.rrule_raw, dtstart=naive_dtstart)
        if not isinstance(parsed, dr.rrule):
            # rrulestr can return an rruleset for multi-line strings; we only
            # support a single RRULE here.
            raise ValueError("rrule_raw must be a single RRULE")
        return parsed

    byweekday = _parse_byday(spec.byday) if spec.byday else None
    kwargs: dict = {
        "dtstart": naive_dtstart,
        "interval": spec.interval,
    }
    if byweekday is not None:
        kwargs["byweekday"] = byweekday
    if spec.bymonthday:
        kwargs["bymonthday"] = spec.bymonthday
    if spec.bymonth:
        kwargs["bymonth"] = spec.bymonth
    if spec.bysetpos:
        kwargs["bysetpos"] = spec.bysetpos
    # NOTE: COUNT is intentionally NOT passed to the rrule. The caller applies
    # the count manually after EXDATE removal so that skipped occurrences do not
    # consume the count. See expand_occurrences.
    if spec.until is not None:
        # Convert UNTIL to naive local space to match the naive expansion.
        kwargs["until"] = _to_naive_local(spec.until, ZoneInfo(spec.timezone))
    return dr.rrule(cast(_Freq, _FREQ_MAP[spec.freq]), **kwargs)


def _is_working_day(d: date, working_days: set[int], holidays: set[str]) -> bool:
    """True if ``d`` is a working weekday and not a listed holiday."""
    if d.isoweekday() not in working_days:
        return False
    if d.isoformat() in holidays:
        return False
    return True


def _adjust_working_day(
    dt: datetime,
    adjustment: str,
    working_days: set[int],
    holidays: set[str],
) -> datetime:
    """Shift a naive-local occurrence off non-working days, preserving time."""
    if adjustment == "none":
        return dt
    if _is_working_day(dt.date(), working_days, holidays):
        return dt

    if adjustment == "next":
        return _walk(dt, +1, working_days, holidays)
    if adjustment == "previous":
        return _walk(dt, -1, working_days, holidays)
    if adjustment == "nearest":
        fwd = _walk(dt, +1, working_days, holidays)
        bwd = _walk(dt, -1, working_days, holidays)
        fwd_dist = (fwd.date() - dt.date()).days
        bwd_dist = (dt.date() - bwd.date()).days
        # Tie -> next (forward) per policy.
        return fwd if fwd_dist <= bwd_dist else bwd
    raise ValueError(f"invalid working_day_adjustment: {adjustment!r}")


def _walk(dt: datetime, step: int, working_days: set[int], holidays: set[str]) -> datetime:
    """Step day-by-day in ``step`` direction until a working day is reached."""
    cur = dt
    # Bound the search to avoid an infinite loop if every day is non-working.
    for _ in range(1, 366):
        cur = cur + timedelta(days=step)
        if _is_working_day(cur.date(), working_days, holidays):
            return cur
    raise ValueError("no working day found within a year of the occurrence")


def expand_occurrences(
    spec: RecurrenceSpec,
    window_start: datetime,
    window_end: datetime,
    exdates: Iterable[datetime] = (),
) -> list[datetime]:
    """Expand a recurrence spec into UTC instants within ``[window_start, window_end]``.

    Args:
        spec: the recurrence definition (plain values).
        window_start: tz-aware UTC lower bound (inclusive).
        window_end: tz-aware UTC upper bound (inclusive).
        exdates: instants to exclude. Each is compared in UTC against the
            scheduled (pre-working-day-adjustment) occurrence instant. Excluded
            occurrences do NOT consume a ``COUNT``.

    Returns:
        Sorted ascending list of tz-aware UTC datetimes.

    Raises:
        ValueError: if the spec is incoherent (see :func:`_validate`).
    """
    _validate(spec)

    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")

    tz = ZoneInfo(spec.timezone)
    naive_dtstart = _to_naive_local(spec.dtstart, tz)

    # Normalise exdates to UTC instants for comparison.
    exdate_set: set[datetime] = set()
    for ex in exdates:
        if ex.tzinfo is None:
            raise ValueError("exdates must be timezone-aware")
        exdate_set.add(ex.astimezone(UTC))

    rule = _build_rrule(spec, naive_dtstart)

    # COUNT semantics: for the discrete-field path the count is NOT baked into
    # the rrule (see _build_rrule) — we apply it here AFTER EXDATE removal so
    # skips don't consume it. For the rrule_raw path, any COUNT inside the raw
    # string is applied natively by dateutil and we don't re-apply it.
    count_limit = spec.count if (spec.count is not None and not spec.rrule_raw) else None

    # Hard stop for the naive iteration when neither count nor an rrule-internal
    # terminator bounds it. We stop a bit past window_end so that a 'previous' /
    # 'nearest' working-day shift can still pull a later occurrence back into
    # the window.
    iteration_cutoff = window_end + timedelta(days=14)

    working_days = set(spec.working_days)
    holidays = set(spec.holiday_calendar or [])
    adjust = spec.working_day_adjustment

    kept: list[datetime] = []
    seen_utc: set[datetime] = set()  # for working-day collision dedupe
    live = 0  # live (non-exdated) occurrences emitted, for COUNT
    scanned = 0

    for naive_occ in rule:
        scanned += 1
        if scanned > _MAX_CANDIDATES:
            break

        # Scheduled UTC instant (before working-day adjustment) — used for the
        # EXDATE comparison and the iteration cutoff.
        scheduled_utc = _localize_to_utc(naive_occ, tz)

        # Stop a non-counted, finite-window scan once we are clearly past the end.
        if count_limit is None and scheduled_utc > iteration_cutoff:
            break

        # EXDATE: skip excluded instants. This does NOT consume the count.
        if scheduled_utc in exdate_set:
            continue

        # COUNT: stop once we have emitted `count` live occurrences.
        if count_limit is not None and live >= count_limit:
            break
        live += 1

        # Working-day adjustment in naive-local space, then re-localise to UTC.
        adjusted_naive = _adjust_working_day(naive_occ, adjust, working_days, holidays)
        final_utc = scheduled_utc if adjusted_naive == naive_occ else _localize_to_utc(adjusted_naive, tz)

        # Collision dedupe: first occurrence on a given instant wins.
        if final_utc in seen_utc:
            continue
        seen_utc.add(final_utc)
        kept.append(final_utc)

    # Window filtering: inclusive on both ends, compared in UTC.
    result = sorted(dt for dt in kept if window_start <= dt <= window_end)
    return result
