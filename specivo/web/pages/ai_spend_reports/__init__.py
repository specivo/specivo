"""Web pages for AI spend reports: the project report, the admin "AI spend" page and its entry list.

- ``/projects/{key}/ai-usage/report/``: the project's AI usage the reader may
  see, grouped and totalled in one currency.
- ``/admin/ai-spend/``: the whole instance (administrators), unallocated seat
  cost included.
- ``/admin/ai-spend/entries/``: the entries behind a cell of the admin
  report, across projects; a project report links the project's "AI usage"
  tab instead.

Every rule (visibility, seat allocation, who sees seat cost) is
:class:`AiSpendReportService`'s. Both report pages are a drill-down whose
whole state is in the URL (:class:`ReportForm`): the grouping (``group``,
``sessions`` for the entries) and the drill filters (``project``,
``user_id``, ``issue``, ``tool``, ``model``, ``provider``, ``cost_type``,
``branch``). A row opens its group: its values become filters and the
grouping moves on (:data:`DRILL_NEXT`); the path above the title steps back.
The page renders every level on the server; htmx only swaps a level in
without a page load (``frontend/js/modules/spend-drill.js`` moves the focus).
Under the drill-down, "Spend by person" cards, then the AI usage charts
(``AiUsageChartService.report_charts``, the ``usage_charts`` macro), whose
stack colours the table swatches and cards share (``color_slots``). Charts
and cards are cached per reader (:func:`cached_chart_payload`). The CSV
export is the REST endpoint with the same query.
"""

from __future__ import annotations

from specivo.web.pages.ai_spend_reports.pages import router

__all__ = ["router"]
