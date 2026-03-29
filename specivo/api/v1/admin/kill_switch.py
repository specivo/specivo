"""Admin kill switch API — graduated agent access revocation."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import get_db
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.kill_switch import KillEventOut, KillRequest
from specivo.services.kill_switch_service import KillSwitchService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])
_service = KillSwitchService()


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: raise 403 if the current user is not an admin."""
    if not current_user.is_admin:
        raise PermissionDeniedError("Admin access required")
    return current_user


async def _resolve_kill_actor(request: Request, db: AsyncSession = Depends(get_db)) -> str:
    """Resolve who triggered the kill: admin user login or 'kill_token'.

    Accepts either:
    - X-Kill-Token header matching settings.kill_token
    - Authorization header with a valid admin JWT
    """
    settings = get_settings()

    # Check kill token first (allows unauthenticated emergency calls)
    import hmac

    x_kill_token = request.headers.get("X-Kill-Token")
    if x_kill_token and settings.kill_token and hmac.compare_digest(x_kill_token, settings.kill_token):
        return "kill_token"

    # Fall back to JWT admin auth
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        raise AppError(
            code="unauthorized",
            message="Admin access or valid X-Kill-Token required",
            status_code=401,
        )

    try:
        user = await get_current_user(request, db)
    except Exception:
        raise AppError(
            code="unauthorized",
            message="Admin access or valid X-Kill-Token required",
            status_code=401,
        )

    if not user.is_admin:
        raise PermissionDeniedError("Admin access required")

    return f"admin:{user.login}"


# ---------------------------------------------------------------------------
# Kill endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/admin/kill/agent/{user_id}",
    response_model=KillEventOut,
)
async def kill_agent(
    user_id: int,
    data: KillRequest,
    triggered_by: str = Depends(_resolve_kill_actor),
    db: AsyncSession = Depends(get_db),
) -> KillEventOut:
    """Kill a single agent — deactivate API keys, revoke credentials."""
    event = await _service.kill_agent(db, user_id, triggered_by, data.reason)
    await db.commit()
    return KillEventOut.model_validate(event)


@router.post(
    "/admin/kill/system/{system_id}",
    response_model=KillEventOut,
)
async def kill_system(
    system_id: int,
    data: KillRequest,
    triggered_by: str = Depends(_resolve_kill_actor),
    db: AsyncSession = Depends(get_db),
) -> KillEventOut:
    """Kill an external system — revoke all credentials for it."""
    event = await _service.kill_system(db, system_id, triggered_by, data.reason)
    await db.commit()
    return KillEventOut.model_validate(event)


@router.post(
    "/admin/kill/all",
    response_model=KillEventOut,
)
async def kill_all(
    data: KillRequest,
    triggered_by: str = Depends(_resolve_kill_actor),
    db: AsyncSession = Depends(get_db),
) -> KillEventOut:
    """Kill all agents — deactivate ALL service account API keys."""
    event = await _service.kill_all(db, triggered_by, data.reason)
    await db.commit()
    return KillEventOut.model_validate(event)


# ---------------------------------------------------------------------------
# Unkill endpoint (admin only)
# ---------------------------------------------------------------------------


@router.post(
    "/admin/unkill/agent/{user_id}",
    status_code=status.HTTP_200_OK,
)
async def unkill_agent(
    user_id: int,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reactivate a killed agent — re-enable API keys (admin only)."""
    count = await _service.unkill_agent(db, user_id)
    await db.commit()
    return {"reactivated_keys": count, "user_id": user_id}


# ---------------------------------------------------------------------------
# List events
# ---------------------------------------------------------------------------


@router.get(
    "/admin/kill-events",
    response_model=list[KillEventOut],
)
async def list_kill_events(
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[KillEventOut]:
    """List all kill events, newest first (admin only)."""
    events = await _service.list_kill_events(db)
    return [KillEventOut.model_validate(e) for e in events]
