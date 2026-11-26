"""The "Spend by person" cards of the spend report."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.i18n import get_locale
from specivo.core.i18n import gettext as _
from specivo.models.ai_subscription import AiSubscription
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    PersonUsage,
    SpendReport,
    fold_rows,
)
from specivo.web.pages.ai_spend_reports._common import _reports
from specivo.web.pages.ai_spend_reports.drill import _share, _slot, _slot_map, _spend
from specivo.web.pages.ai_spend_reports.form import NO_SHOWS, ReportForm, Scope, next_group
from specivo.web.pages.ai_spend_reports.view import _cache_parts, _version_stmt
from specivo.web.pages.ai_usage.charts import cached_chart_payload
from specivo.web.pages.ai_usage.rows import _tool_names

#: Person cards: the largest by spend; one more card sums the rest and opens the Person level.
PEOPLE_TOP = 10


# ---------------------------------------------------------------------------
# Spend by person
# ---------------------------------------------------------------------------


def _initials(name: str) -> str:
    words = [w for w in name.replace(".", " ").replace("_", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    return name[:2].upper() if name else "?"


def _payment(usage: PersonUsage | None, plans: list[str], instance: bool, seat_shown: bool) -> str:
    """How a person's usage is paid: plan names (instance report), a seat, or the cost types only.

    Plan names only on the instance report (administrators); a project report
    that shows seat cost says "subscription seat"; without seat cost only the
    cost types the entries carry.
    """
    cost_types = usage.cost_types if usage is not None else set()
    parts: list[str] = []
    if "subscription" in cost_types or plans:
        if seat_shown and instance and plans:
            parts.extend(plans)
        elif seat_shown:
            parts.append(_("Subscription seat"))
        else:
            parts.append(_("Subscription"))
    labels = {"api": _("Pay-per-use API"), "local": _("Local model"), "free": _("Free")}
    parts.extend(labels[c] for c in ("api", "local", "free") if c in cost_types)
    return " + ".join(parts)


async def _people_view(
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
    report: SpendReport,
    scope: Scope,
    charts: dict[str, Any] | None,
    base_url: str,
) -> dict[str, Any] | None:
    """The "Spend by person" cards of the current level, from Redis when built in the last minute.

    The :data:`PEOPLE_TOP` people with the largest spend, and one summary
    card with how many more there are and their spend (exact: the sum of
    their rows), which opens the Person level of the drill-down, ordered by
    spend (that table folds past :data:`DRILL_MAX_ROWS` rows). Tools, cost
    types and plans are looked up for the people shown only.

    Cached with the same key parts as the charts (scope, reader, what the
    reader may see, every filter, the data version), so readers never share it.
    """
    if form.user_id:
        return None

    async def build() -> dict[str, Any] | None:
        if report.group_by == ("user",) and not report.period:
            by_user = report
        else:
            by_user = await _reports.report(
                db,
                user,
                project=project,
                date_from=report.date_from,
                date_to=report.date_to,
                group_by=("user",),
                currency=report.currency,
                rate_policy=report.rate_policy,
                include_seat_cost=report.include_seat_cost,
                filters=scope.filters,
                sort=form.sort,
            )
        rows = [r for r in by_user.rows if r.key.get("user") is not None]
        if not rows:
            return None
        # The largest by spend (ties by name); the rest are summed into one card.
        ranked = sorted(rows, key=lambda r: (-_spend(r), r.labels.get("user", "").lower(), r.key["user"]))
        top, rest = ranked[:PEOPLE_TOP], ranked[PEOPLE_TOP:]
        if len(rest) == 1:
            top, rest = ranked, []
        instance = project is None
        seat_shown = by_user.seat_cost_shown
        ids = [r.key["user"] for r in top]
        usage = await _reports.person_usage(
            db,
            user,
            project=project,
            date_from=report.date_from,
            date_to=report.date_to,
            filters=scope.filters,
            users=ids,
        )
        plans: dict[int, list[str]] = {}
        if seat_shown and instance:
            plans = await _plan_names(db, by_user, ids, usage)
        tool_names = await _tool_names(db)
        locale = get_locale()
        slots = _slot_map("user", charts, rows)
        largest = max((_spend(r) for r in top), default=Decimal(0))
        filtered = set(form.filtered()) | {"user"}
        cards = []
        for row in top:
            uid = row.key["user"]
            name = row.labels.get("user", "") or str(uid)
            used = usage.get(uid)
            cards.append(
                {
                    "name": name,
                    "initials": _initials(name),
                    "slot": _slot(slots, "user", uid),
                    "spend": row.total_spend.total.format(locale) if row.total_spend is not None else "–",
                    "width": _share(_spend(row), largest)["width"],
                    "tools": [tool_names.get(t, t) for t in used.tools] if used else [],
                    "payment": _payment(used, plans.get(uid, []), instance, seat_shown),
                    "url": base_url + "?" + form.level_query(next_group("user", filtered, instance), user_id=str(uid)),
                }
            )
        more = None
        if rest:
            summed = fold_rows(rest, by_user.currency, seat_shown)
            more = {
                "count": len(rest),
                "spend": summed.total_spend.total.format(locale) if summed.total_spend is not None else "–",
                "url": base_url + "?" + form.level_query("user", sort="spend"),
            }
        return {"cards": cards, "more": more}

    parts = await _cache_parts(db, user, project, report)
    parts.update(sort=form.sort, level=form.query(offset="", limit="", bucket="", detail="", **NO_SHOWS))
    prefix = f"aiu:spend-people:{report.scope}:{project.id if project is not None else 0}:{user.id}"
    return await cached_chart_payload(db, prefix, parts, _version_stmt(project), build)


async def _plan_names(
    db: AsyncSession, report: SpendReport, users: list[int], usage: dict[int, PersonUsage]
) -> dict[int, list[str]]:
    """Plan names of *users* (instance report only): plans their entries used, and idle plans whose fee counts.

    A company seat plan is named for each member whose entries used it.
    """
    used = {sid for u in usage.values() for sid in u.subscription_ids}
    condition: Any = AiSubscription.id.in_(used) if used else None
    if report.unallocated_shown:
        idle = (
            AiSubscription.user_id.in_(users)
            & (AiSubscription.starts_on <= report.date_to)
            & (AiSubscription.ends_on.is_(None) | (AiSubscription.ends_on >= report.date_from))
        )
        condition = idle if condition is None else (condition | idle)
    if condition is None:
        return {}
    stmt = (
        select(AiSubscription.id, AiSubscription.user_id, AiSubscription.plan_name)
        .where(condition)
        .order_by(AiSubscription.plan_name)
    )
    rows = (await db.execute(stmt)).all()
    out: dict[int, list[str]] = {}
    for sid, uid, name in rows:
        holders = [uid] if uid is not None else [u for u, person in usage.items() if sid in person.subscription_ids]
        for holder in holders:
            if name not in out.setdefault(holder, []):
                out[holder].append(name)
    return out
