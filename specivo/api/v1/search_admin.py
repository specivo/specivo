"""Search FTS admin API — instance default + per-project language and reindex.

Instance-level routes require global admin; per-project routes require
``manage_project``. Reindex runs as a background Celery task; status is polled.
"""

from __future__ import annotations

import json

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import _ALLOWED_FTS_LANGUAGES
from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError, ValidationError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.settings_service import SettingsService
from specivo.tasks import celery_app
from specivo.tasks.search import (
    last_result_key,
    reindex_fts_task,
    reindex_needed_key,
    running_task_key,
)

router = APIRouter(tags=["search-admin"])
_project_service = ProjectService()
_settings = SettingsService()

_INSTANCE_DEFAULT_KEY = "search_fts_language"
_RUNNING_STATES = {"STARTED", "PROGRESS", "RETRY"}


class InstanceLanguageIn(BaseModel):
    language: str


class ProjectLanguageIn(BaseModel):
    language: str | None = None  # None / "" / "inherit" => inherit instance default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise PermissionDeniedError("Administrator access required")


async def _require_project_manage(project_key: str, user: User, db: AsyncSession) -> Project:
    project = await _project_service.get_by_key(db, project_key.upper())
    if not user.is_admin and not await check_permission(user, project.id, "manage_project", db):
        raise PermissionDeniedError("manage_project permission required")
    return project


def _validate_language(value: str) -> str:
    if value not in _ALLOWED_FTS_LANGUAGES:
        raise ValidationError(
            message=f"Invalid FTS language {value!r}. Must be one of: {', '.join(sorted(_ALLOWED_FTS_LANGUAGES))}"
        )
    return value


async def _last_result(db: AsyncSession, project_id: int | None) -> dict | None:
    raw = await _settings.get(db, last_result_key(project_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def _running(db: AsyncSession, project_id: int | None) -> tuple[bool, str | None]:
    task_id = await _settings.get(db, running_task_key(project_id))
    if not task_id:
        return False, None
    state = AsyncResult(task_id, app=celery_app).state
    return state in _RUNNING_STATES, task_id


async def _reindex_needed(db: AsyncSession, project_id: int | None) -> bool:
    return (await _settings.get(db, reindex_needed_key(project_id))) == "1"


async def _dispatch_reindex(db: AsyncSession, project_id: int | None) -> dict:
    running, _ = await _running(db, project_id)
    if running:
        raise ValidationError(message="A reindex is already running for this scope")
    async_result = reindex_fts_task.delay(project_id)
    await _settings.set_many(db, {running_task_key(project_id): async_result.id})
    await db.commit()
    return {"task_id": async_result.id, "state": "PENDING"}


async def _status_payload(db: AsyncSession, project_id: int | None) -> dict:
    running, task_id = await _running(db, project_id)
    meta: dict | None = None
    state = "IDLE"
    if task_id:
        ar = AsyncResult(task_id, app=celery_app)
        state = ar.state
        info = ar.info
        if isinstance(info, dict):
            meta = info
    return {
        "task_id": task_id,
        "state": state,
        "running": running,
        "progress": meta,
        "last_result": await _last_result(db, project_id),
        "reindex_needed": await _reindex_needed(db, project_id),
    }


# ---------------------------------------------------------------------------
# Instance-level (global admin)
# ---------------------------------------------------------------------------


@router.get("/admin/search/fts/")
async def get_instance_fts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(current_user)
    language = await _settings.get(db, _INSTANCE_DEFAULT_KEY, "english")
    return {
        "language": language,
        "allowed": sorted(_ALLOWED_FTS_LANGUAGES),
        **await _status_payload(db, None),
    }


@router.put("/admin/search/fts/language/")
async def set_instance_fts(
    data: InstanceLanguageIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(current_user)
    lang = _validate_language(data.language)
    await _settings.set_many(db, {_INSTANCE_DEFAULT_KEY: lang, reindex_needed_key(None): "1"})
    await db.commit()
    return await get_instance_fts(current_user, db)


@router.post("/admin/search/reindex/", status_code=status.HTTP_202_ACCEPTED)
async def reindex_instance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(current_user)
    return await _dispatch_reindex(db, None)


@router.get("/admin/search/reindex/status/")
async def reindex_instance_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_admin(current_user)
    return await _status_payload(db, None)


# ---------------------------------------------------------------------------
# Per-project (manage_project)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_key}/search/fts/")
async def get_project_fts(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _require_project_manage(project_key, current_user, db)
    instance_default = await _settings.get(db, _INSTANCE_DEFAULT_KEY, "english")
    return {
        "language": project.fts_language,  # None = inherit
        "effective": project.fts_language or instance_default,
        "instance_default": instance_default,
        "allowed": sorted(_ALLOWED_FTS_LANGUAGES),
        **await _status_payload(db, project.id),
    }


@router.put("/projects/{project_key}/search/fts/language/")
async def set_project_fts(
    project_key: str,
    data: ProjectLanguageIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _require_project_manage(project_key, current_user, db)
    value = (data.language or "").strip().lower()
    project.fts_language = None if value in ("", "inherit") else _validate_language(value)
    db.add(project)
    await _settings.set_many(db, {reindex_needed_key(project.id): "1"})
    await db.commit()
    return await get_project_fts(project_key, current_user, db)


@router.post("/projects/{project_key}/search/reindex/", status_code=status.HTTP_202_ACCEPTED)
async def reindex_project(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _require_project_manage(project_key, current_user, db)
    return await _dispatch_reindex(db, project.id)


@router.get("/projects/{project_key}/search/reindex/status/")
async def reindex_project_status(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _require_project_manage(project_key, current_user, db)
    return await _status_payload(db, project.id)
