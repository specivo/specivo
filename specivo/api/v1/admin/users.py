"""Admin users API — list, create, reset password, lock/unlock."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.core.exceptions import AppError, ConflictError, NotFoundError
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.schemas.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from specivo.schemas.user import UserCreate, UserOut
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_utils import hash_password
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

router = APIRouter(tags=["admin-users"])
_audit = SecurityAuditService()


class ResetPasswordRequest(BaseModel):
    password: str = Field(max_length=128)


@router.get("/admin/users/", response_model=list[UserOut])
async def list_users(
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
    q: str = Query("", max_length=100),
    status: str = Query("", max_length=20),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> list[UserOut]:
    """List all users (admin only). Optional search by login/display_name."""
    stmt = select(User)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(User.login.ilike(pattern), User.display_name.ilike(pattern)))
    if status:
        stmt = stmt.where(User.status == status)
    stmt = stmt.order_by(User.id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.post("/admin/users/", response_model=UserOut, status_code=201)
async def create_user(
    data: UserCreate,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """Create a new user (admin only)."""
    from specivo.core.config import get_settings

    settings = get_settings()
    if data.password and len(data.password) < settings.password_min_length:
        raise AppError(
            code="validation_error",
            message=f"Password must be at least {settings.password_min_length} characters",
            status_code=422,
            field="password",
        )

    existing = await db.execute(
        select(User).where(
            or_(
                func.lower(User.login) == data.login.lower(),
                func.lower(User.email) == data.email.lower(),
            )
        )
    )
    if existing.scalar_one_or_none():
        raise ConflictError(message="A user with this login or email already exists")

    now = utcnow()
    user = User(
        login=data.login,
        email=data.email,
        password_hash=hash_password(data.password) if data.password else None,
        display_name=data.display_name,
        language=data.language,
        timezone=data.timezone,
        status=data.status,
        is_admin=False,  # admin promotion only via CLI inside container
        is_service_account=data.is_service_account,
        email_verified_at=now if data.status == "active" else None,
        password_changed_at=now if data.password else None,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=admin.id,
            details={"action": "user_created", "target_user_id": user.id, "target_login": user.login},
            request=request,
        )
    except Exception:
        pass

    await db.commit()  # commit before response to avoid reload race condition
    return UserOut.model_validate(user)


@router.post("/admin/users/{user_id}/reset-password/")
async def reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reset a user's password (admin only)."""
    from specivo.core.config import get_settings

    settings = get_settings()
    if len(body.password) < settings.password_min_length:
        raise AppError(
            code="validation_error",
            message=f"Password must be at least {settings.password_min_length} characters",
            status_code=422,
            field="password",
        )
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="User not found")

    user.password_hash = hash_password(body.password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    await db.flush()

    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=admin.id,
            details={"action": "password_reset", "target_user_id": user.id, "target_login": user.login},
            request=request,
        )
    except Exception:
        pass

    return {"detail": "Password reset successfully"}


# ---------------------------------------------------------------------------
# Lock / unlock
# ---------------------------------------------------------------------------


class LockUserRequest(BaseModel):
    """Optional body for the lock endpoint."""

    locked_until: datetime | None = Field(
        default=None,
        description="If provided, lock expires at this UTC datetime. Otherwise permanent.",
    )


@router.post("/admin/users/{user_id}/lock/", response_model=UserOut)
async def lock_user(
    user_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
    body: LockUserRequest | None = None,
) -> UserOut:
    """Lock a user account (admin only). Cannot lock yourself."""
    if user_id == admin.id:
        raise AppError(code="self_lock", message="Cannot lock your own account", status_code=400)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="User not found")

    user.status = "locked"
    user.locked_until = body.locked_until if body else None
    await db.flush()
    await db.refresh(user)

    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=admin.id,
            details={"action": "user_locked", "target_user_id": user.id, "target_login": user.login},
            request=request,
        )
    except Exception:
        pass

    return UserOut.model_validate(user)


@router.post("/admin/users/{user_id}/unlock/", response_model=UserOut)
async def unlock_user(
    user_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """Unlock a user account (admin only). Cannot unlock yourself."""
    if user_id == admin.id:
        raise AppError(code="self_unlock", message="Cannot unlock your own account", status_code=400)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="User not found")

    user.status = "active"
    user.locked_until = None
    user.failed_login_count = 0
    await db.flush()
    await db.refresh(user)

    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=admin.id,
            details={"action": "user_unlocked", "target_user_id": user.id, "target_login": user.login},
            request=request,
        )
    except Exception:
        pass

    return UserOut.model_validate(user)


# ---------------------------------------------------------------------------
# API keys for a specific user (admin manages another user's keys)
# ---------------------------------------------------------------------------

_api_key_svc = ApiKeyService()


@router.get(
    "/admin/users/{user_id}/api-keys/",
    response_model=list[ApiKeyOut],
    summary="List API keys for a user (admin only)",
)
async def list_user_api_keys(
    user_id: int,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[ApiKeyOut]:
    """Return all API keys for the specified user."""
    target = await _get_user_or_404(db, user_id)
    keys = await _api_key_svc.list_keys(session=db, user_id=target.id)
    return [ApiKeyOut.model_validate(k) for k in keys]


@router.post(
    "/admin/users/{user_id}/api-keys/",
    response_model=ApiKeyCreated,
    status_code=201,
    summary="Create API key for a user (admin only)",
    responses={201: {"description": "Key created - raw_key shown once"}},
)
async def create_user_api_key(
    user_id: int,
    body: ApiKeyCreate,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ApiKeyCreated:
    """Create an API key for the specified user. The raw key is returned ONCE."""
    target = await _get_user_or_404(db, user_id)
    key, raw_key = await _api_key_svc.create_key(
        session=db,
        user_id=target.id,
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
        ip_allowlist=body.ip_allowlist,
    )

    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=admin.id,
            details={
                "action": "api_key_created",
                "target_user_id": target.id,
                "target_login": target.login,
                "key_name": body.name,
            },
            request=request,
        )
    except Exception:
        pass

    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        raw_key=raw_key,
        scopes=key.scopes,
        expires_at=key.expires_at,
        created_at=key.created_at,
    )


@router.delete(
    "/admin/users/{user_id}/api-keys/{key_id}/",
    status_code=204,
    summary="Revoke (delete) an API key for a user (admin only)",
)
async def delete_user_api_key(
    user_id: int,
    key_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Permanently revoke and delete an API key for the specified user."""
    target = await _get_user_or_404(db, user_id)
    await _api_key_svc.delete(session=db, user_id=target.id, key_id=key_id)

    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=admin.id,
            details={
                "action": "api_key_revoked",
                "target_user_id": target.id,
                "target_login": target.login,
                "key_id": key_id,
            },
            request=request,
        )
    except Exception:
        pass

    return Response(status_code=204)


async def _get_user_or_404(db: AsyncSession, user_id: int) -> User:
    """Load a user by ID or raise NotFoundError."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError(message="User not found")
    return user
