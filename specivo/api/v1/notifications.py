"""Notification preferences and in-app notifications API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.notification import NotificationPreference
from specivo.models.user import User
from specivo.schemas.notification import (
    MarkAllReadOut,
    NotificationListOut,
    NotificationOut,
    NotificationPreferenceOut,
    NotificationPreferenceUpdate,
    UnreadCountOut,
)
from specivo.services.notification_service import NotificationService

router = APIRouter(tags=["notifications"])

_notification_service = NotificationService()


@router.get(
    "/notification-preferences",
    response_model=list[NotificationPreferenceOut],
)
async def list_notification_preferences(
    project_key: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotificationPreferenceOut]:
    """List the current user's notification preferences.

    Optionally filter by project_key.
    """
    stmt = select(NotificationPreference).where(NotificationPreference.user_id == current_user.id)

    if project_key is not None:
        from specivo.models.project import Project

        proj_result = await db.execute(select(Project.id).where(Project.key == project_key.upper()))
        project_id = proj_result.scalar_one_or_none()
        if project_id is not None:
            stmt = stmt.where(NotificationPreference.project_id == project_id)
        else:
            return []

    stmt = stmt.order_by(NotificationPreference.event_type)
    result = await db.execute(stmt)
    prefs = list(result.scalars().all())

    return [
        NotificationPreferenceOut(
            id=p.id,
            user_id=p.user_id,
            project_id=p.project_id,
            event_type=p.event_type,
            email_enabled=p.email_enabled,
            in_app_enabled=p.in_app_enabled,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in prefs
    ]


@router.patch(
    "/notification-preferences",
    response_model=NotificationPreferenceOut,
)
async def update_notification_preference(
    data: NotificationPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationPreferenceOut:
    """Create or update a notification preference for the current user.

    Upserts based on (user_id, project_id, event_type).
    """
    stmt = select(NotificationPreference).where(
        NotificationPreference.user_id == current_user.id,
        NotificationPreference.event_type == data.event_type,
    )
    if data.project_id is not None:
        stmt = stmt.where(NotificationPreference.project_id == data.project_id)
    else:
        stmt = stmt.where(NotificationPreference.project_id.is_(None))

    result = await db.execute(stmt)
    pref = result.scalar_one_or_none()

    if pref is None:
        pref = NotificationPreference(
            user_id=current_user.id,
            project_id=data.project_id,
            event_type=data.event_type,
            email_enabled=data.email_enabled,
            in_app_enabled=data.in_app_enabled,
        )
        db.add(pref)
    else:
        pref.email_enabled = data.email_enabled
        pref.in_app_enabled = data.in_app_enabled

    await db.flush()
    await db.refresh(pref)

    return NotificationPreferenceOut(
        id=pref.id,
        user_id=pref.user_id,
        project_id=pref.project_id,
        event_type=pref.event_type,
        email_enabled=pref.email_enabled,
        in_app_enabled=pref.in_app_enabled,
        created_at=pref.created_at,
        updated_at=pref.updated_at,
    )


# ---------------------------------------------------------------------------
# In-app notification inbox endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/notifications",
    response_model=NotificationListOut,
)
async def list_notifications(
    unread_only: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListOut:
    """List the current user's in-app notifications (newest first)."""
    items, total = await _notification_service.list_notifications(
        db,
        user_id=current_user.id,
        unread_only=unread_only,
        offset=offset,
        limit=limit,
    )
    return NotificationListOut(
        items=[
            NotificationOut(
                id=n.id,
                user_id=n.user_id,
                event_type=n.event_type,
                entity_type=n.entity_type,
                entity_id=n.entity_id,
                project_id=n.project_id,
                actor_id=n.actor_id,
                title=n.title,
                body=n.body,
                is_read=n.is_read,
                read_at=n.read_at,
                created_at=n.created_at,
            )
            for n in items
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountOut,
)
async def get_unread_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountOut:
    """Return the number of unread notifications for the current user."""
    count = await _notification_service.get_unread_count(db, current_user.id)
    return UnreadCountOut(count=count)


@router.patch(
    "/notifications/{notification_id}/read",
    response_model=NotificationOut,
)
async def mark_notification_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationOut:
    """Mark a single notification as read."""
    notif = await _notification_service.mark_read(db, notification_id, current_user.id)
    if notif is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return NotificationOut(
        id=notif.id,
        user_id=notif.user_id,
        event_type=notif.event_type,
        entity_type=notif.entity_type,
        entity_id=notif.entity_id,
        project_id=notif.project_id,
        actor_id=notif.actor_id,
        title=notif.title,
        body=notif.body,
        is_read=notif.is_read,
        read_at=notif.read_at,
        created_at=notif.created_at,
    )


@router.post(
    "/notifications/mark-all-read",
    response_model=MarkAllReadOut,
)
async def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MarkAllReadOut:
    """Mark all of the current user's notifications as read."""
    count = await _notification_service.mark_all_read(db, current_user.id)
    return MarkAllReadOut(marked=count)
