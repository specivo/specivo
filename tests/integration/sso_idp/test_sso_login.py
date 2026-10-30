"""SSO sign-in against the disposable IdPs (Keycloak and authentik), end to end.

The browser is played by httpx: Specivo's start route redirects to the IdP,
the driver signs in there (Keycloak's form, authentik's flow executor), and
the IdP's redirect is handed to the callback route. The mock IdP covers the
protocol in depth; this proves the same code works with real IdPs (cookies,
iss parameter, sid, at_hash, hashed subjects) and records where they differ:
email verification, the ``locale`` claim, and how refresh tokens are issued.

Requires ``make sso-idp-up`` (Keycloak) and ``make sso-authentik-up``
(authentik); each IdP's half is skipped while it is down.
"""

from __future__ import annotations

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.testing.factories.user import UserFactory
from specivo.testing.sso import link_identity
from tests.fixtures.sso import seed
from tests.integration.sso_idp.conftest import sign_in
from tests.integration.sso_idp.idps import Idp

pytestmark = [pytest.mark.sso_idp, pytest.mark.integration]


async def test_member_signs_in(sso_app, db_session: AsyncSession, idp: Idp, idp_provider: SsoProvider) -> None:
    user = UserFactory.build(email=seed.SSO_MEMBER.email)
    db_session.add(user)
    await db_session.flush()
    await link_identity(db_session, user, idp_provider, subject=await idp.subject(seed.SSO_MEMBER.username))

    response = await sign_in(sso_app, idp, seed.SSO_MEMBER.username, next_path="/projects/")

    assert response.status_code == 302, response.headers.get("location")
    assert response.headers["location"] == "/projects/"
    row = (await db_session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    assert row.auth_method == "sso"
    assert row.idp_sid  # both put the IdP session id in the ID token
    access = jwt.decode(response.cookies["access_token"], get_settings().secret_key, algorithms=["HS256"])
    assert access["sid"] == str(row.session_id)
    # Both issue a refresh token the session is checked with (authentik for
    # offline_access only), and both accept it right after sign-in.
    assert row.idp_refresh_token_encrypted
    assert row.idp_checked_at is not None
    await db_session.refresh(idp_provider)
    assert idp_provider.session_recheck_status == "ok"


async def test_jit_account_reflects_what_the_idp_sends(
    sso_app, db_session: AsyncSession, idp: Idp, idp_provider: SsoProvider
) -> None:
    idp_provider.trust_email_verified = True
    await db_session.flush()

    response = await sign_in(sso_app, idp, seed.SSO_THAI.username)

    assert response.status_code == 302, response.headers.get("location")
    assert "sso_error" not in response.headers["location"]
    identity = (
        await db_session.execute(select(UserIdentity).where(UserIdentity.provider_id == idp_provider.id))
    ).scalar_one()
    assert identity.subject == await idp.subject(seed.SSO_THAI.username)
    if idp.name == "authentik":
        assert len(identity.subject) == 64  # the hashed user id
    user = (await db_session.execute(select(User).where(User.id == identity.user_id))).scalar_one()
    assert user.email == seed.SSO_THAI.email
    # Keycloak sends email_verified: true and locale; authentik neither.
    assert (user.email_verified_at is not None) is idp.email_verified
    assert (user.language == "th") is idp.sends_locale
    assert user.timezone == "UTC"


async def test_no_refresh_token_without_offline_access_at_authentik(
    sso_app, db_session: AsyncSession, idp: Idp
) -> None:
    """authentik issues refresh tokens only for offline_access; Keycloak for openid already."""
    from tests.integration.sso_idp.conftest import build_idp_provider

    provider = build_idp_provider(idp, scopes="openid profile email")
    db_session.add(provider)
    await db_session.flush()

    response = await sign_in(sso_app, idp, seed.SSO_MEMBER.username)

    assert response.status_code == 302, response.headers.get("location")
    await db_session.refresh(provider)
    expected = "no_refresh_token" if idp.name == "authentik" else "ok"
    assert provider.session_recheck_status == expected
