"""Offboarding against the real IdPs: what ends a Specivo session, and what happens to API keys.

For each IdP (Keycloak, authentik) a throwaway user signs in through JIT
provisioning, then an administrator disables, deletes or signs them out at
the IdP:

* **Session check** (no back-channel needed): the next renewal of the
  Specivo session asks the IdP, which refuses the refresh token, so the
  renewal is 401 and the session row is gone. The one exception is an admin
  sign-out at authentik, which leaves an ``offline_access`` refresh token
  valid: that case is the back-channel logout's. API keys are untouched by
  the session check.
* **Back-channel logout with ``deprovision``**: the logout token the IdP
  sends (caught on port 9944, then handed to the app) ends every session and
  switches the API keys off; the account stays active. authentik sends one on
  a disable, Keycloak on an admin sign-out.

Requires ``make sso-idp-up`` / ``make sso-authentik-up``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.auth import ApiKey, RefreshToken
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from tests.integration.sso_idp.conftest import BackchannelCatcher, sign_in
from tests.integration.sso_idp.idps import Idp, IdpUser

pytestmark = [pytest.mark.sso_idp, pytest.mark.integration]

REFRESH_PATH = "/api/v1/auth/refresh/"


@pytest_asyncio.fixture
async def idp_user(idp: Idp) -> AsyncIterator[IdpUser]:
    user = await idp.create_user()
    try:
        yield user
    finally:
        await idp.cleanup(user)


async def _signed_in(app: httpx.AsyncClient, db: AsyncSession, idp: Idp, idp_user: IdpUser) -> tuple[int, str]:
    """Sign ``idp_user`` in (JIT); returns the Specivo user id and refresh token."""
    response = await sign_in(app, idp, idp_user.username)
    assert response.status_code == 302, response.headers.get("location")
    assert "sso_error" not in response.headers["location"], response.headers["location"]
    user_id = await db.scalar(select(User.id).where(func.lower(User.email) == idp_user.email))
    assert user_id is not None
    row = (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))).scalar_one()
    assert row.idp_refresh_token_encrypted, "the IdP issued no usable refresh token"
    token = response.cookies.get("refresh_token")
    assert token
    return user_id, token


async def _make_check_due(db: AsyncSession, user_id: int) -> None:
    await db.execute(
        update(RefreshToken).where(RefreshToken.user_id == user_id).values(idp_checked_at=utcnow() - timedelta(hours=1))
    )
    await db.flush()


async def _sessions(db: AsyncSession, user_id: int) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)) or 0
    )


async def _keys(db: AsyncSession, user_id: int) -> list[bool]:
    rows = await db.execute(select(ApiKey.is_active).where(ApiKey.user_id == user_id).order_by(ApiKey.id))
    return list(rows.scalars().all())


async def _add_keys(db: AsyncSession, user_id: int) -> None:
    await ApiKeyService().create_key(db, user_id, "ci")
    await ApiKeyService().create_key(db, user_id, "agent", scopes=["mcp"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Session check at renewal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["disable", "delete", "sign_out"])
async def test_next_renewal_ends_the_session(
    sso_app: httpx.AsyncClient,
    db_session: AsyncSession,
    idp: Idp,
    idp_provider: SsoProvider,
    idp_user: IdpUser,
    action: str,
) -> None:
    user_id, token = await _signed_in(sso_app, db_session, idp, idp_user)
    await _add_keys(db_session, user_id)

    await getattr(idp, action)(idp_user)
    await _make_check_due(db_session, user_id)
    response = await sso_app.post(REFRESH_PATH, json={"refresh_token": token})

    if action == "sign_out" and not idp.signout_refuses_refresh:
        # authentik keeps an offline_access token valid across an admin
        # sign-out; its back-channel logout ends the session instead.
        assert response.status_code == 200, response.text
        return
    assert response.status_code == 401, response.text
    assert await _sessions(db_session, user_id) == 0
    # The session check never touches API keys.
    assert await _keys(db_session, user_id) == [True, True]


async def test_renewal_still_works_while_the_idp_accepts(
    sso_app: httpx.AsyncClient,
    db_session: AsyncSession,
    idp: Idp,
    idp_provider: SsoProvider,
    idp_user: IdpUser,
) -> None:
    user_id, token = await _signed_in(sso_app, db_session, idp, idp_user)
    for _ in range(2):  # the second presents the token the IdP rotated, if it did
        await _make_check_due(db_session, user_id)
        response = await sso_app.post(REFRESH_PATH, json={"refresh_token": token})
        assert response.status_code == 200, response.text
        token = response.json()["refresh_token"]
    assert await _sessions(db_session, user_id) == 1


# ---------------------------------------------------------------------------
# Back-channel logout with deprovision
# ---------------------------------------------------------------------------


async def _deliver(app: httpx.AsyncClient, catcher: BackchannelCatcher, slug: str) -> int:
    """Hand every caught logout token to the app; returns how many were delivered."""
    caught = catcher.wait(slug)
    assert caught, "the IdP sent no back-channel logout to port 9944"
    for item in caught:
        response = await app.post(item.path, data={"logout_token": item.logout_token})
        assert response.status_code == 200, response.text
    return len(caught)


@pytest.mark.parametrize(
    "idp_name,action",
    [("authentik", "disable"), ("authentik", "sign_out"), ("keycloak", "sign_out")],
)
async def test_backchannel_deprovision_switches_the_keys_off(
    sso_app: httpx.AsyncClient,
    db_session: AsyncSession,
    idp: Idp,
    idp_provider: SsoProvider,
    idp_user: IdpUser,
    backchannel_catcher: BackchannelCatcher,
    idp_name: str,
    action: str,
) -> None:
    if idp.name != idp_name:
        pytest.skip(f"{idp.display_name} sends no back-channel logout for this action")
    idp_provider.backchannel_action = "deprovision"
    await db_session.flush()
    user_id, token = await _signed_in(sso_app, db_session, idp, idp_user)
    await _add_keys(db_session, user_id)

    await getattr(idp, action)(idp_user)
    await _deliver(sso_app, backchannel_catcher, idp.slug)

    assert await _sessions(db_session, user_id) == 0
    assert await _keys(db_session, user_id) == [False, False]
    status = await db_session.scalar(select(User.status).where(User.id == user_id))
    assert status == "active"
    assert (await sso_app.post(REFRESH_PATH, json={"refresh_token": token})).status_code == 401


async def test_keycloak_sign_out_ends_the_session_at_once(
    sso_app: httpx.AsyncClient,
    db_session: AsyncSession,
    idp: Idp,
    idp_provider: SsoProvider,
    idp_user: IdpUser,
    backchannel_catcher: BackchannelCatcher,
) -> None:
    """With the default action the back-channel logout ends the session; the keys stay on."""
    if idp.name != "keycloak":
        pytest.skip("covered for authentik by the deprovision test")
    user_id, _ = await _signed_in(sso_app, db_session, idp, idp_user)
    await _add_keys(db_session, user_id)

    await idp.sign_out(idp_user)
    await _deliver(sso_app, backchannel_catcher, idp.slug)

    assert await _sessions(db_session, user_id) == 0
    assert await _keys(db_session, user_id) == [True, True]
