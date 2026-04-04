"""Admin agent groups API — CRUD for groups, memberships, and policies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.schemas.agent_group import (
    AgentGroupCreate,
    AgentGroupOut,
    MemberAdd,
    MembershipOut,
    PolicyCreate,
    PolicyOut,
)
from specivo.services.group_policy_service import GroupPolicyService

router = APIRouter(tags=["admin"])
_service = GroupPolicyService()


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@router.post(
    "/admin/agent-groups/",
    response_model=AgentGroupOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_group(
    data: AgentGroupCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> AgentGroupOut:
    """Create a new agent group (admin only)."""
    group = await _service.create_group(db, name=data.name, description=data.description)
    return AgentGroupOut.model_validate(group)


@router.get(
    "/admin/agent-groups/",
    response_model=list[AgentGroupOut],
)
async def list_groups(
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[AgentGroupOut]:
    """List all agent groups (admin only)."""
    groups = await _service.list_groups(db)
    return [AgentGroupOut.model_validate(g) for g in groups]


@router.delete(
    "/admin/agent-groups/{group_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_group(
    group_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an agent group (admin only)."""
    await _service.delete_group(db, group_id=group_id)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.post(
    "/admin/agent-groups/{group_id}/members/",
    response_model=MembershipOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    group_id: int,
    data: MemberAdd,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MembershipOut:
    """Add a user to an agent group (admin only)."""
    membership = await _service.add_member(db, group_id=group_id, user_id=data.user_id)
    return MembershipOut.model_validate(membership)


@router.delete(
    "/admin/agent-groups/{group_id}/members/{user_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a user from an agent group (admin only)."""
    await _service.remove_member(db, group_id=group_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@router.post(
    "/admin/agent-groups/{group_id}/policies/",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_policy(
    group_id: int,
    data: PolicyCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> PolicyOut:
    """Add a policy to an agent group (admin only)."""
    policy = await _service.create_policy(
        db,
        group_id=group_id,
        project_id=data.project_id,
        scopes=data.scopes,
        ip_allowlist=data.ip_allowlist,
    )
    return PolicyOut.model_validate(policy)


@router.get(
    "/admin/agent-groups/{group_id}/policies/",
    response_model=list[PolicyOut],
)
async def list_policies(
    group_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[PolicyOut]:
    """List all policies for an agent group (admin only)."""
    policies = await _service.list_policies(db, group_id=group_id)
    return [PolicyOut.model_validate(p) for p in policies]


@router.delete(
    "/admin/agent-groups/{group_id}/policies/{policy_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_policy(
    group_id: int,
    policy_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a policy from an agent group (admin only)."""
    await _service.delete_policy(db, group_id=group_id, policy_id=policy_id)
