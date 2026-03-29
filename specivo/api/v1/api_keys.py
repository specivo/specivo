"""API key management endpoints.

Endpoints:
    GET    /my/api-keys          - List API keys for the authenticated user
    POST   /my/api-keys          - Create a new API key (raw key shown once)
    PATCH  /my/api-keys/{id}     - Deactivate or reactivate a key
    DELETE /my/api-keys/{id}     - Hard-delete a key
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyPatch
from specivo.services.api_key_service import ApiKeyService

router = APIRouter()
_service = ApiKeyService()


# ---------------------------------------------------------------------------
# GET /my/api-keys
# ---------------------------------------------------------------------------


@router.get(
    "/my/api-keys",
    response_model=list[ApiKeyOut],
    summary="List API keys for the authenticated user",
)
async def list_api_keys(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ApiKeyOut]:
    """Return all API keys for the current user (no raw key or hash)."""
    keys = await _service.list_keys(session=db, user_id=current_user.id)
    return [ApiKeyOut.model_validate(k) for k in keys]


# ---------------------------------------------------------------------------
# POST /my/api-keys
# ---------------------------------------------------------------------------


@router.post(
    "/my/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
    responses={201: {"description": "Key created — raw_key shown once"}},
)
async def create_api_key(
    body: ApiKeyCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ApiKeyCreated:
    """Create an API key. The raw_key in the response is shown ONCE and not stored."""
    key, raw_key = await _service.create_key(
        session=db,
        user_id=current_user.id,
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
        ip_allowlist=body.ip_allowlist,
    )
    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        raw_key=raw_key,
        scopes=key.scopes,
        expires_at=key.expires_at,
        created_at=key.created_at,
    )


# ---------------------------------------------------------------------------
# PATCH /my/api-keys/{key_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/my/api-keys/{key_id}",
    response_model=ApiKeyOut,
    summary="Deactivate or reactivate an API key",
)
async def patch_api_key(
    key_id: int,
    body: ApiKeyPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ApiKeyOut:
    """Set is_active on an API key. Raises 404 if key not found or not owned by user."""
    key = await _service.update_key(
        session=db,
        user_id=current_user.id,
        key_id=key_id,
        is_active=body.is_active,
    )
    return ApiKeyOut.model_validate(key)


# ---------------------------------------------------------------------------
# DELETE /my/api-keys/{key_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/my/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete an API key",
)
async def delete_api_key(
    key_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Permanently delete an API key. Raises 404 if key not found or not owned by user."""
    await _service.delete(session=db, user_id=current_user.id, key_id=key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
