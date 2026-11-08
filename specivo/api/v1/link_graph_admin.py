"""Link graph administration: start a full rebuild, follow it, and read the graph's figures.

Per-project routes require ``manage_project`` (or a global admin); the
instance-wide ones require a global admin. A rebuild runs as a Celery task;
only one runs per scope (see ``link_graph_admin_service``), and a second
request while one runs is answered 409 with the running one's status.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery.result import AsyncResult  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.locks import FINISHED_TASK_STATES, LockUnavailableError, TokenLock, acquire_run_lock
from specivo.core.redis import get_redis
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.api_key_scope import is_global_admin
from specivo.services.link_graph_admin_service import (
    LINK_REBUILD_LOCK_TTL,
    last_result,
    project_stats,
    rebuild_lock_key,
)
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.tasks import celery_app

router = APIRouter(tags=["link-graph-admin"])
_project_service = ProjectService()


def _require_admin(user: User) -> None:
    if not is_global_admin(user):
        raise PermissionDeniedError("Administrator access required")


async def _require_project_manage(project_key: str, user: User, db: AsyncSession) -> Project:
    project = await _project_service.get_by_key(db, project_key.upper())
    if not is_global_admin(user) and not await check_permission(user, project.id, Permission.MANAGE_PROJECT, db):
        raise PermissionDeniedError("manage_project permission required")
    return project


async def _lock(project_id: int | None) -> TokenLock:
    return TokenLock(await get_redis(), rebuild_lock_key(project_id))


def _task_state(task_id: str) -> tuple[str, dict | None]:
    result = AsyncResult(task_id, app=celery_app)
    info = result.info
    return result.state, info if isinstance(info, dict) and result.state == "PROGRESS" else None


async def _status_payload(db: AsyncSession, project_id: int | None) -> dict[str, Any]:
    """The run in progress (from the Redis run lock and the task's meta) and the last result."""
    task_id: str | None = None
    lock_error = False
    try:
        task_id = await (await _lock(project_id)).holder()
    except LockUnavailableError:
        lock_error = True
    state, progress = ("IDLE", None)
    if task_id:
        state, progress = _task_state(task_id)
    return {
        "task_id": task_id,
        "state": state,
        "running": bool(task_id) and state not in FINISHED_TASK_STATES,
        "progress": progress,
        "lock_unavailable": lock_error,
        "last_result": await last_result(db, project_id),
    }


async def _dispatch(db: AsyncSession, project_id: int | None, user: User) -> dict[str, Any]:
    from specivo.tasks.entity_links import rebuild_link_graph_task

    task_id = str(uuid.uuid4())
    try:
        lock = await _lock(project_id)
        acquired = await acquire_run_lock(lock, task_id, LINK_REBUILD_LOCK_TTL, lambda holder: _task_state(holder)[0])
    except LockUnavailableError as exc:
        raise AppError(
            code="lock_unavailable",
            message="Cannot start a rebuild right now: the run lock is unavailable. Try again later.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    if not acquired:
        raise AppError(
            code="link_rebuild_running",
            message="A link graph rebuild is already running.",
            status_code=status.HTTP_409_CONFLICT,
            details=await _status_payload(db, project_id),
        )
    try:
        rebuild_link_graph_task.apply_async(args=[project_id, user.login], task_id=task_id)
    except Exception as exc:
        await lock.release(task_id)
        raise AppError(
            code="queue_unavailable",
            message="Cannot start a rebuild right now: the task queue is unavailable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    return {"task_id": task_id, "state": "PENDING", "running": True}


# ---------------------------------------------------------------------------
# Per project (manage_project)
# ---------------------------------------------------------------------------


@router.post("/projects/{project_key}/links/rebuild/", status_code=status.HTTP_202_ACCEPTED)
async def rebuild_project_links(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> dict:
    """Start rebuilding every stored link of the project (issue descriptions, comments, wiki pages)."""
    project = await _require_project_manage(project_key, current_user, db)
    return await _dispatch(db, project.id, current_user)


@router.get("/projects/{project_key}/links/rebuild/status/")
async def rebuild_project_links_status(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> dict:
    """Whether a rebuild runs (with ``progress``: processed / total sources), its last result, and the stats."""
    project = await _require_project_manage(project_key, current_user, db)
    payload = await _status_payload(db, project.id)
    payload["stats"] = await project_stats(db, current_user, project.id)
    return payload


@router.get("/projects/{project_key}/links/stats/")
async def project_link_stats(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> dict:
    """Stored links of the project by kind and source, structured links, unresolved targets, last rebuild."""
    project = await _require_project_manage(project_key, current_user, db)
    return await project_stats(db, current_user, project.id)


# ---------------------------------------------------------------------------
# Instance (global admin)
# ---------------------------------------------------------------------------


@router.post("/admin/links/rebuild/", status_code=status.HTTP_202_ACCEPTED)
async def rebuild_instance_links(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> dict:
    """Start rebuilding every stored link of the instance."""
    _require_admin(current_user)
    return await _dispatch(db, None, current_user)


@router.get("/admin/links/rebuild/status/")
async def rebuild_instance_links_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> dict:
    _require_admin(current_user)
    payload = await _status_payload(db, None)
    payload["stats"] = await project_stats(db, current_user, None)
    return payload
