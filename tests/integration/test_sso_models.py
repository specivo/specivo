"""SSO data model: providers, external identities and group mappings.

Covers what the database itself enforces: identity uniqueness, the foreign-key
delete rules, the provider CHECK constraints and the trigger that keeps
service accounts and the anonymous user from ever holding an external
identity. Every test runs inside the rolled-back test transaction.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.anonymous_user_service import clear_anonymous_user_cache, get_anonymous_user
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_ISSUER = "https://idp.example.com/realms/test"


def _provider(slug: str = "test-idp", **overrides: object) -> SsoProvider:
    values: dict[str, object] = {
        "slug": slug,
        "display_name": "Test IdP",
        "issuer": _ISSUER,
        "client_id": "specivo-client",
    }
    values.update(overrides)
    return SsoProvider(**values)


def _identity(user: User, provider: SsoProvider, subject: str = "sub-1", **overrides: object) -> UserIdentity:
    values: dict[str, object] = {
        "user_id": user.id,
        "provider_id": provider.id,
        "issuer": provider.issuer,
        "subject": subject,
        "linked_via": "user_link",
    }
    values.update(overrides)
    return UserIdentity(**values)


@pytest_asyncio.fixture
async def provider(db_session: AsyncSession) -> SsoProvider:
    prov = _provider()
    db_session.add(prov)
    await db_session.flush()
    return prov


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = UserFactory.build(login="sso_model_user", email="sso_model_user@example.com")
    db_session.add(u)
    await db_session.flush()
    return u


async def _count_identities(db_session: AsyncSession, **where: object) -> int:
    stmt = select(func.count()).select_from(UserIdentity)
    for column, value in where.items():
        stmt = stmt.where(getattr(UserIdentity, column) == value)
    return int(await db_session.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# Provider defaults and constraints
# ---------------------------------------------------------------------------


async def test_provider_server_defaults(db_session: AsyncSession, provider: SsoProvider) -> None:
    await db_session.refresh(provider)
    assert provider.enabled is False
    assert provider.position == 0
    assert provider.token_endpoint_auth_method == "client_secret_basic"
    assert provider.scopes == "openid profile email"
    assert provider.claim_config == {}
    assert provider.allow_jit_provisioning is False
    assert provider.link_policy == "manual"
    assert provider.trust_email_verified is False
    assert provider.allowed_email_domains == []
    assert provider.required_groups == []
    assert provider.admin_groups == []
    assert provider.sync_admin_flag is False
    assert provider.sync_groups is False
    assert provider.session_max_age_hours == 24
    assert provider.extra_auth_params == {}
    assert provider.discovery_cache is None
    assert provider.discovery_fetched_at is None
    assert provider.client_secret_encrypted is None
    assert provider.client_secret_env is None
    assert provider.created_at is not None


async def test_duplicate_slug_is_rejected(db_session: AsyncSession, provider: SsoProvider) -> None:
    db_session.add(_provider(slug=provider.slug, issuer="https://other.example.com"))
    with pytest.raises(IntegrityError, match="uq_sso_providers_slug"):
        await db_session.flush()


@pytest.mark.parametrize("slug", ["a", "Has-Upper", "has_underscore", "has space", "dots.not.allowed", "trailing\n"])
async def test_bad_slug_violates_check(db_session: AsyncSession, slug: str) -> None:
    db_session.add(_provider(slug=slug))
    with pytest.raises(IntegrityError, match="ck_sso_providers_slug"):
        await db_session.flush()


async def test_overlong_slug_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(_provider(slug="x" * 41))
    with pytest.raises(DBAPIError, match="value too long"):
        await db_session.flush()


@pytest.mark.parametrize("slug", ["ab", "keycloak", "azure-ad-2", "x" * 40])
async def test_good_slug_is_accepted(db_session: AsyncSession, slug: str) -> None:
    db_session.add(_provider(slug=slug))
    await db_session.flush()


async def test_both_secret_sources_violate_check(db_session: AsyncSession) -> None:
    db_session.add(_provider(client_secret_encrypted="gAAAAA-ciphertext", client_secret_env="SSO_SECRET"))
    with pytest.raises(IntegrityError, match="ck_sso_providers_single_secret_source"):
        await db_session.flush()


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_secret_encrypted": "gAAAAA-ciphertext"},
        {"client_secret_env": "SSO_SECRET"},
        {"token_endpoint_auth_method": "none"},
    ],
)
async def test_single_or_no_secret_source_is_accepted(db_session: AsyncSession, overrides: dict) -> None:
    db_session.add(_provider(**overrides))
    await db_session.flush()


async def test_unknown_token_endpoint_auth_method_violates_check(db_session: AsyncSession) -> None:
    db_session.add(_provider(token_endpoint_auth_method="private_key_jwt"))
    with pytest.raises(IntegrityError, match="ck_sso_providers_token_endpoint_auth_method"):
        await db_session.flush()


async def test_unknown_link_policy_violates_check(db_session: AsyncSession) -> None:
    db_session.add(_provider(link_policy="auto_any_email"))
    with pytest.raises(IntegrityError, match="ck_sso_providers_link_policy"):
        await db_session.flush()


@pytest.mark.parametrize("hours", [0, -1])
async def test_non_positive_session_max_age_violates_check(db_session: AsyncSession, hours: int) -> None:
    db_session.add(_provider(session_max_age_hours=hours))
    with pytest.raises(IntegrityError, match="ck_sso_providers_session_max_age_hours"):
        await db_session.flush()


@pytest.mark.parametrize("column", ["allowed_email_domains", "required_groups", "admin_groups"])
async def test_list_columns_must_hold_json_arrays(db_session: AsyncSession, column: str) -> None:
    db_session.add(_provider(**{column: {"not": "a list"}}))
    with pytest.raises(IntegrityError, match=f"ck_sso_providers_{column}_array"):
        await db_session.flush()


@pytest.mark.parametrize("column", ["claim_config", "extra_auth_params"])
async def test_object_columns_must_hold_json_objects(db_session: AsyncSession, column: str) -> None:
    db_session.add(_provider(**{column: ["not", "an", "object"]}))
    with pytest.raises(IntegrityError, match=f"ck_sso_providers_{column}_object"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# User identities
# ---------------------------------------------------------------------------


async def test_identity_round_trip(db_session: AsyncSession, provider: SsoProvider, user: User) -> None:
    db_session.add(_identity(user, provider, email_at_last_login="sso_model_user@example.com"))
    await db_session.flush()
    assert await _count_identities(db_session, user_id=user.id) == 1


async def test_duplicate_issuer_subject_is_rejected(
    db_session: AsyncSession, provider: SsoProvider, user: User
) -> None:
    other_provider = _provider(slug="test-idp-2")
    other_user = UserFactory.build(login="sso_model_other", email="sso_model_other@example.com")
    db_session.add_all([other_provider, other_user])
    await db_session.flush()
    db_session.add(_identity(user, provider, subject="same-sub"))
    await db_session.flush()

    # Same (issuer, subject) through another provider row and for another user.
    db_session.add(_identity(other_user, other_provider, subject="same-sub"))
    with pytest.raises(IntegrityError, match="uq_user_identities_issuer_subject"):
        await db_session.flush()


async def test_second_identity_for_same_provider_and_user_is_rejected(
    db_session: AsyncSession, provider: SsoProvider, user: User
) -> None:
    db_session.add(_identity(user, provider, subject="sub-a"))
    await db_session.flush()
    db_session.add(_identity(user, provider, subject="sub-b"))
    with pytest.raises(IntegrityError, match="uq_user_identities_provider_user"):
        await db_session.flush()


async def test_unknown_linked_via_violates_check(db_session: AsyncSession, provider: SsoProvider, user: User) -> None:
    db_session.add(_identity(user, provider, linked_via="magic"))
    with pytest.raises(IntegrityError, match="ck_user_identities_linked_via"):
        await db_session.flush()


@pytest.mark.parametrize("linked_via", ["jit", "user_link", "auto_email", "admin"])
async def test_known_linked_via_is_accepted(
    db_session: AsyncSession, provider: SsoProvider, user: User, linked_via: str
) -> None:
    db_session.add(_identity(user, provider, linked_via=linked_via))
    await db_session.flush()


async def test_identity_for_service_account_is_rejected_by_trigger(
    db_session: AsyncSession, provider: SsoProvider
) -> None:
    agent = UserFactory.build(
        login="sso_model_agent", email="sso_model_agent@example.com", is_service_account=True, password_hash=None
    )
    db_session.add(agent)
    await db_session.flush()
    db_session.add(_identity(agent, provider))
    with pytest.raises(IntegrityError, match="reject_sso_identity_for_inert_user"):
        await db_session.flush()


async def test_identity_for_anonymous_user_is_rejected_by_trigger(
    db_session: AsyncSession, provider: SsoProvider
) -> None:
    clear_anonymous_user_cache()
    try:
        anonymous = await get_anonymous_user(db_session)
        assert anonymous is not None
        db_session.add(_identity(anonymous, provider))
        with pytest.raises(IntegrityError, match="reject_sso_identity_for_inert_user"):
            await db_session.flush()
    finally:
        clear_anonymous_user_cache()


async def test_moving_an_identity_onto_a_service_account_is_rejected_by_trigger(
    db_session: AsyncSession, provider: SsoProvider, user: User
) -> None:
    agent = UserFactory.build(
        login="sso_model_agent2", email="sso_model_agent2@example.com", is_service_account=True, password_hash=None
    )
    db_session.add(agent)
    identity = _identity(user, provider)
    db_session.add(identity)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="reject_sso_identity_for_inert_user"):
        await db_session.execute(update(UserIdentity).where(UserIdentity.id == identity.id).values(user_id=agent.id))


async def test_deleting_a_user_cascades_identities(db_session: AsyncSession, provider: SsoProvider, user: User) -> None:
    db_session.add(_identity(user, provider))
    await db_session.flush()
    assert await _count_identities(db_session, user_id=user.id) == 1

    await db_session.execute(delete(User).where(User.id == user.id))
    db_session.expunge_all()
    assert await _count_identities(db_session, user_id=user.id) == 0


async def test_deleting_a_provider_with_identities_is_restricted(
    db_session: AsyncSession, provider: SsoProvider, user: User
) -> None:
    db_session.add(_identity(user, provider))
    await db_session.flush()

    with pytest.raises(IntegrityError, match="user_identities"):
        await db_session.execute(delete(SsoProvider).where(SsoProvider.id == provider.id))


async def test_orm_delete_of_a_provider_with_identities_is_restricted(
    db_session: AsyncSession, provider: SsoProvider, user: User
) -> None:
    # passive_deletes="all": the ORM must not null out the FK on loaded children.
    db_session.add(_identity(user, provider))
    await db_session.flush()

    await db_session.delete(provider)
    with pytest.raises(IntegrityError, match="user_identities"):
        await db_session.flush()


async def test_deleting_a_provider_without_identities_is_allowed(
    db_session: AsyncSession, provider: SsoProvider
) -> None:
    await db_session.execute(delete(SsoProvider).where(SsoProvider.id == provider.id))
    assert (
        await db_session.scalar(select(func.count()).select_from(SsoProvider).where(SsoProvider.id == provider.id)) == 0
    )


# ---------------------------------------------------------------------------
# Group mappings and managed memberships
# ---------------------------------------------------------------------------


async def test_group_mapping_unique_and_case_sensitive(db_session: AsyncSession, provider: SsoProvider) -> None:
    group = UserGroup(name="sso-model-devs")
    db_session.add(group)
    await db_session.flush()

    db_session.add(SsoGroupMapping(provider_id=provider.id, idp_group="Developers", user_group_id=group.id))
    # A different case is a different IdP group value.
    db_session.add(SsoGroupMapping(provider_id=provider.id, idp_group="developers", user_group_id=group.id))
    await db_session.flush()

    db_session.add(SsoGroupMapping(provider_id=provider.id, idp_group="Developers", user_group_id=group.id))
    with pytest.raises(IntegrityError, match="uq_sso_group_mappings_provider_group_user_group"):
        await db_session.flush()


async def test_deleting_provider_or_group_cascades_mappings(db_session: AsyncSession, provider: SsoProvider) -> None:
    group_a = UserGroup(name="sso-model-a")
    group_b = UserGroup(name="sso-model-b")
    db_session.add_all([group_a, group_b])
    await db_session.flush()
    db_session.add_all(
        [
            SsoGroupMapping(provider_id=provider.id, idp_group="a", user_group_id=group_a.id),
            SsoGroupMapping(provider_id=provider.id, idp_group="b", user_group_id=group_b.id),
        ]
    )
    await db_session.flush()

    await db_session.execute(delete(UserGroup).where(UserGroup.id == group_a.id))
    remaining = await db_session.scalar(
        select(func.count()).select_from(SsoGroupMapping).where(SsoGroupMapping.provider_id == provider.id)
    )
    assert remaining == 1

    await db_session.execute(delete(SsoProvider).where(SsoProvider.id == provider.id))
    db_session.expunge_all()
    remaining = await db_session.scalar(
        select(func.count()).select_from(SsoGroupMapping).where(SsoGroupMapping.provider_id == provider.id)
    )
    assert remaining == 0


async def test_managed_membership_survives_provider_delete_as_unmanaged(
    db_session: AsyncSession, provider: SsoProvider, user: User
) -> None:
    group = UserGroup(name="sso-model-managed")
    db_session.add(group)
    await db_session.flush()
    membership = UserGroupMember(group_id=group.id, user_id=user.id, managed_by_provider_id=provider.id)
    db_session.add(membership)
    await db_session.flush()
    membership_id = membership.id

    await db_session.execute(delete(SsoProvider).where(SsoProvider.id == provider.id))
    db_session.expunge_all()
    managed_by = await db_session.scalar(
        select(UserGroupMember.managed_by_provider_id).where(UserGroupMember.id == membership_id)
    )
    assert managed_by is None
    assert await db_session.scalar(select(func.count()).where(UserGroupMember.id == membership_id)) == 1


async def test_membership_defaults_to_unmanaged(db_session: AsyncSession, user: User) -> None:
    group = UserGroup(name="sso-model-manual")
    db_session.add(group)
    await db_session.flush()
    membership = UserGroupMember(group_id=group.id, user_id=user.id)
    db_session.add(membership)
    await db_session.flush()
    await db_session.refresh(membership)
    assert membership.managed_by_provider_id is None


# ---------------------------------------------------------------------------
# Schema matches the model
# ---------------------------------------------------------------------------


async def test_oauth_placeholder_columns_are_gone(db_session: AsyncSession) -> None:
    columns = set(
        (
            await db_session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
            )
        ).scalars()
    )
    assert "github_id" not in columns
    assert "google_id" not in columns
    assert not hasattr(User, "github_id")
    assert not hasattr(User, "google_id")


async def test_identity_trigger_is_installed(db_session: AsyncSession) -> None:
    triggers = set(
        (
            await db_session.execute(
                text("SELECT tgname FROM pg_trigger WHERE tgfoid = 'reject_sso_identity_for_inert_user'::regproc")
            )
        ).scalars()
    )
    assert triggers == {"trg_user_identities_reject_inert_user"}
