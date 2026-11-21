"""The AI spend of one release or sprint, for the "AI spend" card of the version and sprint pages.

A project's spend report (:class:`AiSpendReportService`) narrowed to the
release or sprint (``ReportFilters.version_id`` / ``sprint_id``) over the days
its counted entries span, so the card and the report it links to agree: the
same entries, visibility and seat cost rules. A span longer than a report may
cover (``MAX_RANGE_DAYS``) keeps its latest days and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.services.ai_spend_report_service import MAX_RANGE_DAYS, AiSpendReportService, ReportFilters, SpendReport

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from specivo.models.project import Project
    from specivo.models.user import User


@dataclass
class PlanningSpend:
    """The card's numbers: the report's totals over ``date_from`` to ``date_to``, and the people."""

    #: None when the reader may read no counted entry of the release or sprint.
    report: SpendReport | None
    people: int = 0
    date_from: date | None = None
    date_to: date | None = None
    #: The entries span more than a report covers; the report has the latest days.
    truncated: bool = False


async def planning_spend(
    session: AsyncSession, viewer: User, project: Project, filters: ReportFilters
) -> PlanningSpend:
    """The AI spend of *project*'s entries matching *filters* (a release or a sprint) that *viewer* may read.

    Queries: the span of the entries, the report (bounded, see the report
    service) and the number of people.
    """
    reports = AiSpendReportService()
    conditions = await reports.conditions(session, viewer, project, None, None, filters)
    first, last = (
        await session.execute(
            select(func.min(AiUsageEntry.usage_date), func.max(AiUsageEntry.usage_date)).where(*conditions)
        )
    ).one()
    if first is None or last is None:
        return PlanningSpend(report=None)
    truncated = (last - first).days + 1 > MAX_RANGE_DAYS
    if truncated:
        first = last - timedelta(days=MAX_RANGE_DAYS - 1)
    report = await reports.report(
        session, viewer, project=project, date_from=first, date_to=last, group_by=(), filters=filters
    )
    facts = await reports.drill_facts(
        session, viewer, project=project, date_from=first, date_to=last, filters=filters, dim=None
    )
    return PlanningSpend(report=report, people=facts.people, date_from=first, date_to=last, truncated=truncated)


__all__ = ["PlanningSpend", "planning_spend"]
