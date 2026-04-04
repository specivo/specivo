"""Admin credential broker API — external systems, credential issuance, audit."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.schemas.credential import (
    CredentialAuditLogOut,
    CredentialIssueRequest,
    CredentialIssueResponse,
    ExternalSystemCreate,
    ExternalSystemOut,
    IssuedCredentialOut,
)
from specivo.services.credential_broker_service import CredentialBrokerService

router = APIRouter(tags=["admin"])
_service = CredentialBrokerService()


# ---------------------------------------------------------------------------
# External Systems
# ---------------------------------------------------------------------------


@router.post(
    "/admin/external-systems/",
    response_model=ExternalSystemOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_system(
    data: ExternalSystemCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> ExternalSystemOut:
    """Register a new external system (admin only)."""
    system = await _service.register_system(db, system_type=data.system_type, name=data.name, config=data.config)
    return ExternalSystemOut.model_validate(system)


@router.get(
    "/admin/external-systems/",
    response_model=list[ExternalSystemOut],
)
async def list_systems(
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[ExternalSystemOut]:
    """List all external systems (admin only)."""
    systems = await _service.list_systems(db)
    return [ExternalSystemOut.model_validate(s) for s in systems]


@router.delete(
    "/admin/external-systems/{system_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_system(
    system_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an external system (admin only)."""
    await _service.delete_system(db, system_id=system_id)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@router.post(
    "/admin/credentials/issue/",
    response_model=CredentialIssueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def issue_credential(
    data: CredentialIssueRequest,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> CredentialIssueResponse:
    """Issue a temporary credential to an agent (admin only)."""
    cred, raw_token = await _service.issue_credential(
        db,
        system_id=data.system_id,
        agent_user_id=data.agent_user_id,
        scope=data.scope,
        ttl_minutes=data.ttl_minutes,
    )
    return CredentialIssueResponse(
        credential=IssuedCredentialOut.model_validate(cred),
        raw_token=raw_token,
    )


@router.post(
    "/admin/credentials/{credential_id}/revoke/",
    response_model=IssuedCredentialOut,
)
async def revoke_credential(
    credential_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> IssuedCredentialOut:
    """Revoke a credential (admin only)."""
    await _service.revoke_credential(db, credential_id=credential_id, actor_user_id=current_user.id)
    # Re-fetch to return updated state
    from specivo.models.credential import IssuedCredential

    cred = await db.get(IssuedCredential, credential_id)
    return IssuedCredentialOut.model_validate(cred)


@router.get(
    "/admin/credentials/",
    response_model=list[IssuedCredentialOut],
)
async def list_active_credentials(
    system_id: int | None = None,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[IssuedCredentialOut]:
    """List active (non-expired, non-revoked) credentials (admin only)."""
    creds = await _service.list_active_credentials(db, system_id=system_id)
    return [IssuedCredentialOut.model_validate(c) for c in creds]


@router.get(
    "/admin/credentials/audit-log/",
    response_model=list[CredentialAuditLogOut],
)
async def list_audit_logs(
    system_id: int | None = None,
    agent_user_id: int | None = None,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[CredentialAuditLogOut]:
    """List credential audit log entries (admin only)."""
    logs = await _service.list_audit_logs(db, system_id=system_id, agent_user_id=agent_user_id)
    return [CredentialAuditLogOut.model_validate(entry) for entry in logs]
