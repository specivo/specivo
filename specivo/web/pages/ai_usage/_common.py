"""Shared pieces of the AI usage pages: services, URLs, small parsers and redirects."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode, urlsplit, urlunsplit

from babel.numbers import format_decimal
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.app_url import prefixed
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_subscription_service import AiToolService
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService
from specivo.web.deps import redirect_to, safe_next_path

_service = AiUsageService()
_projects = ProjectService()
_issues = IssueService()
_tools = AiToolService()

MY_URL = "/my/ai-usage/"
#: Filter value for "none": entries without an issue, a branch or a project.
NONE_VALUE = "-"

_NOTICES = frozenset({"logged", "saved", "excluded", "included", "deleted", "reassigned"})
#: Error codes a redirect may carry back to a list (bulk reassignment, row actions).
_LIST_ERRORS = frozenset(
    {
        "nothing_selected",
        "project_unknown",
        "issue_unknown",
        "issue_other_project",
        "permission_denied",
        "ai_usage_target_not_member",
        "reassign_failed",
    }
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class _FormError(Exception):
    """A form value the page itself refuses (before the service sees it)."""

    def __init__(self, code: str, field_name: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.field = field_name


def _redirect(url: str, params: dict[str, str] | None = None) -> RedirectResponse:
    """303 to *url* with *params* added to its query (empty values dropped)."""
    extra = {k: v for k, v in (params or {}).items() if v}
    if not extra:
        return redirect_to(url, 303)
    parts = urlsplit(url)
    query = "&".join(q for q in (parts.query, urlencode(extra)) if q)
    return redirect_to(urlunsplit(("", "", parts.path, query, parts.fragment)), 303)


def _next_or(raw: str | None, fallback: str) -> str:
    """The safe ``next`` path, else the application path *fallback*; either way a browser path (prefixed)."""
    return safe_next_path(raw) or prefixed(fallback)


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isdigit() else None


def _fmt_int(value: int, locale: str) -> str:
    return format_decimal(value, locale=locale)


def _error_of(exc: Exception) -> dict[str, str]:
    if isinstance(exc, _FormError):
        return {"code": exc.code, "field": exc.field}
    if isinstance(exc, AppError):
        return {"code": exc.code, "field": exc.field or ""}
    raise exc


# ---------------------------------------------------------------------------
# Project tab
# ---------------------------------------------------------------------------


async def _project_for(db: AsyncSession, user: User, project_key: str) -> Project:
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, user)
    return project


async def _target(
    db: AsyncSession, user: User, project_key: str, issue_key: str
) -> tuple[Project | None, Issue | None]:
    """The project and issue a form names; the issue alone names its project. Raises ``_FormError``."""
    project: Project | None = None
    issue: Issue | None = None
    key = project_key.strip().upper()
    if key:
        try:
            project = await _project_for(db, user, key)
        except (NotFoundError, PermissionDeniedError):
            raise _FormError("project_unknown", "project") from None
    ref = issue_key.strip().upper()
    if ref:
        try:
            issue = await _issues.get_by_display_key(db, ref, user=user)
        except NotFoundError:
            raise _FormError("issue_unknown", "issue") from None
        if project is None:
            project = await _project_for(db, user, issue.project_key)
        elif issue.project_id != project.id:
            raise _FormError("issue_other_project", "issue")
    return project, issue
