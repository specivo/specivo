"""Unit tests for User model and UserFactory.

All tests are pure — no database required. They validate that:
- UserFactory produces a valid User instance with expected defaults.
- Field constraints and defaults are set correctly on the model.
- The model __repr__ works as expected.

Tests that require database (expression-based unique indexes,
case-insensitive uniqueness enforcement) are integration tests
and are marked accordingly. They are skipped when no DB is available.
"""

import pytest

from tests.factories.user import (
    TEST_PASSWORD,
    AdminUserFactory,
    ServiceAccountFactory,
    UserFactory,
)


@pytest.mark.unit
class TestUserFactory:
    def test_build_returns_user_instance(self):
        from specivo.models.user import User

        user = UserFactory.build()
        assert isinstance(user, User)

    def test_default_status_is_active(self):
        user = UserFactory.build()
        assert user.status == "active"

    def test_default_is_admin_false(self):
        user = UserFactory.build()
        assert user.is_admin is False

    def test_default_is_service_account_false(self):
        user = UserFactory.build()
        assert user.is_service_account is False

    def test_default_language_is_en(self):
        user = UserFactory.build()
        assert user.language == "en"

    def test_default_timezone_is_utc(self):
        user = UserFactory.build()
        assert user.timezone == "UTC"

    def test_default_failed_login_count_is_zero(self):
        user = UserFactory.build()
        assert user.failed_login_count == 0

    def test_default_preferences_has_avatar_color(self):
        user = UserFactory.build()
        assert isinstance(user.preferences, dict)
        assert "avatar_color" in user.preferences

    def test_password_hash_is_set(self):
        user = UserFactory.build()
        assert user.password_hash is not None
        assert user.password_hash.startswith("$2")

    def test_password_hash_is_valid_for_test_password(self):
        """The factory's pre-hashed password must verify against TEST_PASSWORD."""
        from specivo.services.auth_utils import verify_password

        user = UserFactory.build()
        assert verify_password(TEST_PASSWORD, user.password_hash) is True

    def test_login_is_unique_across_builds(self):
        u1 = UserFactory.build()
        u2 = UserFactory.build()
        assert u1.login != u2.login

    def test_email_matches_login(self):
        user = UserFactory.build(login="tester")
        assert user.email == "tester@example.com"

    def test_override_fields(self):
        user = UserFactory.build(login="alice", status="pending_verification", is_admin=True)
        assert user.login == "alice"
        assert user.status == "pending_verification"
        assert user.is_admin is True

    def test_nullable_fields_are_none_by_default(self):
        user = UserFactory.build()
        assert user.locked_until is None
        assert user.email_verified_at is None
        assert user.last_login_at is None
        assert user.password_changed_at is None
        assert user.github_id is None
        assert user.google_id is None
        assert user.avatar_url is None

    def test_repr_contains_login_and_status(self):
        user = UserFactory.build(login="repr_test")
        r = repr(user)
        assert "repr_test" in r
        assert "active" in r


@pytest.mark.unit
class TestAdminUserFactory:
    def test_is_admin_true(self):
        user = AdminUserFactory.build()
        assert user.is_admin is True

    def test_is_not_service_account(self):
        user = AdminUserFactory.build()
        assert user.is_service_account is False


@pytest.mark.unit
class TestServiceAccountFactory:
    def test_is_service_account_true(self):
        user = ServiceAccountFactory.build()
        assert user.is_service_account is True

    def test_no_password_hash(self):
        """Service accounts have no password — they authenticate via API keys."""
        user = ServiceAccountFactory.build()
        assert user.password_hash is None

    def test_is_not_admin(self):
        user = ServiceAccountFactory.build()
        assert user.is_admin is False


@pytest.mark.unit
class TestUserModelConstraints:
    def test_valid_statuses(self):
        """All valid status values should be constructable on the model."""
        valid_statuses = ["active", "locked", "pending_verification", "deactivated"]
        for status in valid_statuses:
            user = UserFactory.build(status=status)
            assert user.status == status

    def test_preferences_default_is_independent_per_instance(self):
        """Each User instance must get its own preferences dict, not a shared reference."""
        u1 = UserFactory.build()
        u2 = UserFactory.build()
        u1.preferences["theme"] = "dark"
        assert u2.preferences.get("theme") is None
