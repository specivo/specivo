"""The issue page's Development tab: what its templates need besides ``IssueDevelopmentOut``.

The tab is rendered with the page and again by the htmx partials in
``specivo.web.partials.issues`` (a refresh after a detach or move, a kind
filter, "Show more"), so both build their context here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from specivo.schemas.repository import DEVELOPMENT_KINDS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from specivo.models.issue import Issue
    from specivo.models.user import User
    from specivo.schemas.repository import IssueDevelopmentOut

# Timeline entries per page in the tab; "Show more" loads the next page.
DEVELOPMENT_TAB_PAGE_SIZE = 20


def selected_kinds(kinds: Sequence[str] | None) -> list[str]:
    """The known kinds of *kinds*, in timeline order, without repeats; empty means every kind."""
    wanted = set(kinds or ())
    return [k for k in DEVELOPMENT_KINDS if k in wanted]


def development_tab_count(development: IssueDevelopmentOut | None) -> int:
    """The tab's count: linked pull requests plus linked commits (0 hides the tab on the page)."""
    if development is None:
        return 0
    return len(development.pull_requests) + development.commit_count


async def can_edit_development(db: AsyncSession, user: User, project_id: int) -> bool:
    """Whether *user* gets the detach and move controls: ``edit_issues``, as the REST endpoints require."""
    if user.is_anonymous:
        return False
    from specivo.services.permission_service import Permission, check_permission

    return await check_permission(user, project_id, Permission.EDIT_ISSUES, db)


async def load_development(
    db: AsyncSession, issue: Issue, *, kinds: Sequence[str] = (), offset: int = 0
) -> IssueDevelopmentOut:
    """The issue's development activity with one tab page of the timeline of *kinds* (all when empty)."""
    from specivo.services.repository_service import development_for_issue

    return await development_for_issue(
        db, issue.id, kinds=list(kinds) or None, offset=offset, limit=DEVELOPMENT_TAB_PAGE_SIZE
    )


def development_context(
    development: IssueDevelopmentOut | None,
    *,
    kinds: Sequence[str] = (),
    can_edit: bool = False,
) -> dict[str, Any]:
    """Template context for ``pages/issues/_development*.html``.

    ``dev_present_kinds`` are the kinds with at least one entry (the filter
    chips), ``dev_kinds`` the selected ones (empty: all). ``dev_extra`` holds,
    per timeline entry (``"<kind>:<id>"``), what the timeline row lacks: the
    forge link of a branch and whether a pull request says it fixes the issue.
    """
    from specivo.services.repository_service import branch_url

    ctx: dict[str, Any] = {
        "development": development,
        "dev_kinds": list(kinds),
        "dev_present_kinds": [],
        "dev_extra": {},
        "dev_page_size": DEVELOPMENT_TAB_PAGE_SIZE,
        "can_edit_development": can_edit,
    }
    if development is None or development.summary is None:
        return ctx
    counts = development.summary.counts
    ctx["dev_present_kinds"] = [k for k in DEVELOPMENT_KINDS if counts.get(k)]
    repos = {r.name: r for r in development.summary.repositories}
    closing = {pr.id: pr.closing for pr in development.pull_requests}
    extra: dict[str, dict[str, Any]] = {}
    for item in development.timeline.items if development.timeline else []:
        entry: dict[str, Any] = {}
        repo = repos.get(item.repository)
        if item.branch and repo is not None and item.kind in ("commit", "pull_request", "ci"):
            entry["branch_url"] = branch_url(repo.provider, repo.url, item.branch)
        if item.kind == "pull_request":
            entry["closing"] = closing.get(item.id, False)
        extra[f"{item.kind}:{item.id}"] = entry
    ctx["dev_extra"] = extra
    return ctx
