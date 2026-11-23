"""Shared pieces of the AI spend report pages: services and URLs."""

from __future__ import annotations

from specivo.services.ai_spend_report_service import (
    AiSpendReportService,
)
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService

_reports = AiSpendReportService()
_usage = AiUsageService()
_projects = ProjectService()
_issues = IssueService()

ADMIN_URL = "/admin/ai-spend/"
ADMIN_ENTRIES_URL = "/admin/ai-spend/entries/"
#: Rows the chart draws at most (the table shows them all).
CHART_MAX_ROWS = 40
