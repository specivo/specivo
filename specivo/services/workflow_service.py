"""Workflow engine — transition tables, Redis cache, validation on status change."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, ValidationError
from specivo.core.features import get_feature_registry
from specivo.core.i18n import gettext as _
from specivo.core.redis import get_redis
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.user import User
from specivo.models.workflow import WorkflowFieldRule, WorkflowTransition
from specivo.schemas.workflow import FieldRuleCreate, TransitionCreate

logger = logging.getLogger(__name__)


class WorkflowService:
    """Manages workflow transitions and field rules with Redis caching."""

    CACHE_PREFIX = "wf:trans"

    @property
    def cache_ttl(self) -> int:
        from specivo.core.config import get_settings

        return get_settings().workflow_cache_ttl

    # ------------------------------------------------------------------
    # Cache key helpers
    # ------------------------------------------------------------------

    def _cache_key(self, tracker_id: int, role_id: int, old_status_id: int) -> str:
        return f"{self.CACHE_PREFIX}:{tracker_id}:{role_id}:{old_status_id}"

    # ------------------------------------------------------------------
    # Core query methods
    # ------------------------------------------------------------------

    async def _get_allowed_for_role(
        self,
        session: AsyncSession,
        tracker_id: int,
        role_id: int,
        old_status_id: int,
    ) -> list[int]:
        """Get allowed new status IDs for a single role. Uses Redis cache."""
        cache_key = self._cache_key(tracker_id, role_id, old_status_id)

        try:
            redis = await get_redis()
            cached = await redis.get(cache_key)
            if cached is not None:
                return list(json.loads(cached))
        except Exception:
            pass  # Redis unavailable — fall through to DB

        stmt = select(WorkflowTransition.new_status_id).where(
            WorkflowTransition.tracker_id == tracker_id,
            WorkflowTransition.role_id == role_id,
            WorkflowTransition.old_status_id == old_status_id,
        )
        result = await session.execute(stmt)
        allowed = [row[0] for row in result.all()]

        # Cache result
        try:
            redis = await get_redis()
            await redis.set(cache_key, json.dumps(allowed), ex=self.cache_ttl)
        except Exception:
            pass

        return allowed

    async def get_allowed_statuses(
        self,
        session: AsyncSession,
        tracker_id: int,
        role_ids: list[int],
        current_status_id: int,
    ) -> list[int]:
        """Union of allowed new statuses across all roles."""
        all_allowed: set[int] = set()
        for role_id in role_ids:
            allowed = await self._get_allowed_for_role(session, tracker_id, role_id, current_status_id)
            all_allowed.update(allowed)
        return sorted(all_allowed)

    async def _get_field_rules_for_role(
        self,
        session: AsyncSession,
        tracker_id: int,
        role_id: int,
        status_id: int,
    ) -> dict[str, str]:
        """Get field rules for a single role: {field_name: rule}."""
        stmt = select(WorkflowFieldRule).where(
            WorkflowFieldRule.tracker_id == tracker_id,
            WorkflowFieldRule.role_id == role_id,
            WorkflowFieldRule.status_id == status_id,
        )
        result = await session.execute(stmt)
        rules = result.scalars().all()
        return {r.field_name: r.rule for r in rules}

    async def get_field_rules(
        self,
        session: AsyncSession,
        tracker_id: int,
        role_ids: list[int],
        status_id: int,
    ) -> dict[str, str]:
        """Aggregate field rules across roles.

        - 'required': required if ANY role requires it.
        - 'readonly': readonly only if ALL roles mark it readonly.
        - 'required' overrides 'readonly' if conflict.
        """
        all_rules: list[dict[str, str]] = []
        for role_id in role_ids:
            rules = await self._get_field_rules_for_role(session, tracker_id, role_id, status_id)
            all_rules.append(rules)

        if not all_rules:
            return {}

        # Collect all field names mentioned
        all_fields: set[str] = set()
        for rules in all_rules:
            all_fields.update(rules.keys())

        merged: dict[str, str] = {}
        for field in all_fields:
            # Check if any role marks required
            is_required = any(r.get(field) == "required" for r in all_rules)
            if is_required:
                merged[field] = "required"
                continue

            # Check if ALL roles mark readonly
            is_readonly = all(r.get(field) == "readonly" for r in all_rules)
            if is_readonly:
                merged[field] = "readonly"

        return merged

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def _has_any_transitions(self, session: AsyncSession) -> bool:
        """Check if the workflow_transitions table has any rows."""
        stmt = select(func.count()).select_from(WorkflowTransition)
        result = await session.execute(stmt)
        return (result.scalar() or 0) > 0

    async def _get_user_role_ids(self, session: AsyncSession, user: User, project_id: int) -> list[int]:
        """Get role IDs for a user in a project."""
        stmt = (
            select(MemberRole.role_id)
            .join(Member, Member.id == MemberRole.member_id)
            .where(Member.user_id == user.id, Member.project_id == project_id)
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]

    async def validate_transition(
        self,
        session: AsyncSession,
        issue: Issue,
        new_status_id: int,
        user: User,
    ) -> None:
        """Raise ValidationError if transition is not allowed.

        Admin users bypass. If no workflow rules exist at all (empty table),
        allow any transition (backward compat).
        """
        if user.is_admin:
            return

        if not await self._has_any_transitions(session):
            return

        role_ids = await self._get_user_role_ids(session, user, issue.project_id)
        if not role_ids:
            # No roles in project — cannot determine allowed transitions
            raise ValidationError(
                message=_("No project roles found for user"),
                code="workflow_transition_denied",
                details={"allowed_status_ids": []},
            )

        allowed = await self.get_allowed_statuses(session, issue.tracker_id, role_ids, issue.status_id)

        if new_status_id not in allowed:
            raise AppError(
                code="workflow_transition_denied",
                message=_("Transition from status {old} to {new} is not allowed").format(
                    old=issue.status_id, new=new_status_id
                ),
                status_code=422,
                details={"allowed_status_ids": allowed},
            )

    async def validate_field_rules(
        self,
        session: AsyncSession,
        issue: Issue,
        update_data: Any,
        user: User,
        target_status_id: int,
    ) -> None:
        """Raise ValidationError if required fields missing or readonly fields changed.

        Admin users bypass.
        Without the ``workflow_field_rules`` feature, this is a no-op.
        """
        registry = get_feature_registry()
        if not registry.has_feature("workflow_field_rules"):
            return

        if user.is_admin:
            return

        role_ids = await self._get_user_role_ids(session, user, issue.project_id)
        if not role_ids:
            return

        rules = await self.get_field_rules(session, issue.tracker_id, role_ids, target_status_id)

        if not rules:
            return

        for field_name, rule in rules.items():
            if rule == "required":
                # Field must have a value after update
                new_val = getattr(update_data, field_name, None)
                current_val = getattr(issue, field_name, None)
                effective_val = new_val if new_val is not None else current_val
                if effective_val is None:
                    raise AppError(
                        code="workflow_field_required",
                        message=_("Field '{field}' is required for this status").format(field=field_name),
                        status_code=422,
                        field=field_name,
                    )
            elif rule == "readonly":
                # Field must not be changed
                new_val = getattr(update_data, field_name, None)
                if new_val is not None:
                    raise AppError(
                        code="workflow_field_readonly",
                        message=_("Field '{field}' is read-only for this status").format(field=field_name),
                        status_code=422,
                        field=field_name,
                    )

    # ------------------------------------------------------------------
    # Cache invalidation
    # ------------------------------------------------------------------

    async def invalidate_cache(self, tracker_id: int | None = None) -> None:
        """Clear cached transitions. Called on admin CRUD."""
        try:
            redis = await get_redis()
            if tracker_id is not None:
                pattern = f"{self.CACHE_PREFIX}:{tracker_id}:*"
            else:
                pattern = f"{self.CACHE_PREFIX}:*"
            keys = []
            async for key in redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                await redis.delete(*keys)
        except Exception:
            pass  # Redis unavailable — cache will expire naturally

    # ------------------------------------------------------------------
    # Admin CRUD — Transitions
    # ------------------------------------------------------------------

    async def list_transitions(
        self,
        session: AsyncSession,
        tracker_id: int | None = None,
        role_id: int | None = None,
    ) -> list[WorkflowTransition]:
        stmt = select(WorkflowTransition)
        if tracker_id is not None:
            stmt = stmt.where(WorkflowTransition.tracker_id == tracker_id)
        if role_id is not None:
            stmt = stmt.where(WorkflowTransition.role_id == role_id)
        stmt = stmt.order_by(WorkflowTransition.id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def create_transition(self, session: AsyncSession, data: TransitionCreate) -> WorkflowTransition:
        transition = WorkflowTransition(
            tracker_id=data.tracker_id,
            role_id=data.role_id,
            old_status_id=data.old_status_id,
            new_status_id=data.new_status_id,
        )
        session.add(transition)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise AppError(
                code="conflict",
                message=_("Duplicate workflow transition"),
                status_code=409,
            )
        await self.invalidate_cache(data.tracker_id)
        return transition

    async def delete_transition(self, session: AsyncSession, transition_id: int) -> None:
        stmt = select(WorkflowTransition).where(WorkflowTransition.id == transition_id)
        result = await session.execute(stmt)
        transition = result.scalar_one_or_none()
        if transition is None:
            from specivo.core.exceptions import NotFoundError

            raise NotFoundError(_("Workflow transition not found"))
        tracker_id = transition.tracker_id
        await session.delete(transition)
        await session.flush()
        await self.invalidate_cache(tracker_id)

    async def bulk_replace_transitions(
        self,
        session: AsyncSession,
        tracker_id: int,
        role_id: int,
        transitions: list[dict],
    ) -> list[WorkflowTransition]:
        """Delete all transitions for tracker+role, then create new ones."""
        await session.execute(
            delete(WorkflowTransition).where(
                WorkflowTransition.tracker_id == tracker_id,
                WorkflowTransition.role_id == role_id,
            )
        )
        new_transitions = []
        for t in transitions:
            wt = WorkflowTransition(
                tracker_id=tracker_id,
                role_id=role_id,
                old_status_id=t["old_status_id"],
                new_status_id=t["new_status_id"],
            )
            session.add(wt)
            new_transitions.append(wt)
        await session.flush()
        await self.invalidate_cache(tracker_id)
        return new_transitions

    # ------------------------------------------------------------------
    # Admin CRUD — Field Rules
    # ------------------------------------------------------------------

    async def list_field_rules(
        self,
        session: AsyncSession,
        tracker_id: int | None = None,
        role_id: int | None = None,
    ) -> list[WorkflowFieldRule]:
        stmt = select(WorkflowFieldRule)
        if tracker_id is not None:
            stmt = stmt.where(WorkflowFieldRule.tracker_id == tracker_id)
        if role_id is not None:
            stmt = stmt.where(WorkflowFieldRule.role_id == role_id)
        stmt = stmt.order_by(WorkflowFieldRule.id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def create_field_rule(self, session: AsyncSession, data: FieldRuleCreate) -> WorkflowFieldRule:
        rule = WorkflowFieldRule(
            tracker_id=data.tracker_id,
            role_id=data.role_id,
            status_id=data.status_id,
            field_name=data.field_name,
            rule=data.rule,
        )
        session.add(rule)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise AppError(
                code="conflict",
                message=_("Duplicate workflow field rule"),
                status_code=409,
            )
        return rule

    async def delete_field_rule(self, session: AsyncSession, rule_id: int) -> None:
        stmt = select(WorkflowFieldRule).where(WorkflowFieldRule.id == rule_id)
        result = await session.execute(stmt)
        rule = result.scalar_one_or_none()
        if rule is None:
            from specivo.core.exceptions import NotFoundError

            raise NotFoundError(_("Workflow field rule not found"))
        await session.delete(rule)
        await session.flush()
