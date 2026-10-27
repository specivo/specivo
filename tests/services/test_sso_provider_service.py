"""Service tests for SSO provider configuration (``specivo.services.sso.provider_service``).

Covers validation (field rules in the schemas, cross-field rules in the
service), the write-only client secret (encrypted at rest, env-var
alternative, "unreadable" state after a ``SECRET_KEY`` rotation), cache
invalidation on change, the delete rules around linked identities, group
mappings, the global authentication settings and the discovery self-test
against the mock IdP.

Provider slugs are unique database-wide and modules run in parallel inside
uncommitted transactions, so slugs here carry a module-local prefix.
"""

from __future__ import annotations

import itertools
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.auth import RefreshToken
from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.schemas.sso import (
    AuthSettingsUpdate,
    SsoGroupMappingIn,
    SsoProviderCreate,
    SsoProviderUpdate,
)
from specivo.services.settings_service import SettingsService
from specivo.services.sso import provider_service as provider_module
from specivo.services.sso.auth_settings import (
    AUTO_REDIRECT_PROVIDER_KEY,
    get_auth_settings,
    update_auth_settings,
)
from specivo.services.sso.provider_service import (
    CLIENT_SECRET_PURPOSE,
    ProviderService,
    SecretStatus,
)
from specivo.testing.oidc_mock import MockIdP
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.service]

_PREFIX = "spsvc"
_counter = itertools.count(1)
_service = ProviderService()

SECRET = "s3cret-value-for-tests-0001"


def _slug(stem: str = "idp") -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


def _create(**overrides: Any) -> SsoProviderCreate:
    values: dict[str, Any] = {
        "slug": _slug(),
        "display_name": "Example IdP",
        "issuer": "https://idp.example.com/realms/test",
        "client_id": "specivo",
    }
    values.update(overrides)
    return SsoProviderCreate(**values)


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = UserFactory.build(is_admin=True)
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture(autouse=True)
def _record_invalidations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the Redis cache invalidation with a recorder (no Redis needed)."""
    calls: list[int] = []

    async def _fake(provider_id: int, **_: Any) -> None:
        calls.append(provider_id)

    monkeypatch.setattr(provider_module, "invalidate_provider_cache", _fake)
    return calls


@pytest.fixture
def invalidations(_record_invalidations: list[int]) -> list[int]:
    return _record_invalidations


@pytest.fixture
def insecure_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SSO_ALLOW_INSECURE_HTTP", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Field validation (schemas)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["A", "x", "Has-Upper", "with space", "under_score", "a" * 41])
def test_bad_slug_rejected(slug: str):
    with pytest.raises(PydanticValidationError):
        _create(slug=slug)


@pytest.mark.parametrize("scopes", ["profile email", "", "   ", "openidx profile"])
def test_scopes_must_include_openid(scopes: str):
    with pytest.raises(PydanticValidationError):
        _create(scopes=scopes)


def test_scopes_are_normalised():
    data = _create(scopes="  email openid   profile email ")
    assert data.scopes == "openid email profile"


def test_extra_auth_params_allowlist():
    ok = _create(extra_auth_params={"prompt": "login", "max_age": 3600, "domain_hint": "example.com"})
    assert ok.extra_auth_params == {"prompt": "login", "max_age": "3600", "domain_hint": "example.com"}
    for key in ("redirect_uri", "client_id", "state", "nonce", "scope", "response_type", "code_challenge"):
        with pytest.raises(PydanticValidationError):
            _create(extra_auth_params={key: "x"})


@pytest.mark.parametrize(
    "name",
    [
        "lower_case",
        "1STARTS_WITH_DIGIT",
        "AB",
        "HAS-DASH",
        "SPECIVO_SSO_SECRET_" + "A" * 46,
        "specivo_sso_secret_x",
        "SPECIVO_SSO_SECRET_",
        "SPECIVO_SSO_SECRET_lower",
        # Outside the namespace: these hold Specivo's own secrets.
        "SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
        "IDP_SECRET",
        "XSPECIVO_SSO_SECRET_A",
    ],
)
def test_bad_client_secret_env_name_rejected(name: str):
    with pytest.raises(PydanticValidationError):
        _create(client_secret_env=name)
    with pytest.raises(PydanticValidationError):
        SsoProviderUpdate(client_secret_env=name)


def test_client_secret_env_name_in_namespace_accepted():
    assert _create(client_secret_env="SPECIVO_SSO_SECRET_KEYCLOAK").client_secret_env == "SPECIVO_SSO_SECRET_KEYCLOAK"


@pytest.mark.parametrize("name", ["SECRET_KEY", "DATABASE_URL"])
async def test_forged_env_name_is_never_read(
    db_session: AsyncSession, admin: User, monkeypatch: pytest.MonkeyPatch, name: str
):
    """A row written around the API (or before the rule) must not expose a master secret."""
    provider = await _service.create(db_session, _create(), actor=admin)
    provider.client_secret_env = name
    monkeypatch.setenv(name, "master-secret-value")

    # Same answer whether or not the variable exists: nothing is revealed.
    assert _service.secret_status(provider) is SecretStatus.ENV_NOT_ALLOWED
    monkeypatch.delenv(name)
    assert _service.secret_status(provider) is SecretStatus.ENV_NOT_ALLOWED
    monkeypatch.setenv(name, "master-secret-value")
    assert _service.get_client_secret(provider) is None
    assert _service.to_out(provider).client_secret_status == "env_not_allowed"


@pytest.mark.parametrize("issuer", ["not a url", "ftp://idp.example.com", "https://idp.example.com/#frag", "/relative"])
def test_bad_issuer_rejected(issuer: str):
    with pytest.raises(PydanticValidationError):
        _create(issuer=issuer)


def test_secret_is_hidden_in_schema_repr():
    data = _create(client_secret=SECRET)
    assert SECRET not in repr(data)
    assert SECRET not in str(data.model_dump())


def test_group_lists_and_domains_are_normalised():
    data = _create(
        allowed_email_domains=[" Example.COM ", "example.com", "sub.example.org"],
        required_groups=["staff", " staff ", "admins"],
    )
    assert data.allowed_email_domains == ["example.com", "sub.example.org"]
    assert data.required_groups == ["staff", "admins"]
    with pytest.raises(PydanticValidationError):
        _create(allowed_email_domains=["not a domain"])
    with pytest.raises(PydanticValidationError):
        _create(required_groups=["   "])


# ---------------------------------------------------------------------------
# Create: secrets and cross-field rules
# ---------------------------------------------------------------------------


async def test_create_encrypts_secret_and_reports_configured(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(client_secret=SECRET), actor=admin)

    assert provider.enabled is False
    assert provider.client_secret_encrypted
    assert SECRET not in provider.client_secret_encrypted
    assert secret_box.decrypt(provider.client_secret_encrypted, CLIENT_SECRET_PURPOSE) == SECRET
    assert _service.secret_status(provider) is SecretStatus.CONFIGURED
    assert _service.get_client_secret(provider) == SECRET


async def test_create_with_env_secret(db_session: AsyncSession, admin: User, monkeypatch: pytest.MonkeyPatch):
    provider = await _service.create(db_session, _create(client_secret_env="SPECIVO_SSO_SECRET_TEST_IDP"), actor=admin)
    assert provider.client_secret_encrypted is None
    assert provider.client_secret_env == "SPECIVO_SSO_SECRET_TEST_IDP"

    monkeypatch.delenv("SPECIVO_SSO_SECRET_TEST_IDP", raising=False)
    assert _service.secret_status(provider) is SecretStatus.ENV_MISSING
    assert _service.get_client_secret(provider) is None

    monkeypatch.setenv("SPECIVO_SSO_SECRET_TEST_IDP", "from-the-environment")
    assert _service.secret_status(provider) is SecretStatus.ENV
    assert _service.get_client_secret(provider) == "from-the-environment"


async def test_create_rejects_secret_and_env_together(db_session: AsyncSession, admin: User):
    with pytest.raises(AppError) as exc:
        await _service.create(
            db_session, _create(client_secret=SECRET, client_secret_env="SPECIVO_SSO_SECRET_IDP"), actor=admin
        )
    assert exc.value.status_code == 422
    assert exc.value.code == "sso_client_secret_conflict"
    assert SECRET not in exc.value.message


@pytest.mark.parametrize("secret_kwargs", [{"client_secret": SECRET}, {"client_secret_env": "SPECIVO_SSO_SECRET_IDP"}])
async def test_public_client_rejects_any_secret(db_session: AsyncSession, admin: User, secret_kwargs: dict[str, str]):
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _create(token_endpoint_auth_method="none", **secret_kwargs), actor=admin)
    assert exc.value.status_code == 422
    assert exc.value.code == "sso_client_secret_not_allowed"


async def test_public_client_without_secret(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(token_endpoint_auth_method="none", enabled=True), actor=admin)
    assert provider.enabled is True
    assert _service.secret_status(provider) is SecretStatus.NOT_REQUIRED
    assert _service.get_client_secret(provider) is None


async def test_enabling_confidential_client_without_secret_is_refused(db_session: AsyncSession, admin: User):
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _create(enabled=True), actor=admin)
    assert exc.value.code == "sso_client_secret_required"
    # Disabled, it can be saved while the admin is still setting it up.
    provider = await _service.create(db_session, _create(enabled=False), actor=admin)
    assert _service.secret_status(provider) is SecretStatus.MISSING


async def test_auto_verified_email_requires_trust(db_session: AsyncSession, admin: User):
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _create(link_policy="auto_verified_email"), actor=admin)
    assert exc.value.code == "sso_trust_email_verified_required"
    provider = await _service.create(
        db_session, _create(link_policy="auto_verified_email", trust_email_verified=True), actor=admin
    )
    assert provider.link_policy == "auto_verified_email"


async def test_http_issuer_needs_insecure_setting(db_session: AsyncSession, admin: User):
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _create(issuer="http://idp.example.com"), actor=admin)
    assert exc.value.code == "sso_insecure_url"
    assert exc.value.field == "issuer"
    with pytest.raises(AppError):
        await _service.create(
            db_session, _create(discovery_url="http://idp.example.com/.well-known/openid-configuration"), actor=admin
        )


async def test_http_issuer_allowed_with_insecure_setting(db_session: AsyncSession, admin: User, insecure_http: None):
    provider = await _service.create(db_session, _create(issuer="http://idp.example.com"), actor=admin)
    assert provider.issuer == "http://idp.example.com"


async def test_duplicate_slug_conflicts(db_session: AsyncSession, admin: User):
    slug = _slug("dup")
    await _service.create(db_session, _create(slug=slug), actor=admin)
    with pytest.raises(AppError) as exc:
        await _service.create(db_session, _create(slug=slug), actor=admin)
    assert exc.value.status_code == 409
    assert exc.value.code == "sso_provider_slug_taken"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_update_without_secret_keeps_it(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(client_secret=SECRET), actor=admin)
    before = provider.client_secret_encrypted

    updated = (
        await _service.update(db_session, provider.id, SsoProviderUpdate(display_name="Renamed"), actor=admin)
    ).provider
    assert updated.display_name == "Renamed"
    assert updated.client_secret_encrypted == before


async def test_update_replaces_and_clears_secret(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(client_secret=SECRET), actor=admin)

    await _service.update(db_session, provider.id, SsoProviderUpdate(client_secret="another-secret"), actor=admin)
    assert _service.get_client_secret(provider) == "another-secret"

    # Switching to an env var replaces the stored secret.
    await _service.update(
        db_session, provider.id, SsoProviderUpdate(client_secret_env="SPECIVO_SSO_SECRET_IDP"), actor=admin
    )
    assert provider.client_secret_encrypted is None
    assert provider.client_secret_env == "SPECIVO_SSO_SECRET_IDP"

    # And back: a stored secret replaces the env reference.
    await _service.update(db_session, provider.id, SsoProviderUpdate(client_secret=SECRET), actor=admin)
    assert provider.client_secret_env is None

    await _service.update(db_session, provider.id, SsoProviderUpdate(clear_client_secret=True), actor=admin)
    assert provider.client_secret_encrypted is None
    assert provider.client_secret_env is None


async def test_update_to_public_client_needs_secret_cleared(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(client_secret=SECRET), actor=admin)
    with pytest.raises(AppError) as exc:
        await _service.update(
            db_session, provider.id, SsoProviderUpdate(token_endpoint_auth_method="none"), actor=admin
        )
    assert exc.value.code == "sso_client_secret_not_allowed"

    await _service.update(
        db_session,
        provider.id,
        SsoProviderUpdate(token_endpoint_auth_method="none", clear_client_secret=True),
        actor=admin,
    )
    assert provider.token_endpoint_auth_method == "none"
    assert provider.client_secret_encrypted is None


async def test_update_rejects_secret_with_clear(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(client_secret=SECRET), actor=admin)
    with pytest.raises(AppError) as exc:
        await _service.update(
            db_session, provider.id, SsoProviderUpdate(client_secret=SECRET, clear_client_secret=True), actor=admin
        )
    assert exc.value.code == "sso_client_secret_conflict"


async def test_update_unknown_provider_is_404(db_session: AsyncSession, admin: User):
    with pytest.raises(NotFoundError):
        await _service.update(db_session, 987_654_321, SsoProviderUpdate(display_name="x"), actor=admin)


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other.example.com/realms/test"},
        {"client_id": "another-client"},
        {"discovery_url": "https://idp.example.com/custom/openid-configuration"},
    ],
)
async def test_connection_change_invalidates_cache(
    db_session: AsyncSession, admin: User, invalidations: list[int], change: dict[str, str]
):
    provider = await _service.create(db_session, _create(), actor=admin)
    provider.discovery_cache = {"issuer": provider.issuer}
    invalidations.clear()

    await _service.update(db_session, provider.id, SsoProviderUpdate(**change), actor=admin)

    assert invalidations == [provider.id]
    if "client_id" not in change:
        # A stored "last known good" document belongs to the old endpoint.
        assert provider.discovery_cache is None
        assert provider.discovery_fetched_at is None


async def test_unreadable_secret_after_key_rotation(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    provider.client_secret_encrypted = secret_box.encrypt(
        SECRET, CLIENT_SECRET_PURPOSE, secret_key="a-different-secret-key-of-at-least-32-bytes"
    )
    assert _service.secret_status(provider) is SecretStatus.UNREADABLE
    with pytest.raises(secret_box.SecretUnreadableError):
        _service.get_client_secret(provider)


async def test_slug_change_follows_auto_redirect_setting(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(token_endpoint_auth_method="none"), actor=admin)
    await update_auth_settings(db_session, AuthSettingsUpdate(auto_redirect_provider=provider.slug), actor=admin)

    new_slug = _slug("renamed")
    await _service.update(db_session, provider.id, SsoProviderUpdate(slug=new_slug), actor=admin)
    assert (await get_auth_settings(db_session)).auto_redirect_provider == new_slug


# ---------------------------------------------------------------------------
# Events (log lines now, audit events later)
# ---------------------------------------------------------------------------


async def test_events_name_fields_but_never_secret(
    db_session: AsyncSession, admin: User, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO, logger="specivo.sso")
    provider = await _service.create(db_session, _create(client_secret=SECRET), actor=admin)
    await _service.update(
        db_session, provider.id, SsoProviderUpdate(display_name="New name", client_secret="rotated-secret"), actor=admin
    )
    await _service.delete(db_session, provider.id, actor=admin)

    messages = [r.getMessage() for r in caplog.records if r.name == "specivo.sso"]
    joined = "\n".join(messages)
    assert "sso.provider.created" in joined
    assert "sso.provider.secret_changed" in joined
    assert "sso.provider.updated" in joined
    assert "display_name" in joined
    assert "sso.provider.deleted" in joined
    assert SECRET not in joined
    assert "rotated-secret" not in joined
    assert provider.client_secret_encrypted not in joined


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def _link_identity(db: AsyncSession, provider: SsoProvider) -> User:
    user = UserFactory.build()
    db.add(user)
    await db.flush()
    db.add(
        UserIdentity(
            user_id=user.id,
            provider_id=provider.id,
            issuer=provider.issuer,
            subject=f"sub-{user.id}",
            linked_via="admin",
        )
    )
    await db.flush()
    return user


async def test_delete_without_identities(db_session: AsyncSession, admin: User, invalidations: list[int]):
    provider = await _service.create(db_session, _create(), actor=admin)
    pid = provider.id
    result = await _service.delete(db_session, pid, actor=admin)
    assert result.identities_unlinked == 0
    assert (await db_session.execute(select(SsoProvider).where(SsoProvider.id == pid))).scalar_one_or_none() is None
    assert pid in invalidations


async def test_delete_with_identities_is_refused(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    await _link_identity(db_session, provider)

    with pytest.raises(AppError) as exc:
        await _service.delete(db_session, provider.id, actor=admin)
    assert exc.value.status_code == 409
    assert exc.value.code == "sso_provider_has_identities"
    assert exc.value.details == {"identity_count": 1}


async def _refresh_row(db: AsyncSession, user: User, identity_id: int | None) -> RefreshToken:
    row = RefreshToken(
        user_id=user.id,
        token_hash=secrets.token_hex(32),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        auth_method="sso" if identity_id else "password",
        user_identity_id=identity_id,
    )
    db.add(row)
    await db.flush()
    return row


async def _identity_id(db: AsyncSession, provider: SsoProvider, user: User) -> int:
    result = await db.execute(
        select(UserIdentity.id).where(UserIdentity.provider_id == provider.id, UserIdentity.user_id == user.id)
    )
    return result.scalar_one()


async def _token_exists(db: AsyncSession, token_id: int) -> bool:
    found = await db.execute(select(RefreshToken.id).where(RefreshToken.id == token_id))
    return found.scalar_one_or_none() is not None


async def test_force_delete_revokes_only_this_providers_sessions(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    other = await _service.create(db_session, _create(), actor=admin)
    first = await _link_identity(db_session, provider)
    second = await _link_identity(db_session, provider)
    first_sso = await _refresh_row(db_session, first, await _identity_id(db_session, provider, first))
    second_sso = await _refresh_row(db_session, second, await _identity_id(db_session, provider, second))
    first_password = await _refresh_row(db_session, first, None)
    # The same user also signs in through another provider: that session stays.
    db_session.add(
        UserIdentity(
            user_id=first.id, provider_id=other.id, issuer=other.issuer, subject="other-sub", linked_via="admin"
        )
    )
    await db_session.flush()
    first_other = await _refresh_row(db_session, first, await _identity_id(db_session, other, first))

    result = await _service.delete(db_session, provider.id, force=True, actor=admin)

    assert result.identities_unlinked == 2
    assert result.sessions_revoked == 2
    assert not await _token_exists(db_session, first_sso.id)
    assert not await _token_exists(db_session, second_sso.id)
    assert await _token_exists(db_session, first_password.id)
    assert await _token_exists(db_session, first_other.id)
    remaining = await db_session.execute(
        select(func.count()).select_from(UserIdentity).where(UserIdentity.provider_id == provider.id)
    )
    assert remaining.scalar_one() == 0


async def test_force_delete_removes_managed_memberships(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    other = await _service.create(db_session, _create(), actor=admin)
    user = await _link_identity(db_session, provider)
    group = await _user_group(db_session)
    other_group = await _user_group(db_session)
    hand_group = await _user_group(db_session)
    db_session.add_all(
        [
            UserGroupMember(group_id=group.id, user_id=user.id, managed_by_provider_id=provider.id),
            UserGroupMember(group_id=other_group.id, user_id=user.id, managed_by_provider_id=other.id),
            UserGroupMember(group_id=hand_group.id, user_id=user.id),
        ]
    )
    await db_session.flush()

    result = await _service.delete(db_session, provider.id, force=True, actor=admin)

    assert result.memberships_removed == 1
    rows = await db_session.execute(select(UserGroupMember.group_id).where(UserGroupMember.user_id == user.id))
    assert sorted(rows.scalars()) == sorted([other_group.id, hand_group.id])


async def test_refused_delete_changes_nothing(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    user = await _link_identity(db_session, provider)
    group = await _user_group(db_session)
    db_session.add(UserGroupMember(group_id=group.id, user_id=user.id, managed_by_provider_id=provider.id))
    token = await _refresh_row(db_session, user, await _identity_id(db_session, provider, user))

    with pytest.raises(AppError):
        await _service.delete(db_session, provider.id, actor=admin)

    assert await _token_exists(db_session, token.id)
    kept = await db_session.execute(select(UserGroupMember.id).where(UserGroupMember.group_id == group.id))
    assert kept.scalar_one_or_none() is not None


async def test_delete_clears_auto_redirect(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(token_endpoint_auth_method="none"), actor=admin)
    await update_auth_settings(db_session, AuthSettingsUpdate(auto_redirect_provider=provider.slug), actor=admin)
    await _service.delete(db_session, provider.id, actor=admin)
    assert (await get_auth_settings(db_session)).auto_redirect_provider is None


# ---------------------------------------------------------------------------
# Group mappings
# ---------------------------------------------------------------------------


async def _user_group(db: AsyncSession) -> UserGroup:
    group = UserGroup(name=f"{_PREFIX}-group-{next(_counter)}")
    db.add(group)
    await db.flush()
    return group


async def test_group_mapping_crud(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    g1 = await _user_group(db_session)
    g2 = await _user_group(db_session)

    rows = await _service.replace_group_mappings(
        db_session,
        provider.id,
        [
            SsoGroupMappingIn(idp_group="engineering", user_group_id=g1.id),
            SsoGroupMappingIn(idp_group=" engineering ", user_group_id=g1.id),  # duplicate after strip
            SsoGroupMappingIn(idp_group="Engineering", user_group_id=g2.id),  # case-sensitive: distinct
        ],
        actor=admin,
    )
    assert [(r.idp_group, r.user_group_id) for r in rows] == [("Engineering", g2.id), ("engineering", g1.id)]

    added = await _service.add_group_mapping(
        db_session, provider.id, SsoGroupMappingIn(idp_group="ops", user_group_id=g2.id), actor=admin
    )
    assert len(await _service.list_group_mappings(db_session, provider.id)) == 3

    with pytest.raises(AppError) as exc:
        await _service.add_group_mapping(
            db_session, provider.id, SsoGroupMappingIn(idp_group="ops", user_group_id=g2.id), actor=admin
        )
    assert exc.value.status_code == 409

    await _service.delete_group_mapping(db_session, provider.id, added.id, actor=admin)
    assert len(await _service.list_group_mappings(db_session, provider.id)) == 2

    await _service.replace_group_mappings(db_session, provider.id, [], actor=admin)
    assert await _service.list_group_mappings(db_session, provider.id) == []


async def test_group_mapping_unknown_user_group(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(), actor=admin)
    with pytest.raises(AppError) as exc:
        await _service.replace_group_mappings(
            db_session, provider.id, [SsoGroupMappingIn(idp_group="x", user_group_id=987_654_321)], actor=admin
        )
    assert exc.value.status_code == 422
    assert exc.value.code == "sso_user_group_not_found"


async def test_group_mapping_delete_wrong_provider_is_404(db_session: AsyncSession, admin: User):
    p1 = await _service.create(db_session, _create(), actor=admin)
    p2 = await _service.create(db_session, _create(), actor=admin)
    group = await _user_group(db_session)
    mapping = await _service.add_group_mapping(
        db_session, p1.id, SsoGroupMappingIn(idp_group="x", user_group_id=group.id), actor=admin
    )
    with pytest.raises(NotFoundError):
        await _service.delete_group_mapping(db_session, p2.id, mapping.id, actor=admin)
    count = await db_session.execute(select(func.count()).select_from(SsoGroupMapping))
    assert count.scalar_one() >= 1


def test_group_mapping_rejects_blank_group():
    with pytest.raises(PydanticValidationError):
        SsoGroupMappingIn(idp_group="   ", user_group_id=1)


# ---------------------------------------------------------------------------
# Global authentication settings
# ---------------------------------------------------------------------------


async def test_auth_settings_defaults(db_session: AsyncSession):
    current = await get_auth_settings(db_session)
    assert current.hide_local_login is False
    assert current.local_login_admins_only is False
    assert current.auto_redirect_provider is None
    assert current.allow_api_keys_for_sso_users is True


async def test_auth_settings_round_trip(db_session: AsyncSession, admin: User):
    provider = await _service.create(db_session, _create(token_endpoint_auth_method="none"), actor=admin)
    await update_auth_settings(
        db_session,
        AuthSettingsUpdate(
            hide_local_login=True,
            local_login_admins_only=True,
            auto_redirect_provider=provider.slug,
            allow_api_keys_for_sso_users=False,
        ),
        actor=admin,
    )
    current = await get_auth_settings(db_session)
    assert current.hide_local_login is True
    assert current.local_login_admins_only is True
    assert current.auto_redirect_provider == provider.slug
    assert current.allow_api_keys_for_sso_users is False

    # Partial update leaves the rest alone; null clears the auto-redirect.
    await update_auth_settings(db_session, AuthSettingsUpdate(auto_redirect_provider=None), actor=admin)
    current = await get_auth_settings(db_session)
    assert current.auto_redirect_provider is None
    assert current.hide_local_login is True

    stored = await SettingsService().get(db_session, AUTO_REDIRECT_PROVIDER_KEY)
    assert stored is None


async def test_auth_settings_unknown_provider(db_session: AsyncSession, admin: User):
    with pytest.raises(AppError) as exc:
        await update_auth_settings(db_session, AuthSettingsUpdate(auto_redirect_provider="no-such-idp"), actor=admin)
    assert exc.value.status_code == 422
    assert exc.value.field == "auto_redirect_provider"


# ---------------------------------------------------------------------------
# Discovery self-test (against the mock IdP)
# ---------------------------------------------------------------------------


async def _mock_provider(db: AsyncSession, idp: MockIdP, admin: User, **overrides: Any) -> SsoProvider:
    data = _create(issuer=idp.issuer, client_id=idp.client_id, client_secret=idp.client_secret, **overrides)
    return await _service.create(db, data, actor=admin)


async def test_self_test_success(db_session: AsyncSession, admin: User, mock_idp: MockIdP):
    provider = await _mock_provider(db_session, mock_idp, admin, backchannel_logout_enabled=True)

    result = await _service.self_test(provider)

    assert result.ok is True
    assert result.failed_step is None
    assert result.issuer_match is True
    assert result.reported_issuer == mock_idp.issuer
    assert result.token_endpoint == mock_idp.token_endpoint
    assert result.end_session_endpoint == mock_idp.end_session_endpoint
    assert set(result.usable_signing_algorithms) == {"RS256", "ES256"}
    assert result.pkce_s256 is True
    assert result.backchannel_logout_supported is True
    assert result.jwks_key_count == 2
    assert sorted(result.jwks_key_ids) == ["ec-1", "rsa-1"]
    assert result.warnings == []
    # The self-test never stores anything.
    assert provider.discovery_cache is None
    assert provider.discovery_fetched_at is None


async def test_self_test_reports_issuer_mismatch(db_session: AsyncSession, admin: User, mock_idp: MockIdP):
    provider = await _mock_provider(db_session, mock_idp, admin)
    mock_idp.discovery_overrides["issuer"] = "https://evil.example.com/realms/test"

    result = await _service.self_test(provider)

    assert result.ok is False
    assert result.failed_step == "discovery"
    assert result.error_code == "sso_discovery_invalid"
    assert result.error_reason == "issuer_mismatch"
    assert result.issuer_match is False
    assert result.reported_issuer == "https://evil.example.com/realms/test"
    assert mock_idp.calls["jwks"] == 0
    assert provider.discovery_cache is None


async def test_self_test_reports_unreachable(db_session: AsyncSession, admin: User, mock_idp: MockIdP):
    provider = await _mock_provider(db_session, mock_idp, admin)
    provider.discovery_cache = None
    mock_idp.set_fault("discovery", timeout=True)

    result = await _service.self_test(provider)

    assert result.ok is False
    assert result.failed_step == "discovery"
    assert result.error_code == "sso_idp_unreachable"
    assert result.error_reason == "timeout"


async def test_self_test_ignores_last_known_good(db_session: AsyncSession, admin: User, mock_idp: MockIdP):
    """A cached document must not make a broken IdP look healthy."""
    provider = await _mock_provider(db_session, mock_idp, admin)
    provider.discovery_cache = mock_idp.discovery_document()
    mock_idp.set_fault("discovery", status=503)

    result = await _service.self_test(provider)
    assert result.ok is False
    assert result.error_code == "sso_idp_unreachable"


async def test_self_test_reports_jwks_failure(db_session: AsyncSession, admin: User, mock_idp: MockIdP):
    provider = await _mock_provider(db_session, mock_idp, admin)
    mock_idp.set_fault("jwks", status=500)

    result = await _service.self_test(provider)

    assert result.ok is False
    assert result.failed_step == "jwks"
    assert result.issuer_match is True
    assert result.token_endpoint == mock_idp.token_endpoint


async def test_self_test_warnings(
    db_session: AsyncSession, admin: User, mock_idp: MockIdP, monkeypatch: pytest.MonkeyPatch
):
    provider = await _mock_provider(db_session, mock_idp, admin, backchannel_logout_enabled=True)
    mock_idp.discovery_overrides.update(
        {
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "backchannel_logout_supported": False,
        }
    )
    from specivo.testing.oidc_mock import OMIT

    mock_idp.discovery_overrides["end_session_endpoint"] = OMIT
    provider.client_secret_encrypted = secret_box.encrypt(
        SECRET, CLIENT_SECRET_PURPOSE, secret_key="a-different-secret-key-of-at-least-32-bytes"
    )

    result = await _service.self_test(provider)

    assert result.ok is True
    assert "auth_method_not_advertised" in result.warnings
    assert "backchannel_logout_unsupported" in result.warnings
    assert "end_session_missing" in result.warnings
    assert "client_secret_unreadable" in result.warnings
