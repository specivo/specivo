"""Hermes event receivers of the caller's own account.

Endpoints:
    GET    /my/hermes-receivers/       - List the account's receivers (no token, no secret)
    POST   /my/hermes-receivers/       - Create one; the token, secret and Hermes block are answered once
    PATCH  /my/hermes-receivers/{id}/  - Rename, switch on or off, or rotate the secret (answered once)
    DELETE /my/hermes-receivers/{id}/  - Delete one

A key limited to some projects cannot manage receivers (``get_account_user``).
Administrators manage another account's receivers under
``/admin/users/{user_id}/hermes-receivers/`` (``specivo.api.v1.admin.users``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from specivo.core.database import DbSession
from specivo.core.security import get_account_user
from specivo.models.hermes_receiver import HermesReceiver
from specivo.models.user import User
from specivo.schemas.hermes import (
    HermesReceiverCreate,
    HermesReceiverCreated,
    HermesReceiverOut,
    HermesReceiverPatched,
    HermesReceiverUpdate,
)
from specivo.services.hermes_receiver_service import (
    SECRET_ENV,
    HermesReceiverService,
    MintedReceiver,
    event_url,
    hooks_config,
)

router = APIRouter(tags=["hermes"])
_service = HermesReceiverService()


def created_out(minted: MintedReceiver) -> HermesReceiverCreated:
    """The answer to a create: the receiver plus the credentials shown only now."""
    return HermesReceiverCreated(
        **HermesReceiverOut.model_validate(minted.receiver).model_dump(),
        token=minted.token,
        secret=minted.secret,
        url=event_url(minted.token),
        secret_env=SECRET_ENV,
        hooks_config=hooks_config(minted.token),
    )


def patched_out(receiver: HermesReceiver, secret: str | None) -> HermesReceiverPatched:
    """The answer to a change; the new secret only when it was rotated."""
    return HermesReceiverPatched(
        **HermesReceiverOut.model_validate(receiver).model_dump(),
        secret=secret,
        secret_env=SECRET_ENV if secret else None,
    )


@router.get("/my/hermes-receivers/", response_model=list[HermesReceiverOut], summary="List my Hermes receivers")
async def list_my_receivers(
    db: DbSession, current_user: Annotated[User, Depends(get_account_user)]
) -> list[HermesReceiverOut]:
    """The caller's receivers, oldest first. Neither token nor secret is ever answered again."""
    return [HermesReceiverOut.model_validate(r) for r in await _service.list_for_user(db, current_user.id)]


@router.post(
    "/my/hermes-receivers/",
    response_model=HermesReceiverCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Hermes receiver",
    responses={201: {"description": "Created: token, secret and hooks_config are shown once"}},
)
async def create_my_receiver(
    body: HermesReceiverCreate,
    request: Request,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> HermesReceiverCreated:
    """Create a receiver for the caller's account and answer its URL token, secret and ``hooks.outbound`` block."""
    minted = await _service.create(db, owner=current_user, name=body.name, actor=current_user, request=request)
    return created_out(minted)


@router.patch("/my/hermes-receivers/{receiver_id}/", response_model=HermesReceiverPatched, summary="Change a receiver")
async def update_my_receiver(
    receiver_id: int,
    body: HermesReceiverUpdate,
    request: Request,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> HermesReceiverPatched:
    """Rename, switch on or off, or rotate the secret; the old secret stops verifying at once."""
    receiver = await _service.get_for_user(db, current_user.id, receiver_id)
    secret = await _service.update(
        db,
        receiver,
        owner=current_user,
        actor=current_user,
        name=body.name,
        is_active=body.is_active,
        rotate_secret=body.rotate_secret,
        request=request,
    )
    return patched_out(receiver, secret)


@router.delete(
    "/my/hermes-receivers/{receiver_id}/", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a receiver"
)
async def delete_my_receiver(
    receiver_id: int,
    request: Request,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> Response:
    """Delete a receiver. Its agent sessions and audit rows stay; further events answer 404."""
    receiver = await _service.get_for_user(db, current_user.id, receiver_id)
    await _service.delete(db, receiver, owner=current_user, actor=current_user, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
