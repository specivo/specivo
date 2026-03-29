"""GroupPolicyService — manage agent groups, memberships, and access policies."""

from __future__ import annotations

import ipaddress
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.models.agent_group import AgentGroup, AgentGroupMembership, GroupPolicy

logger = logging.getLogger(__name__)


class GroupPolicyService:
    """Stateless service for agent group policy management."""

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    async def create_group(
        self,
        session: AsyncSession,
        name: str,
        description: str | None,
    ) -> AgentGroup:
        """Create a new agent group."""
        # Check for duplicate name
        existing = await session.execute(select(AgentGroup).where(AgentGroup.name == name))
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(f"Agent group '{name}' already exists")

        group = AgentGroup(name=name, description=description)
        session.add(group)
        await session.flush()
        logger.info("Created agent group %d: %s", group.id, name)
        return group

    async def list_groups(self, session: AsyncSession) -> list[AgentGroup]:
        """List all agent groups ordered by name."""
        stmt = select(AgentGroup).order_by(AgentGroup.name)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def delete_group(self, session: AsyncSession, group_id: int) -> None:
        """Delete an agent group by ID."""
        result = await session.execute(delete(AgentGroup).where(AgentGroup.id == group_id))
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"Agent group {group_id} not found")
        logger.info("Deleted agent group %d", group_id)

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    async def add_member(
        self,
        session: AsyncSession,
        group_id: int,
        user_id: int,
    ) -> AgentGroupMembership:
        """Add a user to an agent group."""
        # Verify group exists
        group = await session.get(AgentGroup, group_id)
        if group is None:
            raise NotFoundError(f"Agent group {group_id} not found")

        # Check for duplicate membership
        existing = await session.execute(
            select(AgentGroupMembership).where(
                AgentGroupMembership.group_id == group_id,
                AgentGroupMembership.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("User is already a member of this group")

        membership = AgentGroupMembership(group_id=group_id, user_id=user_id)
        session.add(membership)
        await session.flush()
        logger.info("Added user %d to agent group %d", user_id, group_id)
        return membership

    async def remove_member(
        self,
        session: AsyncSession,
        group_id: int,
        user_id: int,
    ) -> None:
        """Remove a user from an agent group."""
        result = await session.execute(
            delete(AgentGroupMembership).where(
                AgentGroupMembership.group_id == group_id,
                AgentGroupMembership.user_id == user_id,
            )
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError("Membership not found")
        logger.info("Removed user %d from agent group %d", user_id, group_id)

    async def list_members(self, session: AsyncSession, group_id: int) -> list[AgentGroupMembership]:
        """List all memberships for a group."""
        stmt = select(AgentGroupMembership).where(AgentGroupMembership.group_id == group_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Policies
    # ------------------------------------------------------------------

    async def create_policy(
        self,
        session: AsyncSession,
        group_id: int,
        project_id: int | None,
        scopes: list[str],
        ip_allowlist: list[str] | None,
    ) -> GroupPolicy:
        """Create a new access policy for an agent group."""
        # Verify group exists
        group = await session.get(AgentGroup, group_id)
        if group is None:
            raise NotFoundError(f"Agent group {group_id} not found")

        policy = GroupPolicy(
            group_id=group_id,
            project_id=project_id,
            scopes=scopes,
            ip_allowlist=ip_allowlist,
        )
        session.add(policy)
        await session.flush()
        logger.info("Created policy %d for group %d", policy.id, group_id)
        return policy

    async def list_policies(self, session: AsyncSession, group_id: int) -> list[GroupPolicy]:
        """List all policies for a group."""
        stmt = select(GroupPolicy).where(GroupPolicy.group_id == group_id).order_by(GroupPolicy.id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def delete_policy(self, session: AsyncSession, group_id: int, policy_id: int) -> None:
        """Delete a policy by ID within a group."""
        result = await session.execute(
            delete(GroupPolicy).where(
                GroupPolicy.id == policy_id,
                GroupPolicy.group_id == group_id,
            )
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"Policy {policy_id} not found in group {group_id}")
        logger.info("Deleted policy %d from group %d", policy_id, group_id)

    # ------------------------------------------------------------------
    # Access evaluation
    # ------------------------------------------------------------------

    async def evaluate_access(
        self,
        session: AsyncSession,
        user_id: int,
        project_id: int | None,
        scope: str,
        client_ip: str | None,
    ) -> bool:
        """Evaluate whether a user has access based on group policies.

        Resolution:
        1. Find all groups the user belongs to.
        2. Find all policies for those groups.
        3. For each policy, check project match + scope match + IP match.
        4. Union across groups: any group granting access = allowed.
        """
        # Step 1: find user's groups
        membership_stmt = select(AgentGroupMembership.group_id).where(AgentGroupMembership.user_id == user_id)
        memberships = await session.execute(membership_stmt)
        group_ids = [row[0] for row in memberships.fetchall()]

        if not group_ids:
            return False

        # Step 2: find policies for those groups
        policy_stmt = select(GroupPolicy).where(GroupPolicy.group_id.in_(group_ids))
        policies_result = await session.execute(policy_stmt)
        policies = policies_result.scalars().all()

        # Step 3+4: check each policy (union — any match = allowed)
        for policy in policies:
            if not self._project_matches(policy, project_id):
                continue
            if scope not in policy.scopes:
                continue
            if not self._ip_matches(policy, client_ip):
                continue
            return True

        return False

    @staticmethod
    def _project_matches(policy: GroupPolicy, project_id: int | None) -> bool:
        """Check if a policy's project scope matches the requested project.

        A policy with ``project_id=None`` (global) matches any project.
        """
        if policy.project_id is None:
            return True
        return policy.project_id == project_id

    @staticmethod
    def _ip_matches(policy: GroupPolicy, client_ip: str | None) -> bool:
        """Check if the client IP is allowed by the policy.

        If the policy has no IP allowlist, any IP is accepted.
        If client_ip is None and an allowlist exists, deny.
        """
        if not policy.ip_allowlist:
            return True
        if client_ip is None:
            return False
        try:
            addr = ipaddress.ip_address(client_ip)
            return any(addr in ipaddress.ip_network(cidr, strict=False) for cidr in policy.ip_allowlist)
        except ValueError:
            return False
