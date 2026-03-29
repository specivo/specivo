"""Pydantic schemas for credential broker API."""

from datetime import datetime

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# External Systems
# ---------------------------------------------------------------------------


class ExternalSystemCreate(BaseModel):
    system_type: str
    name: str
    config: dict


class ExternalSystemOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    system_type: str
    name: str
    config: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Issued Credentials
# ---------------------------------------------------------------------------


class CredentialIssueRequest(BaseModel):
    system_id: int
    agent_user_id: int
    scope: dict
    ttl_minutes: int = 60


class IssuedCredentialOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    system_id: int
    agent_user_id: int
    scope: dict
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CredentialIssueResponse(BaseModel):
    credential: IssuedCredentialOut
    raw_token: str


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------


class CredentialAuditLogOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    credential_id: int | None
    system_id: int
    agent_user_id: int
    action: str
    details: dict
    created_at: datetime
