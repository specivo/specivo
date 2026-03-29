"""Outgoing webhook CRUD endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.webhook import WebhookCreate, WebhookOut, WebhookUpdate
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.webhook_service import WebhookService

router = APIRouter(prefix="/projects/{project_key}/webhooks", tags=["webhooks"])
_project_service = ProjectService()
_webhook_service = WebhookService()


async def _require_manage_project(user: User, project_id: int, db: AsyncSession) -> None:
    """Raise 403 if user lacks manage_project permission for the project."""
    if not await check_permission(user, project_id, "manage_project", db):
        raise PermissionDeniedError("Permission required: manage_project")


async def _require_project_member(user: User, project_id: int, db: AsyncSession) -> None:
    """Raise 403 if user is not a project member (admins always pass)."""
    if user.is_admin:
        return
    from sqlalchemy import select

    from specivo.models.member import Member

    result = await db.execute(select(Member.id).where(Member.user_id == user.id, Member.project_id == project_id))
    if result.scalar_one_or_none() is None:
        raise PermissionDeniedError("Project membership required")


@router.get("", response_model=list[WebhookOut])
async def list_webhooks(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookOut]:
    """List all outgoing webhooks for a project."""
    project = await _project_service.get_by_key(db, project_key)
    await _require_project_member(current_user, project.id, db)
    webhooks = await _webhook_service.list_for_project(db, project.id)
    return [WebhookOut.model_validate(wh) for wh in webhooks]


@router.post("", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
async def register_webhook(
    project_key: str,
    data: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookOut:
    """Register a new outgoing webhook for a project."""
    project = await _project_service.get_by_key(db, project_key)
    await _require_manage_project(current_user, project.id, db)
    webhook = await _webhook_service.register(
        db,
        project_id=project.id,
        url=data.url,
        secret=data.secret,
        events=data.events,
    )
    return WebhookOut.model_validate(webhook)


@router.patch("/{webhook_id}", response_model=WebhookOut)
async def update_webhook(
    project_key: str,
    webhook_id: int,
    data: WebhookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookOut:
    """Update an outgoing webhook."""
    project = await _project_service.get_by_key(db, project_key)
    await _require_manage_project(current_user, project.id, db)
    update_data = data.model_dump(exclude_unset=True)
    webhook = await _webhook_service.update(db, webhook_id, project.id, update_data)
    return WebhookOut.model_validate(webhook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    project_key: str,
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an outgoing webhook."""
    project = await _project_service.get_by_key(db, project_key)
    await _require_manage_project(current_user, project.id, db)
    await _webhook_service.delete(db, webhook_id, project.id)
