"""Integration tests for the admin SSO provider API (``/api/v1/admin/sso/...``).

The surface is admin-only, the client secret is write-only (never in a
response, including validation errors), and the self-test reaches the mock IdP
through the SSO HTTP client factory.

Slugs are unique database-wide and modules run in parallel inside uncommitted
transactions, so slugs here carry a module-local prefix.
"""

from __future__ import annotations

import itertools
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user_group import UserGroup
from specivo.services.sso.flow import redirect_uri_for
from specivo.testing.oidc_mock import MockIdP
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/admin/sso/providers/"
AUTH_SETTINGS_URL = "/api/v1/admin/settings/authentication/"
SECRET = "api-test-secret-value-0001"

_PREFIX = "spapi"
_counter = itertools.count(1)


def _slug() -> str:
    return f"{_PREFIX}-{next(_counter)}"


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": _slug(),
        "display_name": "Example IdP",
        "issuer": "https://idp.example.com/realms/test",
        "client_id": "specivo",
        "client_secret": SECRET,
    }
    body.update(overrides)
    return body


async def _create(admin_client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    resp = await admin_client.post(URL, json=_body(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", URL),
        ("post", URL),
        ("get", f"{URL}1/"),
        ("patch", f"{URL}1/"),
        ("delete", f"{URL}1/"),
        ("post", f"{URL}1/test/"),
        ("get", f"{URL}1/group-mappings/"),
        ("put", f"{URL}1/group-mappings/"),
        ("get", AUTH_SETTINGS_URL),
        ("patch", AUTH_SETTINGS_URL),
    ],
)
async def test_non_admin_is_forbidden(auth_client: AsyncClient, method: str, path: str):
    kwargs: dict[str, Any] = {} if method in ("get", "delete") else {"json": {}}
    resp = await getattr(auth_client, method)(path, **kwargs)
    assert resp.status_code == 403


async def _admin_api_key(admin_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    from specivo.services.api_key_service import ApiKeyService

    _, raw = await ApiKeyService().create_key(db_session, user_id=admin_client.state.user.id, name="admin-agent")  # type: ignore[attr-defined]
    await db_session.flush()
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", URL),
        ("patch", f"{URL}{{id}}/"),
        ("delete", f"{URL}{{id}}/"),
        ("put", f"{URL}{{id}}/group-mappings/"),
        ("post", f"{URL}{{id}}/group-mappings/"),
        ("delete", f"{URL}{{id}}/group-mappings/1/"),
        ("patch", AUTH_SETTINGS_URL),
    ],
)
async def test_admin_api_key_cannot_change_sso_configuration(
    admin_client: AsyncClient, db_session: AsyncSession, method: str, path: str
):
    """A stolen admin key must not be able to register or rewire an identity provider."""
    provider = await _create(admin_client)
    headers = await _admin_api_key(admin_client, db_session)
    bodies = {
        "post": _body(),
        "patch": {"display_name": "Rogue IdP", "client_secret": "rogue-secret-value-0001"},
        "put": [],
    }
    kwargs: dict[str, Any] = {"headers": headers}
    if method in bodies and path != f"{URL}{{id}}/group-mappings/":
        kwargs["json"] = bodies[method]
    elif method == "post":
        kwargs["json"] = {"idp_group": "admins", "user_group_id": 1}
    elif method == "put":
        kwargs["json"] = []
    if path == AUTH_SETTINGS_URL:
        kwargs["json"] = {"hide_local_login": True}

    resp = await getattr(admin_client, method)(path.format(id=provider["id"]), **kwargs)

    assert resp.status_code == 403, resp.text
    assert resp.json()["errors"][0]["code"] == "api_key_not_allowed"
    row = await db_session.get(SsoProvider, provider["id"])
    assert row is not None
    assert row.display_name == "Example IdP"


async def test_admin_api_key_can_still_read_sso_configuration(admin_client: AsyncClient, db_session: AsyncSession):
    provider = await _create(admin_client)
    headers = await _admin_api_key(admin_client, db_session)
    for path in (URL, f"{URL}{provider['id']}/", f"{URL}{provider['id']}/group-mappings/", AUTH_SETTINGS_URL):
        resp = await admin_client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.text)


async def test_anonymous_is_unauthorized(client: AsyncClient):
    resp = await client.get(URL)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Create / read
# ---------------------------------------------------------------------------


async def test_uris_to_register_carry_the_stealth_prefix(admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """The admin copies these into the IdP; they must be what the login flow sends."""
    data = await _create(admin_client)
    monkeypatch.setenv("STEALTH_PREFIX", "/hidden")
    get_settings.cache_clear()
    try:
        resp = await admin_client.get(f"{URL}{data['id']}/")
        assert resp.status_code == 200, resp.text
        app_url = get_settings().app_url.rstrip("/")
        assert resp.json()["redirect_uri"] == f"{app_url}/hidden/auth/oidc/{data['slug']}/callback/"
        assert resp.json()["redirect_uri"] == redirect_uri_for(data["slug"])
        assert resp.json()["backchannel_logout_uri"] == (
            f"{app_url}/hidden/auth/oidc/{data['slug']}/backchannel-logout/"
        )
    finally:
        monkeypatch.delenv("STEALTH_PREFIX")
        get_settings.cache_clear()


async def test_create_never_returns_secret(admin_client: AsyncClient, db_session: AsyncSession):
    resp = await admin_client.post(URL, json=_body())
    assert resp.status_code == 201
    assert SECRET not in resp.text
    data = resp.json()
    assert "client_secret" not in data
    assert "client_secret_encrypted" not in data
    assert data["client_secret_configured"] is True
    assert data["client_secret_status"] == "configured"
    assert data["enabled"] is False

    app_url = get_settings().app_url.rstrip("/")
    assert data["redirect_uri"] == f"{app_url}/auth/oidc/{data['slug']}/callback/"
    assert data["backchannel_logout_uri"] == f"{app_url}/auth/oidc/{data['slug']}/backchannel-logout/"

    row = (await db_session.execute(select(SsoProvider).where(SsoProvider.id == data["id"]))).scalar_one()
    assert row.client_secret_encrypted
    assert row.client_secret_encrypted != SECRET
    assert SECRET not in row.client_secret_encrypted

    for path in (URL, f"{URL}{data['id']}/"):
        resp = await admin_client.get(path)
        assert resp.status_code == 200
        assert SECRET not in resp.text
        assert row.client_secret_encrypted not in resp.text


async def test_list_providers(admin_client: AsyncClient):
    created = await _create(admin_client)
    resp = await admin_client.get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] >= 1
    assert created["id"] in [item["id"] for item in body["items"]]


async def test_env_secret_variant(admin_client: AsyncClient):
    data = await _create(admin_client, client_secret=None, client_secret_env="SPECIVO_SSO_SECRET_API_TEST")
    assert data["client_secret_env"] == "SPECIVO_SSO_SECRET_API_TEST"
    assert data["client_secret_configured"] is True
    assert data["client_secret_status"] == "env_missing"


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": "Bad Slug"},
        {"scopes": "profile email"},
        {"extra_auth_params": {"redirect_uri": "https://evil.example.com/"}},
        {"client_secret_env": "lowercase"},
        {"client_secret": None, "client_secret_env": "SECRET_KEY"},
        {"client_secret": None, "client_secret_env": "DATABASE_URL"},
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"unknown_field": True},
    ],
)
async def test_field_validation_is_422_without_secret_echo(admin_client: AsyncClient, overrides: dict[str, Any]):
    resp = await admin_client.post(URL, json=_body(**overrides))
    assert resp.status_code == 422
    assert SECRET not in resp.text


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"client_secret_env": "SPECIVO_SSO_SECRET_IDP"}, "sso_client_secret_conflict"),
        ({"token_endpoint_auth_method": "none"}, "sso_client_secret_not_allowed"),
    ],
)
async def test_cross_field_validation_is_422_without_secret_echo(
    admin_client: AsyncClient, overrides: dict[str, Any], code: str
):
    resp = await admin_client.post(URL, json=_body(**overrides))
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == code
    assert SECRET not in resp.text


async def test_duplicate_slug_is_409(admin_client: AsyncClient):
    data = await _create(admin_client)
    resp = await admin_client.post(URL, json=_body(slug=data["slug"]))
    assert resp.status_code == 409


async def test_unknown_provider_is_404(admin_client: AsyncClient):
    resp = await admin_client.get(f"{URL}987654321/")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_patch_without_secret_keeps_it(admin_client: AsyncClient, db_session: AsyncSession):
    data = await _create(admin_client)
    row = (await db_session.execute(select(SsoProvider).where(SsoProvider.id == data["id"]))).scalar_one()
    before = row.client_secret_encrypted

    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"display_name": "Renamed", "enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["enabled"] is True
    await db_session.refresh(row)
    assert row.client_secret_encrypted == before


async def test_deactivate_on_access_loss_defaults_off_and_round_trips(
    admin_client: AsyncClient, db_session: AsyncSession
):
    data = await _create(admin_client)
    assert data["deactivate_on_access_loss"] is False

    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"deactivate_on_access_loss": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deactivate_on_access_loss"] is True
    row = (await db_session.execute(select(SsoProvider).where(SsoProvider.id == data["id"]))).scalar_one()
    await db_session.refresh(row)
    assert row.deactivate_on_access_loss is True

    created = await _create(admin_client, deactivate_on_access_loss=True)
    assert created["deactivate_on_access_loss"] is True


async def test_patch_secret_never_echoed(admin_client: AsyncClient):
    data = await _create(admin_client)
    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"client_secret": "brand-new-secret-value"})
    assert resp.status_code == 200
    assert "brand-new-secret-value" not in resp.text
    assert resp.json()["client_secret_configured"] is True


async def test_patch_clear_secret(admin_client: AsyncClient):
    data = await _create(admin_client)
    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"clear_client_secret": True})
    assert resp.status_code == 200
    assert resp.json()["client_secret_configured"] is False
    assert resp.json()["client_secret_status"] == "missing"


async def test_patch_disable_reports_revoked_sessions(admin_client: AsyncClient, db_session: AsyncSession):
    data = await _create(admin_client, enabled=True)
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    identity = UserIdentity(
        user_id=user.id, provider_id=data["id"], issuer=data["issuer"], subject="sub-disable", linked_via="admin"
    )
    db_session.add(identity)
    await db_session.flush()
    db_session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=secrets.token_hex(32),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            auth_method="sso",
            user_identity_id=identity.id,
        )
    )
    await db_session.flush()

    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"display_name": "Still on"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Revoked-Sessions"] == "0"

    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert resp.headers["X-Revoked-Sessions"] == "1"
    left = await db_session.execute(select(RefreshToken.id).where(RefreshToken.user_id == user.id))
    assert left.first() is None


async def test_patch_rejects_session_length_over_a_week(admin_client: AsyncClient):
    data = await _create(admin_client)
    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"session_max_age_hours": 169})
    assert resp.status_code == 422
    resp = await admin_client.patch(f"{URL}{data['id']}/", json={"session_max_age_hours": 168})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_with_identities_is_409_then_force(admin_client: AsyncClient, db_session: AsyncSession):
    data = await _create(admin_client)
    user = UserFactory.build()
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=user.id, provider_id=data["id"], issuer=data["issuer"], subject="sub-1", linked_via="admin"
        )
    )
    await db_session.flush()

    resp = await admin_client.delete(f"{URL}{data['id']}/")
    assert resp.status_code == 409
    assert resp.json()["errors"][0]["code"] == "sso_provider_has_identities"

    resp = await admin_client.delete(f"{URL}{data['id']}/?force=true")
    assert resp.status_code == 204
    assert resp.headers["X-Unlinked-Identities"] == "1"
    assert resp.headers["X-Revoked-Sessions"] == "0"
    assert resp.headers["X-Removed-Memberships"] == "0"
    gone = await db_session.execute(select(SsoProvider).where(SsoProvider.id == data["id"]))
    assert gone.scalar_one_or_none() is None


async def test_delete_plain(admin_client: AsyncClient):
    data = await _create(admin_client)
    resp = await admin_client.delete(f"{URL}{data['id']}/")
    assert resp.status_code == 204
    assert (await admin_client.get(f"{URL}{data['id']}/")).status_code == 404


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


async def test_self_test_endpoint(admin_client: AsyncClient, mock_idp: MockIdP):
    data = await _create(admin_client, issuer=mock_idp.issuer, client_secret=mock_idp.client_secret)
    resp = await admin_client.post(f"{URL}{data['id']}/test/")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["ok"] is True
    assert result["issuer_match"] is True
    assert result["jwks_key_count"] == 2
    assert mock_idp.client_secret not in resp.text


async def test_self_test_endpoint_issuer_mismatch(admin_client: AsyncClient, mock_idp: MockIdP):
    data = await _create(admin_client, issuer=mock_idp.issuer, client_secret=mock_idp.client_secret)
    mock_idp.discovery_overrides["issuer"] = "https://other.example.com/realms/test"
    resp = await admin_client.post(f"{URL}{data['id']}/test/")
    assert resp.status_code == 200
    result = resp.json()
    assert result["ok"] is False
    assert result["issuer_match"] is False
    assert result["error_reason"] == "issuer_mismatch"


async def test_self_test_endpoint_unreachable(admin_client: AsyncClient, mock_idp: MockIdP):
    data = await _create(admin_client, issuer=mock_idp.issuer, client_secret=mock_idp.client_secret)
    mock_idp.set_fault("discovery", status=503)
    resp = await admin_client.post(f"{URL}{data['id']}/test/")
    assert resp.status_code == 200
    assert resp.json()["error_code"] == "sso_idp_unreachable"


# ---------------------------------------------------------------------------
# Group mappings
# ---------------------------------------------------------------------------


async def test_group_mappings_put_get_post_delete(admin_client: AsyncClient, db_session: AsyncSession):
    data = await _create(admin_client)
    group = UserGroup(name=f"{_PREFIX}-group-{next(_counter)}")
    db_session.add(group)
    await db_session.flush()
    base = f"{URL}{data['id']}/group-mappings/"

    resp = await admin_client.put(base, json=[{"idp_group": "staff", "user_group_id": group.id}])
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert [(i["idp_group"], i["user_group_id"], i["user_group_name"]) for i in items] == [
        ("staff", group.id, group.name)
    ]

    resp = await admin_client.post(base, json={"idp_group": "ops", "user_group_id": group.id})
    assert resp.status_code == 201
    mapping_id = resp.json()["id"]

    resp = await admin_client.get(base)
    assert len(resp.json()) == 2

    resp = await admin_client.delete(f"{base}{mapping_id}/")
    assert resp.status_code == 204
    assert len((await admin_client.get(base)).json()) == 1

    resp = await admin_client.put(base, json=[{"idp_group": "x", "user_group_id": 987654321}])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Authentication settings
# ---------------------------------------------------------------------------


async def test_auth_settings_round_trip(admin_client: AsyncClient):
    resp = await admin_client.get(AUTH_SETTINGS_URL)
    assert resp.status_code == 200
    assert resp.json() == {
        "hide_local_login": False,
        "local_login_admins_only": False,
        "auto_redirect_provider": None,
        "allow_api_keys_for_sso_users": True,
    }

    data = await _create(admin_client)
    resp = await admin_client.patch(
        AUTH_SETTINGS_URL,
        json={"hide_local_login": True, "auto_redirect_provider": data["slug"], "allow_api_keys_for_sso_users": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "hide_local_login": True,
        "local_login_admins_only": False,
        "auto_redirect_provider": data["slug"],
        "allow_api_keys_for_sso_users": False,
    }
    assert (await admin_client.get(AUTH_SETTINGS_URL)).json()["hide_local_login"] is True

    resp = await admin_client.patch(AUTH_SETTINGS_URL, json={"auto_redirect_provider": "no-such-idp"})
    assert resp.status_code == 422


async def test_generic_settings_patch_refuses_sso_keys(admin_client: AsyncClient):
    resp = await admin_client.patch("/api/v1/admin/settings/", json={"sso.hide_local_login": "true"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "setting_managed_elsewhere"
