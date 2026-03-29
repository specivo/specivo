"""Unit tests for the permission service.

All tests are pure — no database required. They validate:
- Admin always has any permission.
- check_permission against a Role.permissions list (M1.3 stub returns False for non-admins).
- _role_grants helper: wildcard "*" grants all, specific string grants exactly that permission.
- Missing permission returns False from _role_grants.
- PERMISSIONS catalogue has expected keys.
"""

from __future__ import annotations

import pytest

from specivo.services.permission_service import PERMISSIONS, _role_grants


@pytest.mark.unit
class TestRoleGrantsHelper:
    """Unit tests for the _role_grants internal helper."""

    def test_wildcard_grants_any_permission(self):
        assert _role_grants(["*"], "add_issues") is True

    def test_wildcard_grants_unknown_permission(self):
        assert _role_grants(["*"], "some_future_permission") is True

    def test_explicit_permission_grants_that_permission(self):
        assert _role_grants(["add_issues", "view_issues"], "add_issues") is True

    def test_explicit_permission_does_not_grant_other(self):
        assert _role_grants(["add_issues", "view_issues"], "delete_issues") is False

    def test_empty_list_grants_nothing(self):
        assert _role_grants([], "view_issues") is False

    def test_missing_permission_returns_false(self):
        assert _role_grants(["manage_members"], "log_time") is False

    def test_partial_match_is_not_a_grant(self):
        # "add_issue" should not match "add_issues"
        assert _role_grants(["add_issue"], "add_issues") is False

    def test_wildcard_in_mixed_list(self):
        assert _role_grants(["view_issues", "*", "log_time"], "delete_issues") is True


@pytest.mark.unit
class TestPermissionsCatalogue:
    """Ensure the PERMISSIONS dict is complete and well-formed."""

    def test_permissions_is_not_empty(self):
        assert len(PERMISSIONS) > 0

    def test_all_values_are_strings(self):
        for key, value in PERMISSIONS.items():
            assert isinstance(key, str), f"Key {key!r} is not a string"
            assert isinstance(value, str), f"Value for {key!r} is not a string"

    def test_expected_core_permissions_present(self):
        expected = [
            "add_issues",
            "edit_issues",
            "delete_issues",
            "view_issues",
            "add_issue_notes",
            "manage_members",
            "log_time",
            "view_time_entries",
            "manage_project",
        ]
        for perm in expected:
            assert perm in PERMISSIONS, f"Expected permission {perm!r} not found"

    def test_no_duplicate_values(self):
        """Each human label should be unique."""
        values = list(PERMISSIONS.values())
        assert len(values) == len(set(values)), "Duplicate labels found in PERMISSIONS"

    def test_keys_are_snake_case(self):
        import re

        for key in PERMISSIONS:
            assert re.match(r"^[a-z][a-z0-9_]*$", key), f"Key {key!r} is not snake_case"


@pytest.mark.unit
class TestCheckPermissionAdmin:
    """Admin bypass: check_permission must return True for admins without any DB query."""

    @pytest.mark.asyncio
    async def test_admin_has_any_permission(self):
        """Admin should pass for every known permission without a DB query."""
        from unittest.mock import AsyncMock

        from specivo.services.permission_service import check_permission
        from tests.factories.user import AdminUserFactory

        admin = AdminUserFactory.build()
        mock_session = AsyncMock()

        for perm in PERMISSIONS:
            result = await check_permission(admin, project_id=1, permission=perm, session=mock_session)
            assert result is True, f"Admin should have permission {perm!r}"

        # Ensure the mock session was never queried (admin check is a pure Python short-circuit)
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_has_unknown_permission(self):
        """Admin should pass even for a permission not in the catalogue."""
        from unittest.mock import AsyncMock

        from specivo.services.permission_service import check_permission
        from tests.factories.user import AdminUserFactory

        admin = AdminUserFactory.build()
        result = await check_permission(admin, project_id=None, permission="nonexistent_perm", session=AsyncMock())
        assert result is True

    @pytest.mark.asyncio
    async def test_non_admin_no_membership_returns_false(self):
        """Non-admin with no project membership returns False."""
        from unittest.mock import AsyncMock, MagicMock

        from specivo.services.permission_service import check_permission
        from tests.factories.user import UserFactory

        user = UserFactory.build(is_admin=False)
        # Mock session.execute to return empty result (no membership)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        result = await check_permission(user, project_id=1, permission="view_issues", session=mock_session)
        assert result is False

    @pytest.mark.asyncio
    async def test_non_admin_returns_false_for_no_project(self):
        """Non-admin with project_id=None returns False (no project context)."""
        from unittest.mock import AsyncMock

        from specivo.services.permission_service import check_permission
        from tests.factories.user import UserFactory

        user = UserFactory.build(is_admin=False)
        result = await check_permission(user, project_id=None, permission="view_issues", session=AsyncMock())
        assert result is False
