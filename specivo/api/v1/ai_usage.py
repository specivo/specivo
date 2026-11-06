"""AI usage entries API: log, list, read, edit, exclude and delete, the sibling of ``time_entries``.

- ``GET/POST /projects/{key}/ai-usage/``: the project's entries the caller may
  read; log an entry there (``on_behalf_of`` and a known ``cost`` accepted).
- ``GET/POST /my/ai-usage/``: the entries logged in the caller's name,
  unassigned ones included; log an unassigned entry.
- ``GET/PATCH/DELETE /ai-usage/{id}/``, ``POST /ai-usage/{id}/exclude/`` and
  ``/include/``.

Who may read and write what is decided by :class:`AiUsageService`; see its
module docstring. An entry the caller cannot read is 404, one they can read
but not change 403. Entries written here have the source ``api`` when the
request authenticates with an API key, ``manual`` otherwise. Reporter and
import entries (``POST /ai-usage/ingest/``) only take a new project, issue or
comment, and are excluded rather than deleted.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status

from specivo.core.constants import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError
from specivo.core.security import get_current_user
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.user import User
from specivo.schemas.ai_usage import (
    AiUsageCostType,
    AiUsageCreate,
    AiUsageEntryOut,
    AiUsageListResponse,
    AiUsageTokens,
    AiUsageUpdate,
    AiUsageUserOut,
)
from specivo.schemas.money import MoneyOut
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService, EntryReferences
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["ai-usage"])
_service = AiUsageService()
_projects = ProjectService()

_REQUIRED_ON_PATCH = ("tool", "model", "usage_date", "request_count")


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _user_out(refs: EntryReferences, user_id: int) -> AiUsageUserOut:
    user = refs.users.get(user_id)
    if user is None:
        return AiUsageUserOut(id=user_id, name="", login="")
    return AiUsageUserOut(id=user.id, name=user.display_name, login=user.login)


def entry_out(entry: AiUsageEntry, refs: EntryReferences) -> AiUsageEntryOut:
    """An entry as the API shows it, with the references loaded by ``AiUsageService.references``."""
    issue_key = refs.issue_keys.get(entry.issue_id) if entry.issue_id is not None else None
    return AiUsageEntryOut(
        id=entry.id,
        project_id=entry.project_id,
        project_key=refs.project_keys.get(entry.project_id) if entry.project_id is not None else None,
        issue_id=entry.issue_id if issue_key is not None else None,
        issue_key=issue_key,
        user=_user_out(refs, entry.user_id),
        reporter=_user_out(refs, entry.reporter_user_id),
        reporter_api_key_id=entry.reporter_api_key_id,
        source=entry.source,
        locked=entry.is_locked,
        attribution_method=entry.attribution_method,
        tool=entry.tool_slug,
        provider=entry.provider,
        model=entry.model,
        usage_date=entry.usage_date,
        started_at=entry.started_at,
        ended_at=entry.ended_at,
        request_count=entry.request_count,
        tokens=AiUsageTokens(**entry.tokens()),
        long_context_tokens=AiUsageTokens(**entry.long_context) if entry.long_context else None,
        cost_type=entry.cost_type,
        subscription_id=entry.subscription_id,
        cost_basis=entry.cost_basis,
        marginal_cost=MoneyOut.from_money(entry.marginal) if entry.marginal is not None else None,
        api_equivalent_cost=(MoneyOut.from_money(entry.api_equivalent) if entry.api_equivalent is not None else None),
        reported_cost=MoneyOut.from_money(entry.reported_cost) if entry.reported_cost is not None else None,
        price_id=entry.price_id,
        priced_at=entry.priced_at,
        external_session_id=entry.external_session_id,
        git_branch=entry.git_branch,
        repo_hint=entry.repo_hint,
        client_name=entry.client_name,
        client_version=entry.client_version,
        comments=entry.comments,
        idempotency_key=entry.idempotency_key,
        revision=entry.revision,
        excluded=entry.is_excluded,
        excluded_at=entry.excluded_at,
        excluded_by_id=entry.excluded_by_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


async def _one_out(db: DbSession, viewer: User, entry: AiUsageEntry) -> AiUsageEntryOut:
    await db.flush()
    await db.refresh(entry)
    return entry_out(entry, await _service.references(db, viewer, [entry]))


async def _page_out(
    db: DbSession, viewer: User, entries: list[AiUsageEntry], total: int, offset: int, limit: int
) -> AiUsageListResponse:
    refs = await _service.references(db, viewer, entries)
    return AiUsageListResponse(
        total_count=total, offset=offset, limit=limit, items=[entry_out(e, refs) for e in entries]
    )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _source(request: Request) -> tuple[str, int | None]:
    """``api`` with the key's id for an API-key request, else ``manual`` (a person, signed in)."""
    key_id = getattr(request.state, "api_key_id", None)
    return ("api", key_id) if key_id is not None else ("manual", None)


def input_of(data: AiUsageCreate) -> AiUsageInput:
    """The service input of a create request (without its project)."""
    return AiUsageInput(
        tool=data.tool,
        model=data.model,
        usage_date=data.usage_date,
        provider=data.provider,
        tokens=data.tokens.model_dump(),
        long_context=data.long_context_tokens.model_dump() if data.long_context_tokens is not None else None,
        started_at=data.started_at,
        ended_at=data.ended_at,
        request_count=data.request_count,
        cost_type=data.cost_type,
        reported_cost=data.cost.to_money() if data.cost is not None else None,
        issue_id=data.issue_id,
        external_session_id=data.external_session_id,
        git_branch=data.git_branch,
        repo_hint=data.repo_hint,
        comments=data.comments,
    )


async def _changes(db: DbSession, viewer: User, entry: AiUsageEntry, data: AiUsageUpdate) -> dict[str, Any]:
    """The PATCH body as ``AiUsageService.update`` changes; only the fields sent."""
    sent = data.model_fields_set
    for name in _REQUIRED_ON_PATCH:
        if name in sent and getattr(data, name) is None:
            raise AppError(code="ai_usage_invalid", message=f"{name} cannot be null", status_code=422, field=name)
    changes: dict[str, Any] = {}
    if "project_key" in sent:
        changes["project_id"] = await _service.project_id_for_key(db, viewer, data.project_key)
    plain = (
        "issue_id",
        "comments",
        "tool",
        "model",
        "provider",
        "usage_date",
        "started_at",
        "ended_at",
        "request_count",
        "cost_type",
        "external_session_id",
        "git_branch",
        "repo_hint",
    )
    for name in plain:
        if name in sent:
            changes[name] = getattr(data, name)
    if "tokens" in sent:
        sent_tokens = data.tokens.model_dump(exclude_unset=True) if data.tokens is not None else {}
        changes["tokens"] = {**entry.tokens(), **sent_tokens}
    if "long_context_tokens" in sent:
        if data.long_context_tokens is None:
            changes["long_context"] = None
        else:
            changes["long_context"] = {
                **(entry.long_context or {}),
                **data.long_context_tokens.model_dump(exclude_unset=True),
            }
    if "cost" in sent:
        changes["reported_cost"] = data.cost.to_money() if data.cost is not None else None
    return changes


# ---------------------------------------------------------------------------
# Project entries
# ---------------------------------------------------------------------------


@router.get("/projects/{project_key}/ai-usage/", response_model=AiUsageListResponse)
async def list_project_ai_usage(
    project_key: str,
    db: DbSession,
    user_id: int | None = Query(default=None, description="Only entries logged in this user's name"),
    tool: str | None = Query(default=None, max_length=50),
    model: str | None = Query(default=None, max_length=200, description="Also matches the aliases of its prices"),
    cost_type: AiUsageCostType | None = Query(default=None),
    issue_id: int | None = Query(default=None),
    source: str | None = Query(default=None, description="manual, api, mcp, reporter or import"),
    excluded: bool | None = Query(default=None, description="true: only excluded entries; false: only counted ones"),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    current_user: User = Depends(get_current_user),
) -> AiUsageListResponse:
    """The project's AI usage entries the caller may read, newest day first.

    Everyone reads the entries logged in their own name and those they
    reported; ``view_ai_usage`` with visibility ``all`` shows everyone's.
    Entries on issues the caller cannot see are left out unless they are the
    caller's own.
    """
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, current_user)
    entries, total = await _service.list_entries(
        db,
        current_user,
        project=project,
        user_id=user_id,
        date_from=from_date,
        date_to=to_date,
        tool=tool,
        model=model,
        cost_type=cost_type,
        issue_id=issue_id,
        source=source,
        excluded=excluded,
        offset=offset,
        limit=limit,
    )
    return await _page_out(db, current_user, entries, total, offset, limit)


@router.post(
    "/projects/{project_key}/ai-usage/",
    response_model=AiUsageEntryOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_ai_usage(
    project_key: str,
    data: AiUsageCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_current_user),
) -> AiUsageEntryOut:
    """Log an AI usage entry in the project.

    Needs ``log_ai_usage`` there for one's own usage. With ``on_behalf_of``
    it needs ``log_ai_usage_for_others`` and the user must be an active member
    of the project; without that permission every login gets the same 403.
    """
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, current_user)
    source, key_id = _source(request)
    entry = await _service.create_for(
        db,
        current_user,
        input_of(data),
        project=project,
        on_behalf_of=data.on_behalf_of,
        source=source,
        api_key_id=key_id,
        request=request,
    )
    return await _one_out(db, current_user, entry)


# ---------------------------------------------------------------------------
# My entries
# ---------------------------------------------------------------------------


@router.get("/my/ai-usage/", response_model=AiUsageListResponse)
async def list_my_ai_usage(
    db: DbSession,
    project_key: str | None = Query(default=None, description="Only this project's entries"),
    unassigned: bool | None = Query(default=None, description="true: only entries without a project"),
    tool: str | None = Query(default=None, max_length=50),
    model: str | None = Query(default=None, max_length=200),
    cost_type: AiUsageCostType | None = Query(default=None),
    source: str | None = Query(default=None),
    excluded: bool | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    current_user: User = Depends(get_current_user),
) -> AiUsageListResponse:
    """The entries logged in the caller's name, unassigned ones included, newest day first.

    A project-limited API key sees unassigned entries only where it reported them.
    """
    project = None
    if project_key:
        project = await _projects.get_by_key(db, project_key.upper())
        await _projects.require_project_access(db, project, current_user)
    entries, total = await _service.list_entries(
        db,
        current_user,
        project=project,
        user_id=current_user.id,
        date_from=from_date,
        date_to=to_date,
        tool=tool,
        model=model,
        cost_type=cost_type,
        source=source,
        excluded=excluded,
        unassigned=unassigned,
        offset=offset,
        limit=limit,
    )
    return await _page_out(db, current_user, entries, total, offset, limit)


@router.post("/my/ai-usage/", response_model=AiUsageEntryOut, status_code=status.HTTP_201_CREATED)
async def create_my_ai_usage(
    data: AiUsageCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_current_user),
) -> AiUsageEntryOut:
    """Log an unassigned AI usage entry (no project); attribute it later with PATCH ``project_key``.

    Needs no permission for one's own usage; ``on_behalf_of`` is for
    administrators only here. ``issue_id`` needs a project: use the project route.
    """
    source, key_id = _source(request)
    entry = await _service.create_for(
        db,
        current_user,
        input_of(data),
        project=None,
        on_behalf_of=data.on_behalf_of,
        source=source,
        api_key_id=key_id,
        request=request,
    )
    return await _one_out(db, current_user, entry)


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


@router.get("/ai-usage/{entry_id}/", response_model=AiUsageEntryOut)
async def get_ai_usage(entry_id: int, db: DbSession, current_user: User = Depends(get_current_user)) -> AiUsageEntryOut:
    """One entry; 404 unless the caller may read it."""
    entry = await _service.get_visible(db, entry_id, current_user)
    return entry_out(entry, await _service.references(db, current_user, [entry]))


@router.patch("/ai-usage/{entry_id}/", response_model=AiUsageEntryOut)
async def update_ai_usage(
    entry_id: int,
    data: AiUsageUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_current_user),
) -> AiUsageEntryOut:
    """Change an entry: its user, or ``manage_ai_usage`` in its project.

    Moving it (``project_key``) is checked like logging it there in its user's
    name. Reporter and import entries keep their usage: 422
    ``ai_usage_field_locked`` for anything but ``project_key``, ``issue_id``
    and ``comments``. Changing the usage re-prices the entry. An edit by
    someone else than the entry's user is audited.
    """
    entry = await _service.get_visible(db, entry_id, current_user)
    changes = await _changes(db, current_user, entry, data)
    entry = await _service.update(db, current_user, entry, changes, request=request)
    return await _one_out(db, current_user, entry)


@router.delete("/ai-usage/{entry_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_usage(
    entry_id: int, request: Request, db: DbSession, current_user: User = Depends(get_current_user)
) -> None:
    """Delete an entry; audited. Reporter and import entries: administrators only (exclude them instead)."""
    entry = await _service.get_visible(db, entry_id, current_user)
    await _service.delete(db, current_user, entry, request=request)


@router.post("/ai-usage/{entry_id}/exclude/", response_model=AiUsageEntryOut)
async def exclude_ai_usage(
    entry_id: int, request: Request, db: DbSession, current_user: User = Depends(get_current_user)
) -> AiUsageEntryOut:
    """Leave an entry out of reports; audited. A reporter's later updates keep it excluded."""
    entry = await _service.get_visible(db, entry_id, current_user)
    entry = await _service.set_excluded(db, current_user, entry, True, request=request)
    return await _one_out(db, current_user, entry)


@router.post("/ai-usage/{entry_id}/include/", response_model=AiUsageEntryOut)
async def include_ai_usage(
    entry_id: int, request: Request, db: DbSession, current_user: User = Depends(get_current_user)
) -> AiUsageEntryOut:
    """Count an excluded entry in reports again; audited."""
    entry = await _service.get_visible(db, entry_id, current_user)
    entry = await _service.set_excluded(db, current_user, entry, False, request=request)
    return await _one_out(db, current_user, entry)
