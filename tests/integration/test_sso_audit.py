"""SSO audit events end to end: the right rows when the audit feature is on, log lines always.

* With the enterprise ``security_audit_log`` feature registered, each SSO flow
  writes its ``AuditEvent`` (flushed by ``AuditBatchMiddleware`` after the
  response) with ids, counts, the provider slug, the issuer and the hashed
  subject. The actor is the user for their own actions, the admin for admin
  actions and nobody for what the IdP starts (back-channel logout).
* Without it nothing is persisted (except the login success/failure rows,
  which are core events like the password login ones) and every event is
  still a ``specivo.sso`` log line.
* No log record and no audit row ever holds the client secret, the
  authorization code, the PKCE verifier, a JWT or the raw subject.
* A failing audit write never breaks the login.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.main import get_plugin_manager
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService
from specivo.services.sso import login as login_module
from specivo.services.sso.events import subject_hash
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory
from specivo.testing.oidc_mock import MockIdP
from specivo.testing.sso import TX_COOKIE, SsoLoginDriver, link_identity

pytestmark = pytest.mark.integration

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_on() -> Iterator[None]:
    """Register the enterprise audit feature (the autouse snapshot fixture removes it again)."""
    get_plugin_manager().feature_registry.register("security_audit_log", "test")
    yield


@pytest.fixture
def audit_off() -> None:
    assert not get_plugin_manager().feature_registry.has_feature("security_audit_log")


class Captured:
    """Everything secret the login exchanged: the code, the verifier and the tokens."""

    def __init__(self) -> None:
        self.values: list[str] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    seen = Captured()
    real = login_module.exchange_code

    async def spy(*args: Any, **kwargs: Any) -> Any:
        tokens = await real(*args, **kwargs)
        seen.values.extend([kwargs["code"], kwargs["code_verifier"], tokens.id_token, tokens.access_token])
        return tokens

    monkeypatch.setattr(login_module, "exchange_code", spy)
    return seen


async def _rows(db: AsyncSession, event: AuditEvent | None = None) -> list[SecurityAuditLog]:
    stmt = select(SecurityAuditLog).order_by(SecurityAuditLog.id)
    if event is not None:
        stmt = stmt.where(SecurityAuditLog.event_type == event.value)
    return list((await db.execute(stmt)).scalars().all())


def _set_cookie_value(resp: Response, name: str) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith(f"{name}="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


def _cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


async def _csrf(client: AsyncClient) -> str:
    resp = await client.get("/login/")
    token = _set_cookie_value(resp, "csrf_token")
    assert token
    return token


async def _sso_login(client: AsyncClient, sso_login: SsoLoginDriver, **kwargs: Any) -> dict[str, str]:
    """Sign in through the mock IdP; return the auth cookies."""
    response = await sso_login(client, **kwargs)
    assert response.status_code == 302, response.text
    assert "sso_error" not in response.headers["location"], response.headers["location"]
    cookies = {"access_token": _set_cookie_value(response, "access_token")}
    cookies["refresh_token"] = _set_cookie_value(response, "refresh_token")
    assert all(cookies.values())
    client.cookies.clear()
    return cookies


async def _web_logout(client: AsyncClient, cookies: dict[str, str]) -> Response:
    csrf = await _csrf(client)
    client.cookies.clear()
    return await client.post(
        "/logout/",
        data={"csrf_token": csrf},
        headers=_cookie_header({**cookies, "csrf_token": csrf}),
        follow_redirects=False,
    )


async def _password_browser(client: AsyncClient, user: User) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    cookies = {"access_token": body["access_token"], "refresh_token": body["refresh_token"]}
    cookies["csrf_token"] = await _csrf(client)
    client.cookies.clear()
    return cookies


async def _link(client: AsyncClient, idp: MockIdP, cookies: dict[str, str]) -> Response:
    started = await client.post(
        "/auth/oidc/test-idp/link/", data={"csrf_token": cookies["csrf_token"]}, headers=_cookie_header(cookies)
    )
    assert started.status_code == 303, started.text
    tx_cookie = _set_cookie_value(started, TX_COOKIE)
    callback_url = idp.authorize(started.headers["location"])
    path = callback_url.split("://", 1)[1].split("/", 1)[1]
    client.cookies.clear()
    return await client.get(f"/{path}", headers=_cookie_header({**cookies, TX_COOKIE: tx_cookie}))


async def _user(db: AsyncSession, **fields: Any) -> User:
    user = UserFactory.build(**fields)
    db.add(user)
    await db.flush()
    return user


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        parts.append(repr(record.__dict__))
    return "\n".join(parts) + caplog.text


def _sso_events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.event for r in caplog.records if r.name == "specivo.sso" and hasattr(r, "event")]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Login success / failure
# ---------------------------------------------------------------------------


async def test_sso_login_writes_sso_login_success_not_the_generic_event(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    audit_off: None,
) -> None:
    user = await _user(db_session)
    await link_identity(db_session, user, sso_provider, subject=mock_idp.default_sub)

    await _sso_login(client, sso_login)

    [row] = await _rows(db_session, AuditEvent.SSO_LOGIN_SUCCESS)
    assert row.user_id == user.id
    assert row.details == {
        "method": "sso",
        "provider": "test-idp",
        "iss": sso_provider.issuer,
        "sub_hash": subject_hash(mock_idp.default_sub),
    }
    assert await _rows(db_session, AuditEvent.LOGIN_SUCCESS) == []


async def test_sso_login_failure_records_the_error_code(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    audit_off: None,
) -> None:
    response = await sso_login(client, error="access_denied")
    assert "sso_error=sso_idp_error" in response.headers["location"]

    [row] = await _rows(db_session, AuditEvent.SSO_LOGIN_FAILURE)
    assert row.user_id is None
    assert row.details == {
        "method": "sso",
        "provider": "test-idp",
        "iss": sso_provider.issuer,
        "reason": "sso_idp_error",
    }
    assert await _rows(db_session, AuditEvent.LOGIN_FAILURE) == []


async def test_a_failing_login_audit_write_does_not_break_the_login(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    audit_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sso_provider.allow_jit_provisioning = True
    await db_session.flush()

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    def broken_record(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("audit buffer down")

    monkeypatch.setattr(SecurityAuditService, "log_sso_login_success", broken)
    monkeypatch.setattr("specivo.services.sso.events.record_event", broken_record)

    cookies = await _sso_login(client, sso_login, claims={"email": "audit-broken@example.com"})

    status = await client.get("/api/v1/auth/sessions/", headers={"Authorization": f"Bearer {cookies['access_token']}"})
    assert status.status_code == 200
    assert await db_session.scalar(select(User.id).where(User.email == "audit-broken@example.com")) is not None


# ---------------------------------------------------------------------------
# Account events
# ---------------------------------------------------------------------------


async def test_jit_provisioning_is_audited_when_the_feature_is_on(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    audit_on: None,
) -> None:
    sso_provider.allow_jit_provisioning = True
    await db_session.flush()

    await _sso_login(client, sso_login, claims={"email": "audit-jit@example.com"})

    user_id = await db_session.scalar(select(User.id).where(User.email == "audit-jit@example.com"))
    [row] = await _rows(db_session, AuditEvent.SSO_USER_PROVISIONED)
    assert row.user_id == user_id
    assert row.details["provider"] == "test-idp"
    assert row.details["iss"] == sso_provider.issuer
    assert row.details["sub_hash"] == subject_hash(mock_idp.default_sub)
    assert row.details["user_id"] == user_id


async def test_without_the_feature_only_log_lines_remain(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    audit_off: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sso_provider.allow_jit_provisioning = True
    await db_session.flush()

    with caplog.at_level(logging.INFO):
        await _sso_login(client, sso_login, claims={"email": "audit-jit-off@example.com"})

    assert [r.event_type for r in await _rows(db_session)] == [AuditEvent.SSO_LOGIN_SUCCESS.value]
    assert AuditEvent.SSO_USER_PROVISIONED.value in _sso_events(caplog)


async def test_link_and_unlink_are_audited_with_the_user_as_actor(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    mock_idp: MockIdP,
    audit_on: None,
) -> None:
    user = await _user(db_session, login="auditlinker", email="audit-linker@example.com")
    await db_session.commit()
    cookies = await _password_browser(client, user)

    linked = await _link(client, mock_idp, cookies)
    assert linked.status_code == 302, linked.text
    assert "sso_linked=test-idp" in linked.headers["location"]

    [row] = await _rows(db_session, AuditEvent.SSO_ACCOUNT_LINKED)
    assert row.user_id == user.id
    assert row.details["method"] == "user_link"
    assert row.details["sub_hash"] == subject_hash(mock_idp.default_sub)

    identity_id = await db_session.scalar(select(UserIdentity.id).where(UserIdentity.user_id == user.id))
    client.cookies.clear()
    unlinked = await client.post(
        f"/my/identities/{identity_id}/unlink/",
        data={"csrf_token": cookies["csrf_token"]},
        headers=_cookie_header(cookies),
    )
    assert unlinked.status_code == 303, unlinked.text

    [row] = await _rows(db_session, AuditEvent.SSO_ACCOUNT_UNLINKED)
    assert row.user_id == user.id
    assert row.details["by"] == "user"
    assert row.details["user_id"] == user.id
    assert row.details["provider"] == "test-idp"


async def test_admin_unlink_has_the_admin_as_actor(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    audit_on: None,
) -> None:
    target = await _user(db_session, login="audittarget", email="audit-target@example.com")
    identity = await link_identity(db_session, target, sso_provider, subject="audit-admin-unlink-subject")

    deleted = await admin_client.delete(f"/api/v1/admin/users/{target.id}/identities/{identity.id}/")
    assert deleted.status_code == 204, deleted.text

    admin_id = admin_client.state.user.id  # type: ignore[attr-defined]
    [row] = await _rows(db_session, AuditEvent.SSO_ACCOUNT_UNLINKED)
    assert row.user_id == admin_id
    assert row.details["by"] == "admin"
    assert row.details["user_id"] == target.id
    assert row.details["sub_hash"] == subject_hash("audit-admin-unlink-subject")
    # The target had no session to end, so nothing was revoked.
    assert await _rows(db_session, AuditEvent.SESSIONS_REVOKED) == []


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


async def test_rp_logout_and_session_revocation_are_audited(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    audit_on: None,
) -> None:
    user = await _user(db_session)
    await link_identity(db_session, user, sso_provider, subject=mock_idp.default_sub)
    cookies = await _sso_login(client, sso_login)

    response = await _web_logout(client, cookies)
    assert response.status_code in (302, 303), response.text

    [row] = await _rows(db_session, AuditEvent.SSO_LOGOUT)
    assert row.user_id == user.id
    assert row.details["provider"] == "test-idp"
    assert row.details["sub_hash"] == subject_hash(mock_idp.default_sub)
    assert row.details["idp_redirect"] is True
    [revoked] = await _rows(db_session, AuditEvent.SESSIONS_REVOKED)
    assert revoked.user_id == user.id
    assert revoked.details["reason"] == "logout"
    assert revoked.details["session_count"] == 1
    assert revoked.details["user_ids"] == [user.id]


async def test_backchannel_logout_is_audited_without_an_actor(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    audit_on: None,
) -> None:
    sso_provider.backchannel_logout_enabled = True
    user = await _user(db_session)
    await link_identity(db_session, user, sso_provider, subject=mock_idp.default_sub)
    await _sso_login(client, sso_login, claims={"sid": "audit-idp-sid"})

    token = mock_idp.mint_logout_token(sid="audit-idp-sid")
    response = await client.post("/auth/oidc/test-idp/backchannel-logout/", data={"logout_token": token})
    assert response.status_code == 200, response.text

    [row] = await _rows(db_session, AuditEvent.SSO_BACKCHANNEL_LOGOUT)
    assert row.user_id is None
    assert row.details == {
        "provider": "test-idp",
        "iss": sso_provider.issuer,
        "sub_hash": subject_hash(mock_idp.default_sub),
        "by": "sid",
        "sessions_revoked": 1,
    }
    [revoked] = await _rows(db_session, AuditEvent.SESSIONS_REVOKED)
    assert revoked.user_id is None
    assert revoked.details["reason"] == "backchannel"
    assert revoked.details["user_ids"] == [user.id]


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


async def test_provider_crud_is_audited_with_the_admin_as_actor(
    admin_client: AsyncClient, db_session: AsyncSession, audit_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "audit-provider-secret-0001"
    caplog.set_level(logging.DEBUG)
    created = await admin_client.post(
        "/api/v1/admin/sso/providers/",
        json={
            "slug": "audit-crud",
            "display_name": "Audit IdP",
            "issuer": "https://idp.example.com/realms/audit",
            "client_id": "specivo",
            "client_secret": secret,
        },
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]
    updated = await admin_client.patch(
        f"/api/v1/admin/sso/providers/{provider_id}/",
        json={"display_name": "Renamed IdP", "client_secret": "audit-provider-secret-0002"},
    )
    assert updated.status_code == 200, updated.text
    deleted = await admin_client.delete(f"/api/v1/admin/sso/providers/{provider_id}/")
    assert deleted.status_code == 204, deleted.text

    admin_id = admin_client.state.user.id  # type: ignore[attr-defined]
    rows = [r for r in await _rows(db_session) if r.event_type.startswith("sso_provider_")]
    assert [r.event_type for r in rows] == [
        AuditEvent.SSO_PROVIDER_CREATED.value,
        AuditEvent.SSO_PROVIDER_SECRET_CHANGED.value,
        AuditEvent.SSO_PROVIDER_UPDATED.value,
        AuditEvent.SSO_PROVIDER_SECRET_CHANGED.value,
        AuditEvent.SSO_PROVIDER_DELETED.value,
    ]
    assert {r.user_id for r in rows} == {admin_id}
    assert all(r.details["provider_id"] == provider_id and r.details["provider"] == "audit-crud" for r in rows)
    assert rows[1].details["fields"] == ["client_secret"]
    assert rows[2].details["fields"] == ["display_name"]
    assert rows[4].details["identities_unlinked"] == 0
    dumped = repr([r.details for r in rows]) + _all_log_text(caplog)
    assert secret not in dumped
    assert "audit-provider-secret-0002" not in dumped


# ---------------------------------------------------------------------------
# The guarantee: a full cycle leaks nothing, with and without the feature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature", [True, False], ids=["audit-on", "audit-off"])
async def test_full_cycle_never_leaks_secrets(
    client: AsyncClient,
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    sso_login: SsoLoginDriver,
    mock_idp: MockIdP,
    captured: Captured,
    caplog: pytest.LogCaptureFixture,
    feature: bool,
) -> None:
    if feature:
        get_plugin_manager().feature_registry.register("security_audit_log", "test")
    sso_provider.allow_jit_provisioning = True
    sso_provider.backchannel_logout_enabled = True
    await db_session.flush()
    caplog.set_level(logging.DEBUG)

    # JIT login, RP logout.
    cookies = await _sso_login(client, sso_login, claims={"email": "audit-cycle@example.com"})
    await _web_logout(client, cookies)
    # Login again with an IdP session, ended by the IdP over the back channel.
    await _sso_login(client, sso_login, claims={"sid": "audit-cycle-sid"})
    logout_token = mock_idp.mint_logout_token(sid="audit-cycle-sid")
    bcl = await client.post("/auth/oidc/test-idp/backchannel-logout/", data={"logout_token": logout_token})
    assert bcl.status_code == 200, bcl.text
    # Give the user a password, then unlink the identity and link it again.
    user = await db_session.scalar(select(User).where(User.email == "audit-cycle@example.com"))
    assert user is not None
    user.password_hash = UserFactory.build().password_hash
    await db_session.commit()
    browser = await _password_browser(client, user)
    identity_id = await db_session.scalar(select(UserIdentity.id).where(UserIdentity.user_id == user.id))
    unlinked = await client.post(
        f"/my/identities/{identity_id}/unlink/",
        data={"csrf_token": browser["csrf_token"]},
        headers=_cookie_header(browser),
    )
    assert unlinked.status_code == 303, unlinked.text
    linked = await _link(client, mock_idp, browser)
    assert "sso_linked=test-idp" in linked.headers["location"], linked.headers["location"]

    expected = {
        AuditEvent.SSO_USER_PROVISIONED,
        AuditEvent.SSO_LOGOUT,
        AuditEvent.SSO_BACKCHANNEL_LOGOUT,
        AuditEvent.SSO_ACCOUNT_UNLINKED,
        AuditEvent.SSO_ACCOUNT_LINKED,
    }
    assert {e.value for e in expected} <= set(_sso_events(caplog))
    rows = await _rows(db_session)
    persisted = {r.event_type for r in rows}
    # Login rows are core events; the cycle has SSO logins and one password login.
    core = {AuditEvent.SSO_LOGIN_SUCCESS.value, AuditEvent.LOGIN_SUCCESS.value}
    if feature:
        assert {e.value for e in expected} | core | {AuditEvent.SESSIONS_REVOKED.value} <= persisted
    else:
        assert persisted == core

    denylist = [
        mock_idp.client_secret or "",
        mock_idp.default_sub,
        logout_token,
        cookies["access_token"],
        cookies["refresh_token"],
        *captured.values,
    ]
    assert len(captured.values) == 3 * 4  # three code exchanges: code, verifier, ID token, access token
    texts = {
        "logs": _all_log_text(caplog),
        "audit": "\n".join(repr((r.details, r.user_agent, r.request_id)) for r in rows),
    }
    for where, text in texts.items():
        for value in denylist:
            assert value and value not in text, f"secret found in {where}"
        assert not _JWT_RE.search(text), f"JWT found in {where}"
