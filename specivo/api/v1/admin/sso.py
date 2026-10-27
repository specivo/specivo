"""Admin SSO API: OpenID Connect providers, their group mappings, and authentication settings.

Every route is admin-only. The client secret is write-only: it is accepted on
create and update and never returned (``client_secret_configured`` and
``client_secret_status`` say whether one is set and readable). The rules
behind each route are in :mod:`specivo.services.sso.provider_service`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.constants import MAX_PAGE_LIMIT
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.sso import (
    AuthSettingsOut,
    AuthSettingsUpdate,
    SsoGroupMappingIn,
    SsoGroupMappingOut,
    SsoProviderCreate,
    SsoProviderOut,
    SsoProviderUpdate,
    SsoSelfTestResult,
)
from specivo.services.sso.auth_settings import get_auth_settings, update_auth_settings
from specivo.services.sso.provider_service import ProviderService

router = APIRouter(tags=["admin-sso"])
_service = ProviderService()

UNLINKED_IDENTITIES_HEADER = "X-Unlinked-Identities"
REVOKED_SESSIONS_HEADER = "X-Revoked-Sessions"

AdminUser = Annotated[User, Depends(require_admin_api)]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@router.get("/admin/sso/providers/", response_model=PaginatedResponse[SsoProviderOut])
async def list_providers(
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
    offset: int = Query(0, ge=0),
    limit: int = Query(MAX_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> PaginatedResponse[SsoProviderOut]:
    """List providers in login-button order (``position``, then name)."""
    providers, total = await _service.list_providers(db, offset=offset, limit=limit)
    counts = await _service.identity_counts(db, [p.id for p in providers])
    return PaginatedResponse[SsoProviderOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[_service.to_out(p, identity_count=counts.get(p.id, 0)) for p in providers],
    )


@router.post("/admin/sso/providers/", response_model=SsoProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    data: SsoProviderCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SsoProviderOut:
    """Create a provider (disabled unless ``enabled`` is sent).

    Send the secret as ``client_secret``, or the name of an environment
    variable holding it as ``client_secret_env``; not both, and neither for
    a public client (``token_endpoint_auth_method: none``). The response has
    the ``redirect_uri`` and ``backchannel_logout_uri`` to register at the IdP.
    """
    provider = await _service.create(db, data, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return await _service.out(db, provider)


@router.get("/admin/sso/providers/{provider_id}/", response_model=SsoProviderOut)
async def get_provider(provider_id: int, admin: AdminUser, db: AsyncSession = Depends(get_db)) -> SsoProviderOut:
    """Return one provider, without its secret."""
    return await _service.out(db, await _service.get(db, provider_id))


@router.patch("/admin/sso/providers/{provider_id}/", response_model=SsoProviderOut)
async def update_provider(
    provider_id: int,
    data: SsoProviderUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SsoProviderOut:
    """Update the fields present in the body.

    Omitting ``client_secret`` keeps the stored secret. ``clear_client_secret:
    true`` removes the secret or env reference. Any change drops the cached
    discovery document and JWKS.
    """
    provider = await _service.update(db, provider_id, data, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return await _service.out(db, provider)


@router.delete(
    "/admin/sso/providers/{provider_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Provider deleted.",
            "headers": {
                UNLINKED_IDENTITIES_HEADER: {
                    "description": "User identities unlinked (only with force).",
                    "schema": {"type": "integer"},
                },
                REVOKED_SESSIONS_HEADER: {
                    "description": "Sessions ended for the unlinked users.",
                    "schema": {"type": "integer"},
                },
            },
        },
        status.HTTP_409_CONFLICT: {"description": "Users are linked to the provider; disable it, or use force."},
    },
)
async def delete_provider(
    provider_id: int,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
    force: bool = Query(False, description="Unlink the provider's identities and end their users' sessions."),
) -> Response:
    """Delete a provider. Refused with 409 while users are linked to it, unless ``force=true``."""
    result = await _service.delete(db, provider_id, force=force, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            UNLINKED_IDENTITIES_HEADER: str(result.identities_unlinked),
            REVOKED_SESSIONS_HEADER: str(result.sessions_revoked),
        },
    )


@router.post("/admin/sso/providers/{provider_id}/test/", response_model=SsoSelfTestResult)
async def test_provider(provider_id: int, admin: AdminUser, db: AsyncSession = Depends(get_db)) -> SsoSelfTestResult:
    """Fetch the provider's discovery document and JWKS now and report what was found.

    Always 200 for an existing provider: ``ok`` says whether it worked,
    ``failed_step`` / ``error_code`` / ``error_reason`` say why not. Nothing
    is stored or cached, whatever the result.
    """
    provider = await _service.get(db, provider_id)
    return await _service.self_test(provider)


# ---------------------------------------------------------------------------
# Group mappings
# ---------------------------------------------------------------------------


@router.get("/admin/sso/providers/{provider_id}/group-mappings/", response_model=list[SsoGroupMappingOut])
async def list_group_mappings(
    provider_id: int, admin: AdminUser, db: AsyncSession = Depends(get_db)
) -> list[SsoGroupMappingOut]:
    """List the provider's IdP group -> user group mappings."""
    return await _service.list_group_mappings(db, provider_id)


@router.put("/admin/sso/providers/{provider_id}/group-mappings/", response_model=list[SsoGroupMappingOut])
async def replace_group_mappings(
    provider_id: int,
    items: list[SsoGroupMappingIn],
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[SsoGroupMappingOut]:
    """Replace all mappings of the provider with ``items``. IdP group values match exactly (case-sensitive)."""
    result = await _service.replace_group_mappings(db, provider_id, items, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return result


@router.post(
    "/admin/sso/providers/{provider_id}/group-mappings/",
    response_model=SsoGroupMappingOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_group_mapping(
    provider_id: int,
    item: SsoGroupMappingIn,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SsoGroupMappingOut:
    """Add one mapping. An identical mapping returns 409."""
    result = await _service.add_group_mapping(db, provider_id, item, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return result


@router.delete(
    "/admin/sso/providers/{provider_id}/group-mappings/{mapping_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_group_mapping(
    provider_id: int,
    mapping_id: int,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove one mapping. Memberships it created are re-evaluated on the users' next sign-in."""
    await _service.delete_group_mapping(db, provider_id, mapping_id, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Authentication settings
# ---------------------------------------------------------------------------


@router.get("/admin/settings/authentication/", response_model=AuthSettingsOut)
async def get_authentication_settings(admin: AdminUser, db: AsyncSession = Depends(get_db)) -> AuthSettingsOut:
    """Return the instance-wide sign-in settings."""
    return await get_auth_settings(db)


@router.patch("/admin/settings/authentication/", response_model=AuthSettingsOut)
async def update_authentication_settings(
    data: AuthSettingsUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> AuthSettingsOut:
    """Update the fields present in the body. ``auto_redirect_provider`` must name an existing provider."""
    result = await update_auth_settings(db, data, actor=admin)
    await db.commit()  # commit before response to avoid reload race condition
    return result
