"""Service tests for SSO account resolution (``specivo.services.sso.provisioning``).

The decision order is fixed by the "Account linking and provisioning rules"
section of the SSO design:

1. identity by ``(iss, sub)``;
2. link intent: link to the signed-in user;
3. ``auto_verified_email``: link to the one user with that email, only when
   every precondition holds;
4. JIT provisioning when enabled;
5. otherwise ``sso_account_not_provisioned``.

Every precondition of rule 3 has its own test: it is the only place where an
email ever selects an account, so it is the account-takeover surface.

Logins and emails carry a ``prv`` prefix: modules run in parallel inside
uncommitted transactions and both columns are unique.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.auth_service import AuthService
from specivo.services.sso.errors import (
    SsoAccountDisabledError,
    SsoAccountNotProvisionedError,
    SsoEmailConflictError,
    SsoEmailMissingError,
    SsoIdentityInUseError,
    SsoProviderAlreadyLinkedError,
)
from specivo.services.sso.id_token import IdTokenClaims
from specivo.services.sso.provisioning import resolve_user, sanitize_login, subject_hash
from specivo.testing.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory
from specivo.testing.sso import build_sso_provider, link_identity

pytestmark = pytest.mark.service

SUB = "prv-subject-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token(provider: SsoProvider, sub: str = SUB) -> IdTokenClaims:
    return IdTokenClaims(
        issuer=provider.issuer,
        subject=sub,
        audience=(provider.client_id,),
        expires_at=0,
        issued_at=0,
        nonce="n",
    )


def _claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "email": "prv-member@example.com",
        "email_verified": True,
        "name": "Provisioned Member",
        "preferred_username": "prvmember",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


async def _resolve(
    db: AsyncSession,
    provider: SsoProvider,
    *,
    sub: str = SUB,
    intent: str = "login",
    link_user_id: int | None = None,
    **claim_overrides: Any,
):
    return await resolve_user(
        db,
        provider=provider,
        id_token=_token(provider, sub),
        claims=_claims(**claim_overrides),
        intent=intent,  # type: ignore[arg-type]
        link_user_id=link_user_id,
    )


VERIFIED_AT = datetime(2026, 1, 1, tzinfo=UTC)


async def _user(db: AsyncSession, factory=UserFactory, **fields: Any) -> User:
    """A local user whose email is verified unless ``email_verified_at=None`` is passed."""
    fields.setdefault("email_verified_at", VERIFIED_AT)
    user = factory.build(**fields)
    db.add(user)
    await db.flush()
    return user


async def _identity_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(UserIdentity)))


async def _user_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)))


def _auto_link(provider: SsoProvider) -> None:
    provider.link_policy = "auto_verified_email"
    provider.trust_email_verified = True


# ---------------------------------------------------------------------------
# 1. Identity lookup
# ---------------------------------------------------------------------------


async def test_existing_identity_wins_over_an_email_match(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _auto_link(sso_provider)
    sso_provider.allow_jit_provisioning = True
    owner = await _user(db_session, login="prvowner", email="prv-owner@example.com")
    other = await _user(db_session, login="prvother", email="prv-member@example.com")
    await link_identity(db_session, owner, sso_provider, subject=SUB)

    account = await _resolve(db_session, sso_provider)

    assert account.user.id == owner.id
    assert account.outcome == "login"
    assert other.id != owner.id
    assert await _identity_count(db_session) == 1


async def test_existing_identity_refreshes_the_profile(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.trust_email_verified = True
    user = await _user(db_session, login="prvprofile", email="prv-old@example.com", display_name="Old Name")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    await _resolve(db_session, sso_provider, email="prv-new@example.com", name="New Name", locale="th-TH")

    await db_session.refresh(user)
    assert user.display_name == "New Name"
    assert user.email == "prv-new@example.com"
    assert user.language == "th"


async def test_profile_email_change_skipped_on_collision(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.trust_email_verified = True
    user = await _user(db_session, login="prvcollide", email="prv-mine@example.com")
    await _user(db_session, login="prvtaken", email="prv-taken@example.com")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        account = await _resolve(db_session, sso_provider, email="PRV-Taken@example.com", name="Still Signs In")

    assert account.user.id == user.id
    await db_session.refresh(user)
    assert user.email == "prv-mine@example.com"
    assert user.display_name == "Still Signs In"
    assert "sso.profile.email_conflict" in caplog.text


# --- Email trust on login ---------------------------------------------------


@pytest.mark.parametrize(
    ("trusted", "email_verified"),
    [(False, True), (True, None), (True, False), (True, "true")],
    ids=["provider_not_trusted", "claim_missing", "claim_false", "claim_string_true"],
)
async def test_unverified_token_email_is_not_copied_to_the_user(
    db_session: AsyncSession,
    sso_provider: SsoProvider,
    caplog: pytest.LogCaptureFixture,
    trusted: bool,
    email_verified: Any,
) -> None:
    sso_provider.trust_email_verified = trusted
    user = await _user(db_session, login="prvsquat", email="prv-squat-old@example.com")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        await _resolve(
            db_session, sso_provider, email="prv-squat-new@example.com", email_verified=email_verified, name="Renamed"
        )

    await db_session.refresh(user)
    assert user.email == "prv-squat-old@example.com"
    assert user.email_verified_at == VERIFIED_AT
    # The other profile fields still follow the token.
    assert user.display_name == "Renamed"
    assert "sso.profile.email_unverified" in caplog.text


async def test_trusted_verified_email_change_marks_the_new_address_verified(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.trust_email_verified = True
    user = await _user(db_session, login="prvnewaddr", email="prv-addr-old@example.com", email_verified_at=None)
    await link_identity(db_session, user, sso_provider, subject=SUB)

    await _resolve(db_session, sso_provider, email="prv-addr-new@example.com")

    await db_session.refresh(user)
    assert user.email == "prv-addr-new@example.com"
    assert user.email_verified_at is not None
    assert user.email_verified_at != VERIFIED_AT


async def test_verified_flag_is_never_carried_to_a_new_address(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    """The old address was verified; an email change always re-stamps (or clears) the flag for the new one."""
    sso_provider.trust_email_verified = True
    user = await _user(db_session, login="prvcarry", email="prv-carry-old@example.com")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    await _resolve(db_session, sso_provider, email="prv-carry-new@example.com")

    await db_session.refresh(user)
    assert user.email == "prv-carry-new@example.com"
    assert user.email_verified_at is not None
    assert user.email_verified_at > VERIFIED_AT


async def test_same_email_keeps_the_verified_flag(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session, login="prvsame", email="prv-same@example.com")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    await _resolve(db_session, sso_provider, email="prv-same@example.com", email_verified=None)

    await db_session.refresh(user)
    assert user.email_verified_at == VERIFIED_AT


async def test_unsupported_locale_leaves_the_language(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session, login="prvlocale", email="prv-locale@example.com", language="en")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    await _resolve(db_session, sso_provider, email="prv-locale@example.com", locale="xx-YY")

    await db_session.refresh(user)
    assert user.language == "en"


async def test_custom_claim_names_are_used(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.claim_config = {"name_claim": "full_name", "locale_claim": "lang", "email_claim": "mail"}
    sso_provider.trust_email_verified = True
    user = await _user(db_session, login="prvclaims", email="prv-claims@example.com")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    await _resolve(
        db_session,
        sso_provider,
        email="prv-ignored@example.com",
        mail="prv-claims@example.com",
        full_name="From Custom Claim",
        lang="th",
    )

    await db_session.refresh(user)
    assert user.display_name == "From Custom Claim"
    assert user.email == "prv-claims@example.com"
    assert user.language == "th"


async def test_deactivated_user_with_identity_is_disabled(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session, login="prvdeact", email="prv-deact@example.com", status="deactivated")
    await link_identity(db_session, user, sso_provider, subject=SUB)

    with pytest.raises(SsoAccountDisabledError):
        await _resolve(db_session, sso_provider)


# ---------------------------------------------------------------------------
# 2. Link intent
# ---------------------------------------------------------------------------


async def test_link_intent_links_the_signed_in_user(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session, login="prvlinker", email="prv-linker@example.com", display_name="Keep Me")

    account = await _resolve(db_session, sso_provider, intent="link", link_user_id=user.id, name="Other Name")

    assert account.user.id == user.id
    assert account.outcome == "linked"
    assert account.identity.linked_via == "user_link"
    assert account.identity.issuer == sso_provider.issuer
    assert account.identity.subject == SUB
    await db_session.refresh(user)
    # Linking is not a login: the profile is left as the user set it.
    assert user.display_name == "Keep Me"


async def test_link_intent_works_without_any_email(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    user = await _user(db_session, login="prvnoemail", email="prv-noemail@example.com")
    account = await _resolve(
        db_session, sso_provider, intent="link", link_user_id=user.id, email=None, email_verified=None
    )
    assert account.identity.user_id == user.id


async def test_link_intent_refuses_an_identity_of_another_user(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    owner = await _user(db_session, login="prvinuse1", email="prv-inuse1@example.com")
    me = await _user(db_session, login="prvinuse2", email="prv-inuse2@example.com")
    await link_identity(db_session, owner, sso_provider, subject=SUB)

    with pytest.raises(SsoIdentityInUseError) as exc:
        await _resolve(db_session, sso_provider, intent="link", link_user_id=me.id)
    assert exc.value.code == "sso_identity_in_use"


async def test_link_intent_for_an_identity_already_mine_is_a_no_op(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    me = await _user(db_session, login="prvmine", email="prv-mine2@example.com")
    identity = await link_identity(db_session, me, sso_provider, subject=SUB)

    account = await _resolve(db_session, sso_provider, intent="link", link_user_id=me.id)

    assert account.identity.id == identity.id
    assert await _identity_count(db_session) == 1


async def test_link_intent_refuses_a_second_identity_at_the_same_provider(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    me = await _user(db_session, login="prvdouble", email="prv-double@example.com")
    await link_identity(db_session, me, sso_provider, subject="prv-first-subject")

    with pytest.raises(SsoProviderAlreadyLinkedError) as exc:
        await _resolve(db_session, sso_provider, sub="prv-second-subject", intent="link", link_user_id=me.id)
    assert exc.value.code == "sso_provider_already_linked"
    assert await _identity_count(db_session) == 1


async def test_link_intent_without_a_user_is_refused(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider, intent="link", link_user_id=None)


@pytest.mark.parametrize(
    "fields",
    [{"is_service_account": True, "password_hash": None}, {"status": "deactivated"}],
    ids=["service_account", "deactivated"],
)
async def test_link_intent_refuses_an_account_that_cannot_sign_in(
    db_session: AsyncSession, sso_provider: SsoProvider, fields: dict[str, Any]
) -> None:
    user = await _user(db_session, login="prvcannot", email="prv-cannot@example.com", **fields)
    with pytest.raises(SsoAccountDisabledError):
        await _resolve(db_session, sso_provider, intent="link", link_user_id=user.id)
    assert await _identity_count(db_session) == 0


# ---------------------------------------------------------------------------
# 3. auto_verified_email
# ---------------------------------------------------------------------------


async def test_auto_link_links_the_one_user_with_a_verified_email(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _auto_link(sso_provider)
    user = await _user(db_session, login="prvauto", email="PRV-Member@Example.com")

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        account = await _resolve(db_session, sso_provider)

    assert account.user.id == user.id
    assert account.outcome == "auto_linked"
    assert account.identity.linked_via == "auto_email"
    assert "sso.account.linked" in caplog.text
    assert "method=auto_email" in caplog.text
    assert subject_hash(SUB) in caplog.text
    assert SUB not in caplog.text


@pytest.mark.parametrize(
    "email_verified",
    [None, False, "true", "True", 1],
    ids=["missing", "false", "string_true", "string_True", "int_1"],
)
async def test_auto_link_requires_a_boolean_true_email_verified(
    db_session: AsyncSession, sso_provider: SsoProvider, email_verified: Any
) -> None:
    _auto_link(sso_provider)
    await _user(db_session, login="prvunverified", email="prv-member@example.com")

    claims = _claims()
    if email_verified is None:
        claims.pop("email_verified")
    else:
        claims["email_verified"] = email_verified
    with pytest.raises(SsoAccountNotProvisionedError):
        await resolve_user(db_session, provider=sso_provider, id_token=_token(sso_provider), claims=claims)
    assert await _identity_count(db_session) == 0


async def test_entra_style_token_is_never_auto_linked(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    """Entra ID sends an unverified, mutable ``email`` and no ``email_verified``."""
    _auto_link(sso_provider)
    await _user(db_session, login="prventra", email="prv-entra@example.com")
    claims = {"email": "prv-entra@example.com", "name": "Entra User", "preferred_username": "prv-entra@example.com"}

    with pytest.raises(SsoAccountNotProvisionedError):
        await resolve_user(db_session, provider=sso_provider, id_token=_token(sso_provider), claims=claims)
    assert await _identity_count(db_session) == 0


async def test_auto_link_requires_trust_email_verified(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.link_policy = "auto_verified_email"
    sso_provider.trust_email_verified = False
    await _user(db_session, login="prvnotrust", email="prv-member@example.com")

    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_manual_link_policy_never_auto_links(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.link_policy = "manual"
    sso_provider.trust_email_verified = True
    await _user(db_session, login="prvmanual", email="prv-member@example.com")

    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_auto_link_never_captures_an_admin(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _auto_link(sso_provider)
    await _user(db_session, AdminUserFactory, login="prvadmin", email="prv-member@example.com")

    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_auto_link_never_captures_a_service_account(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _auto_link(sso_provider)
    await _user(db_session, ServiceAccountFactory, login="prvagent", email="prv-member@example.com")

    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_auto_link_never_captures_the_anonymous_user(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    _auto_link(sso_provider)
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None

    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider, email=anonymous.email)
    assert await _identity_count(db_session) == 0


async def test_auto_link_refused_when_two_users_share_the_email(
    db_session: AsyncSession, sso_provider: SsoProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case-insensitive unique index prevents this today; the rule still counts matches itself."""
    from specivo.services.sso import provisioning

    _auto_link(sso_provider)
    first = await _user(db_session, login="prvshared1", email="prv-member@example.com")
    second = await _user(db_session, login="prvshared2", email="prv-shared2@example.com")

    async def two_matches(db: AsyncSession, email: str) -> list[User]:
        return [first, second]

    monkeypatch.setattr(provisioning, "_users_with_email", two_matches)
    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_auto_link_matches_the_email_case_insensitively(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    _auto_link(sso_provider)
    user = await _user(db_session, login="prvcase", email="prv-member@example.com")
    account = await _resolve(db_session, sso_provider, email="PRV-MEMBER@EXAMPLE.COM")
    assert account.user.id == user.id


async def test_auto_link_requires_the_local_email_to_be_verified(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-account hijack: an unverified local sign-up must not receive the IdP identity."""
    _auto_link(sso_provider)
    await _user(db_session, login="prvpreacct", email="prv-member@example.com", email_verified_at=None)

    with caplog.at_level(logging.INFO, logger="specivo.sso"), pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert "reason=target_email_unverified" in caplog.text
    assert await _identity_count(db_session) == 0


async def test_auto_link_to_an_unverified_target_falls_through_to_jit_conflict(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    _auto_link(sso_provider)
    sso_provider.allow_jit_provisioning = True
    await _user(db_session, login="prvpreacct2", email="prv-member@example.com", email_verified_at=None)

    with pytest.raises(SsoEmailConflictError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_auto_link_refused_when_the_target_has_an_identity_at_this_provider(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    _auto_link(sso_provider)
    user = await _user(db_session, login="prvhasone", email="prv-member@example.com")
    await link_identity(db_session, user, sso_provider, subject="prv-other-subject")

    with pytest.raises(SsoAccountNotProvisionedError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 1


async def test_auto_link_to_a_deactivated_user_is_disabled_and_not_linked(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    _auto_link(sso_provider)
    await _user(db_session, login="prvautooff", email="prv-member@example.com", status="deactivated")

    with pytest.raises(SsoAccountDisabledError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_failed_auto_link_falls_through_to_jit_email_conflict(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    _auto_link(sso_provider)
    sso_provider.allow_jit_provisioning = True
    await _user(db_session, AdminUserFactory, login="prvadmin2", email="prv-member@example.com")

    with pytest.raises(SsoEmailConflictError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


# ---------------------------------------------------------------------------
# 4. JIT provisioning
# ---------------------------------------------------------------------------


async def test_jit_is_off_by_default(db_session: AsyncSession, mock_idp) -> None:
    provider = build_sso_provider(mock_idp, slug="prv-default")
    db_session.add(provider)
    await db_session.flush()
    assert provider.allow_jit_provisioning is False
    assert provider.link_policy == "manual"

    before = await _user_count(db_session)
    with pytest.raises(SsoAccountNotProvisionedError) as exc:
        await _resolve(db_session, provider)
    assert exc.value.code == "sso_account_not_provisioned"
    assert await _user_count(db_session) == before


async def test_jit_creates_a_user_without_a_password(
    db_session: AsyncSession, sso_provider: SsoProvider, caplog: pytest.LogCaptureFixture
) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.trust_email_verified = True

    with caplog.at_level(logging.INFO, logger="specivo.sso"):
        account = await _resolve(db_session, sso_provider, locale="th")

    user = account.user
    assert account.outcome == "provisioned"
    assert user.id is not None
    assert user.login == "prvmember"
    assert user.email == "prv-member@example.com"
    assert user.display_name == "Provisioned Member"
    assert user.password_hash is None
    assert user.status == "active"
    assert user.is_admin is False
    assert user.is_service_account is False
    assert user.must_change_password is False
    assert user.email_verified_at is not None
    assert user.language == "th"
    assert account.identity.linked_via == "jit"
    assert account.identity.user_id == user.id
    assert "sso.user.provisioned" in caplog.text
    assert subject_hash(SUB) in caplog.text
    assert SUB not in caplog.text


async def test_jit_unverified_email_leaves_email_verified_at_empty(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.allow_jit_provisioning = True
    account = await _resolve(db_session, sso_provider, email_verified="true")
    assert account.user.email_verified_at is None


@pytest.mark.parametrize(
    ("trusted", "claims"),
    [
        (False, {"email_verified": True}),
        (True, {"email_verified": None}),
        (True, {"email_verified": False}),
    ],
    ids=["provider_not_trusted", "entra_style_no_claim", "claim_false"],
)
async def test_jit_marks_the_email_verified_only_when_trusted_and_verified(
    db_session: AsyncSession, sso_provider: SsoProvider, trusted: bool, claims: dict[str, Any]
) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.trust_email_verified = trusted
    account = await _resolve(db_session, sso_provider, **claims)
    assert account.user.email == "prv-member@example.com"
    assert account.user.email_verified_at is None


async def test_jit_unsupported_locale_uses_the_default_language(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.allow_jit_provisioning = True
    account = await _resolve(db_session, sso_provider, locale="xx")
    assert account.user.language == "en"


async def test_jit_login_collision_gets_a_numeric_suffix(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    await _user(db_session, login="prvmember", email="prv-first@example.com")
    await _user(db_session, login="prvmember-2", email="prv-second@example.com")

    account = await _resolve(db_session, sso_provider, preferred_username="PrvMember")

    assert account.user.login == "prvmember-3"


async def test_jit_login_falls_back_to_the_email_local_part(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.allow_jit_provisioning = True
    account = await _resolve(db_session, sso_provider, preferred_username=None, email="prv.jane@example.com")
    assert account.user.login == "prv-jane"


async def test_jit_uses_the_configured_username_claim(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    sso_provider.claim_config = {"username_claim": "upn"}
    account = await _resolve(db_session, sso_provider, upn="Prv.Custom@corp.example.com")
    assert account.user.login == "prv-custom"


async def test_jit_missing_email_is_refused(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    before = await _user_count(db_session)

    with pytest.raises(SsoEmailMissingError) as exc:
        await _resolve(db_session, sso_provider, email=None)
    assert exc.value.code == "sso_email_missing"
    assert await _user_count(db_session) == before


async def test_jit_email_conflict_is_refused_and_never_linked(
    db_session: AsyncSession, sso_provider: SsoProvider
) -> None:
    sso_provider.allow_jit_provisioning = True
    await _user(db_session, login="prvexisting", email="Prv-Member@example.com")
    before = await _user_count(db_session)

    with pytest.raises(SsoEmailConflictError) as exc:
        await _resolve(db_session, sso_provider)
    assert exc.value.code == "sso_email_conflict"
    assert await _user_count(db_session) == before
    assert await _identity_count(db_session) == 0


async def test_jit_email_conflict_with_a_service_account(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    sso_provider.allow_jit_provisioning = True
    await _user(db_session, ServiceAccountFactory, login="prvbot", email="prv-member@example.com")

    with pytest.raises(SsoEmailConflictError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_jit_retries_a_login_taken_by_a_concurrent_sign_up(
    db_session: AsyncSession, sso_provider: SsoProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.services.sso import provisioning

    sso_provider.allow_jit_provisioning = True
    await _user(db_session, login="prvrace", email="prv-race@example.com")
    real = provisioning._unique_login
    calls: list[str] = []

    async def stale_first(db: AsyncSession, base: str) -> str:
        calls.append(base)
        return "prvrace" if len(calls) == 1 else await real(db, base)

    monkeypatch.setattr(provisioning, "_unique_login", stale_first)
    account = await _resolve(db_session, sso_provider, preferred_username="prvrace")

    assert account.user.login == "prvrace-2"
    assert len(calls) == 2


async def test_jit_email_taken_by_a_concurrent_sign_up_is_a_conflict(
    db_session: AsyncSession, sso_provider: SsoProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.services.sso import provisioning

    sso_provider.allow_jit_provisioning = True
    await _user(db_session, login="prvracemail", email="prv-member@example.com")
    real = provisioning._email_taken
    calls: list[str] = []

    async def missed_first(db: AsyncSession, email: str, **kwargs: Any) -> bool:
        calls.append(email)
        return False if len(calls) == 1 else await real(db, email, **kwargs)

    monkeypatch.setattr(provisioning, "_email_taken", missed_first)
    with pytest.raises(SsoEmailConflictError):
        await _resolve(db_session, sso_provider)
    assert await _identity_count(db_session) == 0


async def test_jit_user_cannot_sign_in_with_a_password(db_session: AsyncSession, sso_provider: SsoProvider) -> None:
    from specivo.core.exceptions import AppError

    sso_provider.allow_jit_provisioning = True
    account = await _resolve(db_session, sso_provider)

    with pytest.raises(AppError) as jit_exc:
        await AuthService().login(db_session, account.user.login, "anything-at-all")
    with pytest.raises(AppError) as wrong_exc:
        await AuthService().login(db_session, "prv-no-such-login", "anything-at-all")
    assert jit_exc.value.code == wrong_exc.value.code
    assert jit_exc.value.message == wrong_exc.value.message


# ---------------------------------------------------------------------------
# Login sanitising
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice", "alice"),
        ("alice.smith", "alice-smith"),
        ("alice@example.com", "alice"),
        ("  Bob  Builder ", "bob-builder"),
        ("Zoë", "zoe"),
        ("a", "a-user"),
        ("--__--", None),
        ("$anonymous", "anonymous"),
        ("x" * 150, "x" * 90),
    ],
)
def test_sanitize_login(raw: str, expected: str | None) -> None:
    assert sanitize_login(raw) == expected
