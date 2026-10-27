"""SSO claims and group mapping through the browser callback.

A policy refusal (domain, required group, Entra group overage) ends on
``/login/?sso_error=<code>`` with no new session, revokes the identity's
existing sessions, and the login page shows a translated message.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoGroupMapping, SsoProvider
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.auth_service import AuthService
from specivo.testing.factories.user import UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import SsoLoginDriver, link_identity
from specivo.web.pages.sso import sso_error_message

pytestmark = pytest.mark.integration


def _sso_error(response: Any) -> str:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert urlsplit(location).path == "/login/", location
    return parse_qs(urlsplit(location).query)["sso_error"][0]


async def _member(db: AsyncSession, provider: SsoProvider, idp: MockIdP, **fields: Any) -> User:
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    identity = await link_identity(db, user, provider, subject=idp.default_sub)
    await AuthService().create_sso_session(
        db, user=user, identity=identity, provider=provider, id_token="not-a-real-token", idp_sid=None
    )
    return user


async def _sessions(db: AsyncSession, user_id: int) -> int:
    return int(await db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)))


async def test_overage_token_with_required_groups_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    sso_provider.required_groups = ["mflow-staff"]
    user = await _member(db_session, sso_provider, mock_idp, login="mflowover", email="mflow-over@example.com")
    assert await _sessions(db_session, user.id) == 1

    response = await sso_login(client, id_token_variant="groups_overage")

    assert _sso_error(response) == "sso_groups_overage"
    assert await _sessions(db_session, user.id) == 0


async def test_not_in_required_group_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    sso_provider.required_groups = ["mflow-staff"]
    user = await _member(db_session, sso_provider, mock_idp, login="mflowgrp", email="mflow-grp@example.com")

    response = await sso_login(client, claims={"groups": ["mflow-guests"]})

    assert _sso_error(response) == "sso_not_in_required_group"
    assert await _sessions(db_session, user.id) == 0


async def test_domain_not_allowed_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    sso_provider.allowed_email_domains = ["corp.example.com"]
    await _member(db_session, sso_provider, mock_idp, login="mflowdom", email="mflow-dom@example.com")

    response = await sso_login(client, claims={"email": "mflow-dom@example.com"})

    assert _sso_error(response) == "sso_domain_not_allowed"


async def test_login_syncs_groups(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
) -> None:
    sso_provider.sync_groups = True
    group = UserGroup(name="mflow-developers")
    db_session.add(group)
    await db_session.flush()
    db_session.add(SsoGroupMapping(provider_id=sso_provider.id, idp_group="mflow-dev", user_group_id=group.id))
    user = await _member(db_session, sso_provider, mock_idp, login="mflowsync", email="mflow-sync@example.com")

    response = await sso_login(client, claims={"groups": ["mflow-dev"]})

    assert response.status_code == 302
    assert "access_token" in response.headers.get("set-cookie", "")
    managed = await db_session.scalar(
        select(UserGroupMember.managed_by_provider_id).where(
            UserGroupMember.user_id == user.id, UserGroupMember.group_id == group.id
        )
    )
    assert managed == sso_provider.id


@pytest.mark.parametrize("code", ["sso_not_in_required_group", "sso_domain_not_allowed", "sso_groups_overage"])
def test_policy_codes_have_their_own_message(code: str) -> None:
    generic = sso_error_message("sso_no_such_code")
    assert sso_error_message(code) not in (None, generic)


async def test_login_page_shows_the_overage_message(client: AsyncClient) -> None:
    page = await client.get("/login/", params={"sso_error": "sso_groups_overage"})
    assert page.status_code == 200
    assert "too many groups" in page.text
