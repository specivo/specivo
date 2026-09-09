"""Integration tests for the forced password change (``users.must_change_password``).

Covers:
- The JSON API refuses a flagged user with ``password_change_required``
- Each exempt path stays reachable while the flag is set
- An API key belonging to a flagged user keeps working — the flag gates
  password sessions, not agents
- Web pages redirect to the standalone change-password page, and following
  the redirects terminates there rather than looping
- Changing the password clears the flag and the app opens up
- Completing an email reset clears it too
- The CHECK rejects the flag on a service account at the database level
- Admin create and admin reset set it, and refuse to set it on a service
  account with a readable error rather than an integrity error
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.auth import PasswordResetToken
from specivo.models.user import User
from specivo.services.auth_service import _hash_token
from tests.factories.user import TEST_PASSWORD, ServiceAccountFactory, UserFactory

pytestmark = pytest.mark.integration

_NEW_PASSWORD = "a-password-only-i-know"
_CHANGE_URL = "/api/v1/auth/change-password/"
_CHANGE_PAGE = "/my/password/"
# A perfectly ordinary authenticated endpoint — nothing about it is special,
# which is the point: the flag has to close everything, not a chosen list.
_NORMAL_API_URL = "/api/v1/projects/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _flagged_user(db: AsyncSession, login: str, **kwargs) -> User:
    user = UserFactory.build(login=login, status="active", must_change_password=True, **kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str, password: str = TEST_PASSWORD) -> dict:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error_codes(resp) -> list[str]:
    return [e["code"] for e in resp.json().get("errors", [])]


# ---------------------------------------------------------------------------
# API refusal
# ---------------------------------------------------------------------------


class TestApiRefusesFlaggedUser:
    async def test_normal_endpoint_returns_403_password_change_required(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _flagged_user(db_session, "fpc_refused")
        tokens = await _login(client, user.login)

        resp = await client.get(_NORMAL_API_URL, headers=_bearer(tokens["access_token"]))

        assert resp.status_code == 403
        assert _error_codes(resp) == ["password_change_required"]

    async def test_message_says_where_to_go(self, client: AsyncClient, db_session: AsyncSession):
        """A refusal nobody can act on is a dead end, so the way out is named."""
        user = await _flagged_user(db_session, "fpc_message")
        tokens = await _login(client, user.login)

        resp = await client.get(_NORMAL_API_URL, headers=_bearer(tokens["access_token"]))

        assert _CHANGE_URL in resp.json()["errors"][0]["message"]

    async def test_cookie_session_is_refused_too(self, client: AsyncClient, db_session: AsyncSession):
        """The gate covers the browser's cookie, not only Bearer callers."""
        user = await _flagged_user(db_session, "fpc_cookie")
        tokens = await _login(client, user.login)

        resp = await client.get(_NORMAL_API_URL, cookies={"access_token": tokens["access_token"]})

        assert resp.status_code == 403
        assert _error_codes(resp) == ["password_change_required"]

    async def test_unflagged_user_reaches_the_same_endpoint(self, client: AsyncClient, db_session: AsyncSession):
        """The control: nothing about the endpoint is closed by itself."""
        user = UserFactory.build(login="fpc_unflagged", status="active")
        db_session.add(user)
        await db_session.commit()
        tokens = await _login(client, user.login)

        resp = await client.get(_NORMAL_API_URL, headers=_bearer(tokens["access_token"]))

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Exempt paths
# ---------------------------------------------------------------------------


class TestExemptPaths:
    async def test_change_password_is_reachable(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_exempt_change")
        tokens = await _login(client, user.login)

        resp = await client.post(
            _CHANGE_URL,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            headers=_bearer(tokens["access_token"]),
        )

        assert resp.status_code == 200, resp.text

    async def test_change_password_rejection_is_not_the_gate(self, client: AsyncClient, db_session: AsyncSession):
        """Even a wrong current password gets the endpoint's own answer, not a 403."""
        user = await _flagged_user(db_session, "fpc_exempt_change_bad")
        tokens = await _login(client, user.login)

        resp = await client.post(
            _CHANGE_URL,
            json={"current_password": "not-the-password", "new_password": _NEW_PASSWORD},
            headers=_bearer(tokens["access_token"]),
        )

        assert resp.status_code == 400
        assert _error_codes(resp) == ["password_current_invalid"]

    async def test_logout_is_reachable(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_exempt_logout")
        tokens = await _login(client, user.login)

        resp = await client.post(
            "/api/v1/auth/logout/",
            json={"refresh_token": tokens["refresh_token"]},
            headers=_bearer(tokens["access_token"]),
        )

        assert resp.status_code == 204

    async def test_logout_all_is_reachable(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_exempt_logout_all")
        tokens = await _login(client, user.login)

        resp = await client.post("/api/v1/auth/logout-all/", headers=_bearer(tokens["access_token"]))

        assert resp.status_code == 200
        assert resp.json()["revoked_count"] >= 1

    async def test_refresh_is_reachable(self, client: AsyncClient, db_session: AsyncSession):
        """Otherwise a session dies while its owner is on the change-password page."""
        user = await _flagged_user(db_session, "fpc_exempt_refresh")
        tokens = await _login(client, user.login)

        resp = await client.post(
            "/api/v1/auth/refresh/",
            json={"refresh_token": tokens["refresh_token"]},
            headers=_bearer(tokens["access_token"]),
        )

        assert resp.status_code == 200
        assert resp.json()["access_token"]

    async def test_health_is_reachable(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_exempt_health")
        tokens = await _login(client, user.login)

        resp = await client.get("/health/", headers=_bearer(tokens["access_token"]))

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class TestApiKeyIsUnaffected:
    async def test_api_key_of_a_flagged_user_still_works(self, client: AsyncClient, db_session: AsyncSession):
        """An agent's access must not depend on a flag set for a human session.

        The account is a person's — a service account cannot carry the flag at
        all — and the key it issued keeps working while its JWT sessions do not.
        """
        from specivo.services.api_key_service import ApiKeyService

        user = await _flagged_user(db_session, "fpc_keyholder")
        _key, raw_key = await ApiKeyService().create_key(
            session=db_session,
            user_id=user.id,
            name="fpc-key",
        )
        await db_session.commit()

        by_key = await client.get(_NORMAL_API_URL, headers=_bearer(raw_key))
        assert by_key.status_code == 200, by_key.text

        # Same user, same endpoint, password session: refused.
        tokens = await _login(client, user.login)
        by_jwt = await client.get(_NORMAL_API_URL, headers=_bearer(tokens["access_token"]))
        assert by_jwt.status_code == 403


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


class TestWebRedirect:
    async def test_a_page_redirects_to_the_change_password_page(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_web_redirect")
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get("/projects/")

        assert resp.status_code == 302
        assert resp.headers["location"] == _CHANGE_PAGE

    async def test_following_the_redirects_terminates(self, client: AsyncClient, db_session: AsyncSession):
        """The redirect must land somewhere that renders, not bounce forever.

        ``httpx`` raises ``TooManyRedirects`` on a loop, so reaching a 200 at
        all is the assertion; the URL confirms it stopped at the right page.
        """
        user = await _flagged_user(db_session, "fpc_web_terminates")
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get("/projects/", follow_redirects=True)

        assert resp.status_code == 200
        assert resp.url.path == _CHANGE_PAGE
        assert len(resp.history) == 1

    async def test_the_change_password_page_does_not_redirect_to_itself(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _flagged_user(db_session, "fpc_web_no_self_redirect")
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get(_CHANGE_PAGE)

        assert resp.status_code == 200
        assert "changePasswordForm(" in resp.text

    async def test_the_forced_page_explains_why_and_offers_a_way_out(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _flagged_user(db_session, "fpc_web_explains")
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get(_CHANGE_PAGE)

        assert "set by somebody else" in resp.text
        assert 'href="/logout/"' in resp.text
        # The forced page sends the browser into the app once the flag clears.
        assert 'redirectTo: "/"' in resp.text

    async def test_the_login_page_redirects_too(self, client: AsyncClient, db_session: AsyncSession):
        """/login/ resolves the user in order to greet them, so it is gated too."""
        user = await _flagged_user(db_session, "fpc_web_login_page")
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get("/login/")

        assert resp.status_code == 302
        assert resp.headers["location"] == _CHANGE_PAGE

    async def test_an_admin_page_redirects_before_the_admin_check(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_web_admin", is_admin=True)
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get("/admin/users/", follow_redirects=True)

        assert resp.status_code == 200
        assert resp.url.path == _CHANGE_PAGE

    async def test_an_unflagged_user_may_visit_the_page_voluntarily(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = UserFactory.build(login="fpc_web_voluntary", status="active")
        db_session.add(user)
        await db_session.commit()
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get(_CHANGE_PAGE)

        assert resp.status_code == 200
        assert "changePasswordForm(" in resp.text
        # No explanation, and no redirect afterwards — they asked to be here.
        assert "set by somebody else" not in resp.text
        assert 'redirectTo: ""' in resp.text

    async def test_an_unflagged_user_reaches_a_normal_page(self, client: AsyncClient, db_session: AsyncSession):
        user = UserFactory.build(login="fpc_web_control", status="active")
        db_session.add(user)
        await db_session.commit()
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        resp = await client.get("/projects/")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Clearing the flag
# ---------------------------------------------------------------------------


class TestFlagIsCleared:
    async def test_changing_the_password_opens_the_app(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_cleared_by_change")
        tokens = await _login(client, user.login)

        changed = await client.post(
            _CHANGE_URL,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            headers=_bearer(tokens["access_token"]),
        )
        assert changed.status_code == 200, changed.text

        await db_session.refresh(user)
        assert user.must_change_password is False

        after = await client.get(_NORMAL_API_URL, headers=_bearer(tokens["access_token"]))
        assert after.status_code == 200

    async def test_web_pages_open_up_after_the_change(self, client: AsyncClient, db_session: AsyncSession):
        user = await _flagged_user(db_session, "fpc_cleared_web")
        tokens = await _login(client, user.login)
        client.cookies.set("access_token", tokens["access_token"])

        changed = await client.post(
            _CHANGE_URL,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            headers=_bearer(tokens["access_token"]),
        )
        assert changed.status_code == 200, changed.text

        resp = await client.get("/projects/")
        assert resp.status_code == 200

    async def test_completing_an_email_reset_clears_it(self, client: AsyncClient, db_session: AsyncSession):
        """They chose this password themselves; asking again would be absurd."""
        user = await _flagged_user(db_session, "fpc_cleared_by_reset")

        raw_token = "fpc-reset-token-value"
        db_session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=_hash_token(raw_token),
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        await db_session.commit()

        resp = await client.post(
            "/api/v1/auth/reset-password/",
            json={"token": raw_token, "new_password": _NEW_PASSWORD},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(user)
        assert user.must_change_password is False

        tokens = await _login(client, user.login, _NEW_PASSWORD)
        after = await client.get(_NORMAL_API_URL, headers=_bearer(tokens["access_token"]))
        assert after.status_code == 200


# ---------------------------------------------------------------------------
# Database constraint
# ---------------------------------------------------------------------------


class TestServiceAccountConstraint:
    async def test_check_rejects_a_flagged_service_account(self, db_session: AsyncSession):
        """The rule survives any code path that forgets it."""
        db_session.add(ServiceAccountFactory.build(login="fpc_agent_flagged", must_change_password=True))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_an_unflagged_service_account_is_fine(self, db_session: AsyncSession):
        agent = ServiceAccountFactory.build(login="fpc_agent_plain")
        db_session.add(agent)
        await db_session.flush()

        assert agent.id is not None
        assert agent.must_change_password is False


# ---------------------------------------------------------------------------
# Admin: creating a user
# ---------------------------------------------------------------------------


async def _created_user(db: AsyncSession, login: str) -> User:
    result = await db.execute(select(User).where(User.login == login))
    return result.scalar_one()


class TestAdminCreate:
    async def test_a_new_account_with_a_password_is_flagged_by_default(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        """The admin typed this password, so it must not be the one that sticks."""
        resp = await admin_client.post(
            "/api/v1/admin/users/",
            json={
                "login": "fpc_created",
                "email": "fpc_created@example.com",
                "display_name": "Created User",
                "password": "handed-over-password",
            },
        )
        assert resp.status_code == 201, resp.text

        user = await _created_user(db_session, "fpc_created")
        assert user.must_change_password is True

    async def test_an_admin_can_opt_out(self, admin_client: AsyncClient, db_session: AsyncSession):
        resp = await admin_client.post(
            "/api/v1/admin/users/",
            json={
                "login": "fpc_created_optout",
                "email": "fpc_created_optout@example.com",
                "display_name": "Opted Out",
                "password": "handed-over-password",
                "must_change_password": False,
            },
        )
        assert resp.status_code == 201, resp.text

        user = await _created_user(db_session, "fpc_created_optout")
        assert user.must_change_password is False

    async def test_a_service_account_is_never_flagged_by_default(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        resp = await admin_client.post(
            "/api/v1/admin/users/",
            json={
                "login": "fpc_created_agent",
                "email": "fpc_created_agent@example.com",
                "display_name": "Created Agent",
                "is_service_account": True,
            },
        )
        assert resp.status_code == 201, resp.text

        user = await _created_user(db_session, "fpc_created_agent")
        assert user.must_change_password is False

    async def test_flagging_a_service_account_is_a_readable_422(self, admin_client: AsyncClient):
        """Not an integrity error leaking out of the database."""
        resp = await admin_client.post(
            "/api/v1/admin/users/",
            json={
                "login": "fpc_created_agent_flagged",
                "email": "fpc_created_agent_flagged@example.com",
                "display_name": "Flagged Agent",
                "is_service_account": True,
                "must_change_password": True,
            },
        )

        assert resp.status_code == 422
        error = resp.json()["errors"][0]
        assert error["code"] == "validation_error"
        assert error["field"] == "must_change_password"
        assert "API key" in error["message"]


# ---------------------------------------------------------------------------
# Admin: resetting a password
# ---------------------------------------------------------------------------


class TestAdminResetPassword:
    async def test_reset_flags_the_account_by_default(self, admin_client: AsyncClient, db_session: AsyncSession):
        user = UserFactory.build(login="fpc_reset_target", status="active")
        db_session.add(user)
        await db_session.commit()

        resp = await admin_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/",
            json={"password": "an-admin-chose-this"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(user)
        assert user.must_change_password is True

    async def test_an_admin_can_opt_out(self, admin_client: AsyncClient, db_session: AsyncSession):
        user = UserFactory.build(login="fpc_reset_optout", status="active")
        db_session.add(user)
        await db_session.commit()

        resp = await admin_client.post(
            f"/api/v1/admin/users/{user.id}/reset-password/",
            json={"password": "an-admin-chose-this", "must_change_password": False},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(user)
        assert user.must_change_password is False

    async def test_resetting_a_service_account_does_not_flag_it(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        agent = ServiceAccountFactory.build(login="fpc_reset_agent", status="active")
        db_session.add(agent)
        await db_session.commit()

        resp = await admin_client.post(
            f"/api/v1/admin/users/{agent.id}/reset-password/",
            json={"password": "an-admin-chose-this"},
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(agent)
        assert agent.must_change_password is False

    async def test_flagging_a_service_account_is_a_readable_422(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        agent = ServiceAccountFactory.build(login="fpc_reset_agent_flagged", status="active")
        db_session.add(agent)
        await db_session.commit()
        original_hash = agent.password_hash

        resp = await admin_client.post(
            f"/api/v1/admin/users/{agent.id}/reset-password/",
            json={"password": "an-admin-chose-this", "must_change_password": True},
        )

        assert resp.status_code == 422
        error = resp.json()["errors"][0]
        assert error["field"] == "must_change_password"

        # Refused before anything was written.
        await db_session.refresh(agent)
        assert agent.password_hash == original_hash
        assert agent.must_change_password is False
