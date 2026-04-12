"""Web issue pages: list, detail, create/edit forms."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.core.lookup_cache import get_lookups as _get_cached_lookups
from specivo.models.sprint import Sprint
from specivo.models.version import Version
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.project_service import ProjectService
from specivo.services.reaction_service import ReactionService
from specivo.services.relation_service import RelationService
from specivo.services.saved_filter_service import SavedFilterService
from specivo.web.deps import get_active_sprint_id as _get_active_sprint_id
from specivo.web.deps import get_current_user_optional, get_templates
from specivo.web.thread_tree import build_thread_tree

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-issues"], include_in_schema=False)
# Short-URL router: /issue/{issue_ref}/ (canonical)
short_router = APIRouter(tags=["web-issues-short"], include_in_schema=False)

_issue_svc = IssueService()
_journal_svc = JournalService()
_project_svc = ProjectService()
_reaction_svc = ReactionService()
_relation_svc = RelationService()
_saved_filter_svc = SavedFilterService()


async def _resolve_issue_project(
    db: AsyncSession,
    issue_ref: str,
    user: User,
) -> tuple:
    """Resolve project from issue_ref (e.g. 'PROJ-42') and verify access.

    Returns (project, issue).
    """
    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Issue not found")

    try:
        project = await _project_svc.get_by_key(db, issue.project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    return project, issue


async def _get_lookups(db: AsyncSession) -> dict:
    """Load trackers, statuses, priorities, activities for dropdown options.

    Backed by a process-local cache (see specivo.core.lookup_cache).
    """
    cached = await _get_cached_lookups(db)
    return {
        "trackers": cached.trackers,
        "statuses": cached.statuses,
        "priorities": cached.priorities,
        "activities": cached.activities,
    }


@router.get("/projects/{project_key}/issues/", response_class=HTMLResponse)
async def issues_list(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    status: str = Query("open"),
    tracker_id: str = Query(""),
    assigned_to_id: str = Query(""),
    priority_id: str = Query(""),
    sort: str = Query("created_at:desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    view: str = Query("list"),
    board_per_col: int = Query(10, ge=5, le=50),
) -> Response:
    """Render the issue list page for a project."""
    from specivo.core.utils import safe_int

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    filters: dict = {"status": status}
    if safe_int(tracker_id) is not None:
        filters["tracker_id"] = safe_int(tracker_id)
    if safe_int(assigned_to_id) is not None:
        filters["assigned_to_id"] = safe_int(assigned_to_id)
    if safe_int(priority_id) is not None:
        filters["priority_id"] = safe_int(priority_id)

    # Board view loads more issues (up to 200) to fill columns
    effective_limit = min(limit, 100) if view != "board" else 200
    effective_offset = offset if view != "board" else 0

    issues, total = await _issue_svc.list_issues(
        db,
        project_id=project.id,
        filters=filters,
        sort=sort,
        offset=effective_offset,
        limit=effective_limit,
        user=user,
    )

    saved_filters = await _saved_filter_svc.list_for_project(db, user, project.id)
    lookups = await _get_lookups(db)

    # Get project members for assignee dropdown
    members = await _project_svc.list_members(db, project)

    # Parse per-column offsets for board view: col_<status_id>_offset=N
    col_offsets: dict[int, int] = {}
    board_base_params = ""
    if view == "board":
        non_col_parts = []
        for k, v in request.query_params.items():
            if not k.startswith("col_"):
                non_col_parts.append(f"{k}={v}")
            elif k.endswith("_offset"):
                try:
                    sid = int(k[4:-7])
                    col_offsets[sid] = max(0, int(v))
                except (ValueError, TypeError):
                    pass
        board_base_params = "&".join(non_col_parts)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/list.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issues": issues,
            "total": total,
            "saved_filters": saved_filters,
            "members": members,
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "view": view if view in ("list", "board") else "list",
            "col_offsets": col_offsets,
            "board_base_params": board_base_params,
            "board_per_col": board_per_col,
            "filters": filters,
            "active_sprint_id": await _get_active_sprint_id(db, project.id),
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/new/", response_class=HTMLResponse)
async def issue_create_form(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue creation form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)
    versions_result = await db.execute(select(Version).where(Version.project_id == project.id).order_by(Version.name))
    versions = list(versions_result.scalars().all())

    # Load metadata schemas for this project
    from specivo.schemas.metadata_schema import MetadataSchemaOut
    from specivo.services.metadata_schema_service import MetadataSchemaService

    schema_svc = MetadataSchemaService()
    metadata_schemas = await schema_svc.list_for_project(db, project.id)
    metadata_schemas_data = [MetadataSchemaOut.model_validate(s).model_dump(mode="json") for s in metadata_schemas]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/form.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": None,
            "mode": "create",
            "members": members,
            "versions": versions,
            "metadata_schemas_data": metadata_schemas_data,
            **lookups,
        },
    )


@short_router.get("/issue/{issue_ref}/", response_class=HTMLResponse)
async def issue_detail(
    issue_ref: str,
    request: Request,
    activity_page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue detail page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    project, issue = await _resolve_issue_project(db, issue_ref, user)

    from specivo.core.constants import ACTIVITY_DEFAULT_PER_PAGE, ACTIVITY_PER_PAGE_OPTIONS
    from specivo.schemas.metadata_schema import MetadataSchemaOut
    from specivo.services.metadata_schema_service import MetadataSchemaService
    from specivo.services.watcher_service import WatcherService

    schema_svc = MetadataSchemaService()
    watcher_svc = WatcherService()

    # NOTE: these awaits are sequential on purpose. They are logically
    # independent and we explored wrapping them in asyncio.gather() on
    # fan-out sessions from the session factory — but the test harness
    # uses a single rollback-based connection for isolation (see
    # _make_test_get_db in conftest_base). New sessions from the global
    # factory bypass that connection and cannot see uncommitted fixture
    # state, so gather breaks integration tests. Left sequential; real
    # wins come from the joinedload/JOIN collapses below and from
    # htmx-deferred tabs.
    all_journals = await _journal_svc.list_for_issue(db, issue.id, include_private=user.is_admin)
    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)
    versions_result = await db.execute(
        select(Version)
        .where(Version.project_id == project.id, Version.status == "open")
        .order_by(Version.effective_date.asc().nullslast(), Version.name.asc())
    )
    versions = list(versions_result.scalars().all())
    sprints_result = await db.execute(
        select(Sprint)
        .where(Sprint.project_id == project.id, Sprint.status.in_(["active", "planned"]))
        .order_by(Sprint.status.desc(), Sprint.start_date.asc().nullslast(), Sprint.name.asc())
    )
    sprints = list(sprints_result.scalars().all())
    issue_schemas = await schema_svc.list_for_project(db, project.id)
    # Attachments are lazy-loaded via htmx when the Attachments tab is opened
    # (see /partials/issues/{key}/attachments/). Time entries stay eager because
    # the sidebar displays the time_logged sum derived from the list.
    time_entries = await _issue_svc.list_time_entries(db, issue.id)
    time_logged = sum((te.hours or 0) for te in time_entries)
    tab_ctx = {
        "time_entries": time_entries,
        "time_entry_count": len(time_entries),
        "time_logged": time_logged,
    }
    watchers = await watcher_svc.list_watchers(db, issue)
    relations = await _relation_svc.list_for_issue(db, issue)

    # Paginate activity feed
    activity_per_page = user.preferences.get("activity_per_page", ACTIVITY_DEFAULT_PER_PAGE)
    if activity_per_page not in ACTIVITY_PER_PAGE_OPTIONS:
        activity_per_page = ACTIVITY_DEFAULT_PER_PAGE

    activity_total = len(all_journals)
    activity_total_pages = max(1, (activity_total + activity_per_page - 1) // activity_per_page)
    if activity_page > activity_total_pages:
        activity_page = activity_total_pages

    activity_start = (activity_page - 1) * activity_per_page
    journals = all_journals[activity_start : activity_start + activity_per_page]
    thread_tree = build_thread_tree(journals)

    # Latest activity: most recent of issue.updated_at and last journal
    last_activity_at = issue.updated_at
    last_journal_at = all_journals[-1].created_at if all_journals else None
    if last_journal_at and (not last_activity_at or last_journal_at > last_activity_at):
        last_activity_at = last_journal_at

    # Load emoji reactions for all journals (paginated subset)
    journal_ids = [j.id for j in journals]
    reactions_by_journal_raw = await _reaction_svc.list_reactions_bulk(db, journal_ids, user.id)
    reactions_by_journal = {
        jid: [{"emoji": r.emoji, "count": r.count, "reacted_by_me": r.reacted_by_me} for r in groups]
        for jid, groups in reactions_by_journal_raw.items()
    }

    # Derive active sprint id from the already-loaded list to avoid a second query
    active_sprint_id = next((s.id for s in sprints if s.status == "active"), None)

    # Filter to applicable schemas (project-wide + issue's tracker)
    applicable_schemas = [s for s in issue_schemas if s.tracker_id is None or s.tracker_id == issue.tracker_id]
    metadata_schemas_data = [MetadataSchemaOut.model_validate(s).model_dump(mode="json") for s in applicable_schemas]

    # Build lookup maps for human-readable activity details
    lookup_maps: dict[str, dict[str, str]] = {
        "status_id": {str(s.id): s.name for s in lookups["statuses"]},
        "tracker_id": {str(t.id): t.name for t in lookups["trackers"]},
        "priority_id": {str(p.id): p.name for p in lookups["priorities"]},
        "assigned_to_id": {
            str(m.get("user_id", m.get("id", ""))): m.get("login", m.get("display_name", "")) for m in members
        },
        "sprint_id": {str(sp.id): sp.name for sp in sprints},
    }

    # Derive from the already-loaded watchers list (list[User])
    is_watching = any(w.id == user.id for w in watchers)

    # Relations count (list already loaded via gather above)
    relation_count = len(relations)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/detail.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "journals": journals,
            "thread_tree": thread_tree,
            "members": members,
            "lookup_maps": lookup_maps,
            "reactions_by_journal": reactions_by_journal,
            **tab_ctx,
            "relation_count": relation_count,
            "versions": versions,
            "sprints": sprints,
            "relations": relations,
            "metadata_schemas_data": metadata_schemas_data,
            "issue_metadata": issue.issue_metadata or {},
            "watchers": watchers,
            "is_watching": is_watching,
            "last_activity_at": last_activity_at,
            "activity_page": activity_page,
            "activity_per_page": activity_per_page,
            "activity_total_pages": activity_total_pages,
            "activity_total": activity_total,
            "activity_per_page_options": ACTIVITY_PER_PAGE_OPTIONS,
            "active_sprint_id": active_sprint_id,
            **lookups,
        },
    )


@short_router.get("/issue/{issue_ref}/edit/", response_class=HTMLResponse)
async def issue_edit_form(
    issue_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue edit form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    project, issue = await _resolve_issue_project(db, issue_ref, user)

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)
    versions_result = await db.execute(select(Version).where(Version.project_id == project.id).order_by(Version.name))
    versions = list(versions_result.scalars().all())

    # Load metadata schemas for this project
    from specivo.schemas.metadata_schema import MetadataSchemaOut
    from specivo.services.metadata_schema_service import MetadataSchemaService

    schema_svc = MetadataSchemaService()
    metadata_schemas = await schema_svc.list_for_project(db, project.id)
    metadata_schemas_data = [MetadataSchemaOut.model_validate(s).model_dump(mode="json") for s in metadata_schemas]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/form.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "mode": "edit",
            "members": members,
            "versions": versions,
            "metadata_schemas_data": metadata_schemas_data,
            "issue_metadata": issue.issue_metadata or {},
            **lookups,
        },
    )


@short_router.get("/issue/{issue_ref}/versions/{journal_id}/", response_class=HTMLResponse)
async def issue_description_diff(
    issue_ref: str,
    journal_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render a description diff page for a specific journal entry."""
    import difflib

    from specivo.models.journal import JournalDetail

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    project, issue = await _resolve_issue_project(db, issue_ref, user)

    journal_result = await db.execute(
        select(JournalDetail).where(
            JournalDetail.journal_id == journal_id,
            JournalDetail.prop_key == "description",
        )
    )
    detail = journal_result.scalar_one_or_none()
    if detail is None:
        raise HTTPException(status_code=404, detail="Description change not found")

    old_text = detail.old_value or ""
    new_text = detail.new_value or ""

    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="Previous",
            tofile="Current",
            lineterm="",
        )
    )

    # Load all description versions for the history table
    from specivo.models.journal import Journal
    from specivo.models.user import User as UserModel

    all_desc_changes = (
        await db.execute(
            select(
                JournalDetail.journal_id,
                JournalDetail.old_value,
                JournalDetail.new_value,
                Journal.created_at,
                Journal.user_id,
                UserModel.login,
                UserModel.display_name,
                UserModel.avatar_url,
                UserModel.preferences,
            )
            .join(Journal, JournalDetail.journal_id == Journal.id)
            .join(UserModel, Journal.user_id == UserModel.id)
            .where(JournalDetail.prop_key == "description", Journal.issue_id == issue.id)
            .order_by(Journal.created_at.desc())
        )
    ).all()

    # Check if the earliest change has old_value — if so, there was an original
    # description at issue creation that was never stored as version 0.
    # Synthesize it so the diff view shows the proper baseline.
    has_synthetic_v0 = False
    if all_desc_changes:
        earliest = all_desc_changes[-1]  # ordered DESC, so last = earliest
        if earliest[1] is not None:  # old_value is not None → original existed
            # Check if there's already a version 0 (old_value=None entry)
            has_v0 = any(row[1] is None for row in all_desc_changes)
            if not has_v0:
                has_synthetic_v0 = True

    # Build version list: newest first, numbered from len down to 1
    total_versions = len(all_desc_changes) + (1 if has_synthetic_v0 else 0)
    versions = []
    for idx, row in enumerate(all_desc_changes):
        version_num = total_versions - idx
        prefs = row[8] or {}
        versions.append(
            {
                "num": version_num,
                "journal_id": row[0],
                "author_login": row[5],
                "author_name": row[6] or row[5],
                "author_avatar": row[7],
                "author_color": prefs.get("avatar_color", ""),
                "date": row[3],
                "is_current": idx == 0,
                "is_selected": row[0] == journal_id,
            }
        )

    # Append synthetic version 0 (original description) for issues created before the fix
    if has_synthetic_v0:
        # Use the issue author (who wrote the original description), not the first editor
        author_result = await db.execute(select(UserModel).where(UserModel.id == issue.author_id))
        issue_author = author_result.scalar_one()
        author_prefs = issue_author.preferences or {}
        versions.append(
            {
                "num": 1,
                "journal_id": None,
                "author_login": issue_author.login,
                "author_name": issue_author.display_name or issue_author.login,
                "author_avatar": issue_author.avatar_url,
                "author_color": author_prefs.get("avatar_color", ""),
                "date": issue.created_at,
                "is_current": False,
                "is_selected": False,
                "is_synthetic": True,
            }
        )

    # Find selected version number for display
    selected_version = next((v for v in versions if v["is_selected"]), None)
    selected_num = selected_version["num"] if selected_version else "?"
    prev_num = selected_num - 1 if selected_num != "?" and selected_num > 1 else None

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/description_diff.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "old_text": old_text,
            "new_text": new_text,
            "selected_num": selected_num,
            "prev_num": prev_num,
            "versions": versions,
            "diff_lines": diff_lines,
        },
    )


@short_router.post("/issue/{issue_ref}/versions/{journal_id}/restore/", response_class=HTMLResponse)
async def restore_description_version(
    issue_ref: str,
    journal_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Restore a previous description version by PATCHing the issue."""
    from specivo.models.journal import JournalDetail
    from specivo.services.permission_service import check_permission

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    project, issue = await _resolve_issue_project(db, issue_ref, user)

    if not await check_permission(user, issue.project_id, "edit_issues", db):
        raise HTTPException(status_code=403, detail="Permission denied")

    detail_result = await db.execute(
        select(JournalDetail).where(
            JournalDetail.journal_id == journal_id,
            JournalDetail.prop_key == "description",
        )
    )
    detail = detail_result.scalar_one_or_none()
    if detail is None:
        raise HTTPException(status_code=404, detail="Description version not found")

    # Restore old_value as the new description
    from specivo.schemas.issue import IssueUpdate

    update_data = IssueUpdate(description=detail.old_value or "", lock_version=issue.lock_version)
    await _issue_svc.update(db, issue, update_data, user)

    return RedirectResponse(
        f"/issue/{issue_ref}/",
        status_code=302,
    )
