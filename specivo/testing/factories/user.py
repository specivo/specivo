"""factory_boy factory for User model.

Produces User instances with a pre-hashed test password so tests never
call bcrypt at full cost. The test password is "testpassword" (10 chars,
meets the min-length policy).

Usage::

    # Unsaved instance (no DB required)
    user = UserFactory.build()

    # Saved instance (requires db_session fixture)
    user = await UserFactory.create_async(db_session)

Note: factory_boy's async SQLAlchemy support requires the session to be
passed explicitly. We provide a thin helper for this pattern.
"""

from __future__ import annotations

import factory

from specivo.models.user import User
from specivo.services.auth_utils import hash_password

# Pre-hash once at import time — bcrypt is expensive; tests should not pay
# full cost on every factory call. The hash is valid for "testpassword".
_TEST_PASSWORD = "testpassword"
_TEST_PASSWORD_HASH = hash_password(_TEST_PASSWORD)


class UserFactory(factory.Factory):
    """Builds User model instances.

    All fields have sensible defaults for testing. Override any field by
    passing kwargs::

        user = UserFactory.build(login="alice", is_admin=True)
    """

    class Meta:
        model = User

    login = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda obj: f"{obj.login}@example.com")
    password_hash = _TEST_PASSWORD_HASH
    display_name = factory.LazyAttribute(lambda obj: f"Test User {obj.login.capitalize()}")
    avatar_url = None
    language = "en"
    timezone = "UTC"
    status = "active"
    is_admin = False
    is_service_account = False
    failed_login_count = 0
    locked_until = None
    email_verified_at = None
    last_login_at = None
    password_changed_at = None
    github_id = None
    google_id = None
    preferences = factory.LazyFunction(dict)


class AdminUserFactory(UserFactory):
    """A UserFactory variant that produces admin users."""

    is_admin = True
    login = factory.Sequence(lambda n: f"admin{n}")
    display_name = factory.LazyAttribute(lambda obj: f"Admin {obj.login.capitalize()}")


class ServiceAccountFactory(UserFactory):
    """A UserFactory variant that produces service account users (agents)."""

    is_service_account = True
    password_hash = None  # Service accounts authenticate via API key, no password
    login = factory.Sequence(lambda n: f"agent{n}")
    display_name = factory.LazyAttribute(lambda obj: f"Agent {obj.login.capitalize()}")


# ---------------------------------------------------------------------------
# Plain-password constant for test assertions
# ---------------------------------------------------------------------------
TEST_PASSWORD = _TEST_PASSWORD
