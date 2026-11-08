"""API key management endpoints.

A key limited to some projects cannot manage keys (``get_account_user``).

Endpoints:
    GET    /my/api-keys          - List API keys for the authenticated user
    POST   /my/api-keys          - Create a new API key (raw key shown once)
    PATCH  /my/api-keys/{id}     - Deactivate or reactivate a key
    DELETE /my/api-keys/{id}     - Hard-delete a key
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from specivo.core.database import DbSession
from specivo.core.security import get_account_user
from specivo.models.user import User
from specivo.schemas.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyPatch
from specivo.services.api_key_scope import project_keys_by_api_key
from specivo.services.api_key_service import ApiKeyService

router = APIRouter()
_service = ApiKeyService()


# ---------------------------------------------------------------------------
# GET /my/api-keys
# ---------------------------------------------------------------------------


@router.get(
    "/my/api-keys/",
    response_model=list[ApiKeyOut],
    summary="List API keys for the authenticated user",
)
async def list_api_keys(
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> list[ApiKeyOut]:
    """Return all API keys for the current user (no raw key or hash), with the projects each is limited to."""
    keys = await _service.list_keys(session=db, user_id=current_user.id)
    project_keys = await project_keys_by_api_key(db, keys)
    return [ApiKeyOut.model_validate(k).model_copy(update={"project_keys": project_keys[k.id]}) for k in keys]


# ---------------------------------------------------------------------------
# POST /my/api-keys
# ---------------------------------------------------------------------------


@router.post(
    "/my/api-keys/",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key",
    responses={201: {"description": "Key created — raw_key shown once"}},
)
async def create_api_key(
    body: ApiKeyCreate,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> ApiKeyCreated:
    """Create an API key. The raw_key in the response is shown ONCE and not stored.

    ``scopes`` optionally limits the key to some projects: ``{"projects": ["ACME", 12]}``,
    by key or id, each one the caller can see. It is stored as project ids and answered
    with ``project_keys``. An empty list or any other shape answers 422.
    """
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
        project_keys=(await project_keys_by_api_key(db, [key]))[key.id],
        expires_at=key.expires_at,
        created_at=key.created_at,
    )


# ---------------------------------------------------------------------------
# PATCH /my/api-keys/{key_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/my/api-keys/{key_id}/",
    response_model=ApiKeyOut,
    summary="Deactivate or reactivate an API key",
)
async def patch_api_key(
    key_id: int,
    body: ApiKeyPatch,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> ApiKeyOut:
    """Set is_active on an API key. Raises 404 if key not found or not owned by user."""
    key = await _service.update_key(
        session=db,
        user_id=current_user.id,
        key_id=key_id,
        is_active=body.is_active,
    )
    project_keys = await project_keys_by_api_key(db, [key])
    return ApiKeyOut.model_validate(key).model_copy(update={"project_keys": project_keys[key.id]})


# ---------------------------------------------------------------------------
# DELETE /my/api-keys/{key_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/my/api-keys/{key_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete an API key",
)
async def delete_api_key(
    key_id: int,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> Response:
    """Permanently delete an API key. Raises 404 if key not found or not owned by user."""
    await _service.delete(session=db, user_id=current_user.id, key_id=key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
