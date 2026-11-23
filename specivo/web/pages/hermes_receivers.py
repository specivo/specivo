"""Web page for the Hermes event receivers of the signed-in account: ``/my/hermes-receivers/``.

The page is a shell for the ``hermesReceiverManager`` Alpine component, which
lists, creates, changes and deletes receivers through
``/api/v1/my/hermes-receivers/`` and shows a new receiver's URL, secret and
``hooks.outbound`` block once. Administrators get the same panel for another
account on its Admin > Users page, backed by
``/api/v1/admin/users/{id}/hermes-receivers/``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from specivo.core.app_url import base_path
from specivo.models.user import User
from specivo.web.deps import get_templates, require_user

router = APIRouter(tags=["web-hermes"], include_in_schema=False)


@router.get("/my/hermes-receivers/", response_model=None)
async def hermes_receivers_page(request: Request, user: Annotated[User, Depends(require_user)]) -> Response:
    """Render the Hermes receivers page of the signed-in account."""
    return get_templates().TemplateResponse(
        request,
        "pages/my/hermes_receivers.html",
        context={
            "user": user,
            "active_page": "hermes-receivers",
            "receivers_api": base_path() + "/api/v1/my/hermes-receivers/",
        },
    )
