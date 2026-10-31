"""Outgoing webhook endpoints: CRUD, the delivery log and a test event.

Every route needs ``manage_project``. A private project the
caller cannot reach answers 404, like every project route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.webhook import (
    WebhookCreate,
    WebhookDeliveryOut,
    WebhookOut,
    WebhookSecretOut,
    WebhookUpdate,
)
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.webhook_service import WebhookService

router = APIRouter(prefix="/projects/{project_key}/webhooks", tags=["webhooks"])
_project_service = ProjectService()
_webhook_service = WebhookService()


async def _project_for(project_key: str, user: User, db: AsyncSession) -> Project:
    project = await _project_service.get_by_key(db, project_key)
    await _project_service.require_project_access(db, project, user)
    return project


def _require_admin_for_private(user: User, private: bool) -> None:
    """Refuse a non-admin who would set up, or change, a webhook that sends private content.

    Only admins see every private issue and note, so only they may decide
    where such content is sent. Otherwise a manager could subscribe, or point
    an admin's webhook at their own server, and read issues hidden from them.
    """
    if private and not user.is_admin:
        raise PermissionDeniedError("Only an administrator can manage a webhook that includes private content")


async def _require_manage_project(user: User, project_id: int, db: AsyncSession) -> None:
    """Raise 403 if user lacks manage_project permission for the project."""
    if not await check_permission(user, project_id, Permission.MANAGE_PROJECT, db):
        raise PermissionDeniedError("Permission required: manage_project")


@router.get("/", response_model=list[WebhookOut])
async def list_webhooks(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> list[WebhookOut]:
    """List all outgoing webhooks for a project.

    Needs ``manage_project`` like every other route here: receiver URLs often
    carry tokens.
    """
    project = await _project_for(project_key, current_user, db)
    await _require_manage_project(current_user, project.id, db)
    webhooks = await _webhook_service.list_for_project(db, project.id)
    return [WebhookOut.model_validate(wh) for wh in webhooks]


@router.post("/", response_model=WebhookSecretOut, status_code=status.HTTP_201_CREATED)
async def register_webhook(
    project_key: str,
    data: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> WebhookSecretOut:
    """Register an outgoing webhook.

    Leave ``secret`` out to have one generated. The response carries the
    secret in clear; it is never shown again (set a new one with ``PATCH``
    and ``rotate_secret``).
    """
    project = await _project_for(project_key, current_user, db)
    await _require_manage_project(current_user, project.id, db)
    _require_admin_for_private(current_user, data.include_private)
    webhook, secret = await _webhook_service.register(
        db,
        project_id=project.id,
        url=data.url,
        events=data.events,
        secret=data.secret,
        include_private=data.include_private,
    )
    await db.refresh(webhook)
    return WebhookSecretOut(**WebhookOut.model_validate(webhook).model_dump(), secret=secret)


@router.patch("/{webhook_id}/", response_model=WebhookSecretOut)
async def update_webhook(
    project_key: str,
    webhook_id: int,
    data: WebhookUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> WebhookSecretOut:
    """Update an outgoing webhook. ``secret`` in the response is set only when the secret changed."""
    project = await _project_for(project_key, current_user, db)
    await _require_manage_project(current_user, project.id, db)
    current = await _webhook_service.get(db, webhook_id, project.id)
    _require_admin_for_private(current_user, current.include_private or data.include_private is True)
    webhook, secret = await _webhook_service.update(db, webhook_id, project.id, data.model_dump(exclude_unset=True))
    await db.refresh(webhook)
    return WebhookSecretOut(**WebhookOut.model_validate(webhook).model_dump(), secret=secret)


@router.delete("/{webhook_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    project_key: str,
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> None:
    """Delete an outgoing webhook and its delivery log."""
    project = await _project_for(project_key, current_user, db)
    await _require_manage_project(current_user, project.id, db)
    await _webhook_service.delete(db, webhook_id, project.id)


@router.get("/{webhook_id}/deliveries/", response_model=PaginatedResponse[WebhookDeliveryOut])
async def list_deliveries(
    project_key: str,
    webhook_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> PaginatedResponse[WebhookDeliveryOut]:
    """The webhook's delivery log, newest first: status, attempts, last response code and an excerpt.

    Succeeded and failed deliveries are deleted after 30 days; pending ones are kept until they settle.
    """
    project = await _project_for(project_key, current_user, db)
    await _require_manage_project(current_user, project.id, db)
    webhook = await _webhook_service.get(db, webhook_id, project.id)
    rows, total = await _webhook_service.list_deliveries(db, webhook, offset=offset, limit=limit)
    return PaginatedResponse[WebhookDeliveryOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[WebhookDeliveryOut.model_validate(r) for r in rows],
    )


@router.post("/{webhook_id}/test/", response_model=WebhookDeliveryOut, status_code=status.HTTP_202_ACCEPTED)
async def send_test_event(
    project_key: str,
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> WebhookDeliveryOut:
    """Send a ``ping`` event to the webhook, even when it is disabled; returns the queued delivery."""
    project = await _project_for(project_key, current_user, db)
    await _require_manage_project(current_user, project.id, db)
    webhook = await _webhook_service.get(db, webhook_id, project.id)
    delivery = await _webhook_service.send_test(db, webhook, current_user)
    await db.refresh(delivery)
    return WebhookDeliveryOut.model_validate(delivery)
