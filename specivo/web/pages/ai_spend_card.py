"""The "AI spend" card of the version and sprint pages: what a release or sprint cost, with a link to the report.

The numbers are the project's AI spend report narrowed to the release or
sprint (:func:`specivo.services.ai_spend_summary.planning_spend`), over the
days its entries span, so the card and the report it links to agree.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.i18n import get_locale
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import ReportFilters
from specivo.services.ai_spend_summary import planning_spend
from specivo.web.pages.ai_spend_reports.view import _short_int
from specivo.web.pages.ai_usage._common import _fmt_int
from specivo.web.pages.ai_usage.rows import _money_view


async def ai_spend_card(db: AsyncSession, user: User, project: Project, dim: str, value_id: int) -> dict[str, Any]:
    """The card of the release (*dim* ``version``) or sprint *value_id* of *project*, as *user* may see it.

    ``empty`` when *user* may read no counted entry of it; ``url`` opens the
    project's report narrowed to it, grouped by person, over the same days.
    """
    filters = ReportFilters(version_id=value_id) if dim == "version" else ReportFilters(sprint_id=value_id)
    spend = await planning_spend(db, user, project, filters)
    base = f"/projects/{project.key}/ai-usage/report/?"
    report = spend.report
    if report is None or spend.date_from is None or spend.date_to is None:
        return {"empty": True, "url": base + urlencode({"group": "user", dim: value_id})}
    totals = report.totals
    locale = get_locale()
    query = {
        "group": "user",
        dim: value_id,
        "preset": "custom",
        "from_date": spend.date_from.isoformat(),
        "to_date": spend.date_to.isoformat(),
    }
    return {
        "empty": False,
        "spend": _money_view(totals.total_spend, locale) if totals.total_spend else None,
        "api_equivalent": _money_view(totals.api_equivalent, locale) if totals.api_equivalent else None,
        "seat_shown": report.seat_cost_shown,
        "tokens": _short_int(totals.total_tokens, locale),
        "tokens_full": _fmt_int(totals.total_tokens, locale),
        "entries": totals.entries,
        "people": spend.people,
        "date_from": spend.date_from.isoformat(),
        "date_to": spend.date_to.isoformat(),
        "truncated": spend.truncated,
        "url": base + urlencode(query),
    }


__all__ = ["ai_spend_card"]
