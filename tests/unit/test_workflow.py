"""Unit tests for workflow engine logic.

Tests the WorkflowService cache key format and multi-role aggregation logic
without requiring a database or Redis.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from specivo.services.workflow_service import WorkflowService


@pytest.fixture
def service() -> WorkflowService:
    return WorkflowService()


class TestCacheKeyFormat:
    """Verify cache key string construction."""

    def test_cache_key_format(self, service: WorkflowService) -> None:
        key = service._cache_key(tracker_id=1, role_id=2, old_status_id=3)
        assert key == "wf:trans:1:2:3"

    def test_cache_key_different_ids(self, service: WorkflowService) -> None:
        key = service._cache_key(tracker_id=10, role_id=20, old_status_id=30)
        assert key == "wf:trans:10:20:30"


class TestUnionOfStatusesAcrossRoles:
    """Given role A allows [2,3] and role B allows [3,4], union is [2,3,4]."""

    @pytest.mark.asyncio
    async def test_union_of_statuses_across_roles(self, service: WorkflowService) -> None:
        """Multi-role union: allowed statuses are the union across all roles."""
        session = AsyncMock()

        # Mock _get_allowed_for_role to return different sets per role
        async def mock_get_allowed(sess, tracker_id, role_id, old_status_id):
            if role_id == 1:
                return [2, 3]
            elif role_id == 2:
                return [3, 4]
            return []

        with patch.object(service, "_get_allowed_for_role", side_effect=mock_get_allowed):
            result = await service.get_allowed_statuses(session, tracker_id=1, role_ids=[1, 2], current_status_id=1)

        assert sorted(result) == [2, 3, 4]

    @pytest.mark.asyncio
    async def test_single_role_returns_its_statuses(self, service: WorkflowService) -> None:
        session = AsyncMock()

        async def mock_get_allowed(sess, tracker_id, role_id, old_status_id):
            return [2, 3]

        with patch.object(service, "_get_allowed_for_role", side_effect=mock_get_allowed):
            result = await service.get_allowed_statuses(session, tracker_id=1, role_ids=[1], current_status_id=1)

        assert sorted(result) == [2, 3]


class TestFieldRulesAggregation:
    """Field rule aggregation across multiple roles."""

    @pytest.mark.asyncio
    async def test_field_rules_required_if_any_role(self, service: WorkflowService) -> None:
        """If ANY role marks a field required, it is required."""
        session = AsyncMock()

        # Role 1: assigned_to is required
        # Role 2: no rules for assigned_to
        async def mock_get_rules(sess, tracker_id, role_id, status_id):
            if role_id == 1:
                return {"assigned_to_id": "required"}
            return {}

        with patch.object(service, "_get_field_rules_for_role", side_effect=mock_get_rules):
            result = await service.get_field_rules(session, tracker_id=1, role_ids=[1, 2], status_id=3)

        assert result["assigned_to_id"] == "required"

    @pytest.mark.asyncio
    async def test_field_rules_readonly_only_if_all_roles(self, service: WorkflowService) -> None:
        """A field is readonly only if ALL roles mark it readonly."""
        session = AsyncMock()

        # Role 1: subject is readonly
        # Role 2: subject is NOT in rules (not readonly)
        async def mock_get_rules(sess, tracker_id, role_id, status_id):
            if role_id == 1:
                return {"subject": "readonly"}
            return {}

        with patch.object(service, "_get_field_rules_for_role", side_effect=mock_get_rules):
            result = await service.get_field_rules(session, tracker_id=1, role_ids=[1, 2], status_id=3)

        # Not readonly because role 2 does not mark it readonly
        assert "subject" not in result

    @pytest.mark.asyncio
    async def test_field_rules_readonly_when_all_agree(self, service: WorkflowService) -> None:
        """A field is readonly when ALL roles mark it readonly."""
        session = AsyncMock()

        async def mock_get_rules(sess, tracker_id, role_id, status_id):
            return {"subject": "readonly"}

        with patch.object(service, "_get_field_rules_for_role", side_effect=mock_get_rules):
            result = await service.get_field_rules(session, tracker_id=1, role_ids=[1, 2], status_id=3)

        assert result["subject"] == "readonly"

    @pytest.mark.asyncio
    async def test_required_overrides_readonly(self, service: WorkflowService) -> None:
        """If one role marks required and another readonly, required wins."""
        session = AsyncMock()

        async def mock_get_rules(sess, tracker_id, role_id, status_id):
            if role_id == 1:
                return {"assigned_to_id": "required"}
            return {"assigned_to_id": "readonly"}

        with patch.object(service, "_get_field_rules_for_role", side_effect=mock_get_rules):
            result = await service.get_field_rules(session, tracker_id=1, role_ids=[1, 2], status_id=3)

        assert result["assigned_to_id"] == "required"
