"""Resolve the Specivo account behind a validated external identity.

The identity key is ``(iss, sub)``. :func:`resolve_user` is the single entry
point the callback uses, and decides in this order (the "Account linking and
provisioning rules" of the SSO design):

1. **identity by ``(iss, sub)``**: that user signs in. Email is never used
   here. With ``intent="link"`` the identity must already be the signed-in
   user's (``sso_identity_in_use`` otherwise).
2. **link intent** (a signed-in user pressed "Connect"): link the identity
   to that user (``linked_via="user_link"``), unless they already have an
   identity at this provider (``sso_provider_already_linked``).
3. **``link_policy="auto_verified_email"``**: link to the user with the
   token's email only if *all* hold: ``trust_email_verified`` is on; the
   ``email_verified`` claim is the boolean ``true`` (not the string); exactly
   one user has that email, case-insensitively; that user is not an admin,
   a service account or the anonymous user, has a verified email
   (``email_verified_at`` set) and has no identity at this provider. This
   is the only code path that selects an account by email.
4. **JIT provisioning** when the provider allows it: a new active user
   without a password (``sso_email_missing`` without an email,
   ``sso_email_conflict`` when another account has it: never linked). Its
   email counts as verified only when the provider is trusted and the claim
   is the boolean ``true``.
5. otherwise ``sso_account_not_provisioned``.

Status checks always apply (:func:`_refusal_reason`): service accounts and
the anonymous user never sign in, even with an identity row; deactivated,
unverified and admin-locked accounts are ``sso_account_disabled`` until a
timed admin lock runs out. A brute-force lockout protects the password only
and does not block SSO.

On a login (rules 1, 3 and 4) the provider's access policy
(:func:`specivo.services.sso.mapping.enforce_access`: email domain, required
groups, group overage) is checked first, after the status checks and
**before** an account is signed in, linked or created, so a refused user is
never provisioned or linked; a refused existing identity loses its sessions.
Then ``last_login_at`` is updated on the user and the identity, the profile
fields follow the token: display name, email (only a trusted, verified one;
skipped, and logged, when another user has it) and language (``locale``
mapped to a supported language), and the admin flag and SSO-managed group
memberships follow the groups claim
(:func:`specivo.services.sso.mapping.apply_mapping`). A link (rule 2) changes
no profile field and maps nothing.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.sso import events
from specivo.services.sso.claims import read_groups
from specivo.services.sso.errors import (
    SsoAccountDisabledError,
    SsoAccountNotProvisionedError,
    SsoEmailConflictError,
    SsoEmailMissingError,
    SsoIdentityInUseError,
    SsoProviderAlreadyLinkedError,
)
from specivo.services.sso.events import subject_hash
from specivo.services.sso.flow import Intent
from specivo.services.sso.id_token import IdTokenClaims
from specivo.services.sso.log import logger
from specivo.services.sso.mapping import apply_mapping, enforce_access

__all__ = [
    "Outcome",
    "ResolvedAccount",
    "map_locale",
    "resolve_user",
    "sanitize_login",
    "subject_hash",
]

#: What :func:`resolve_user` did: signed in an existing identity, linked one
#: for the signed-in user, linked one by verified email, or created a user.
Outcome = Literal["login", "linked", "auto_linked", "provisioned"]

_DEFAULT_CLAIMS = {
    "email_claim": "email",
    "username_claim": "preferred_username",
    "name_claim": "name",
    "locale_claim": "locale",
}
_MAX_EMAIL_LENGTH = 255
_MAX_DISPLAY_NAME_LENGTH = 255
# The login column holds 100 characters; the base leaves room for "-NNNN".
_MAX_LOGIN_BASE_LENGTH = 90
_MIN_LOGIN_LENGTH = 3
_LOGIN_INVALID_RUN = re.compile(r"[^a-z0-9_-]+")
_LOGIN_DASH_RUN = re.compile(r"-{2,}")
_FALLBACK_LOGIN = "user"
# Inserts of a JIT user tried before a lost race on the login gives up.
_INSERT_ATTEMPTS = 3


@dataclass(frozen=True)
class ResolvedAccount:
    """The user who signs in (or links), the identity used, and what happened."""

    user: User
    identity: UserIdentity
    outcome: Outcome = "login"


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def _claim(claims: Mapping[str, Any], provider: SsoProvider, key: str) -> Any:
    name = (provider.claim_config or {}).get(key) or _DEFAULT_CLAIMS[key]
    return claims.get(name) if isinstance(name, str) else None


def _text_claim(claims: Mapping[str, Any], provider: SsoProvider, key: str, limit: int) -> str | None:
    value = _claim(claims, provider, key)
    if isinstance(value, str):
        value = value.strip()
        if 0 < len(value) <= limit:
            return value
    return None


def _email_from(claims: Mapping[str, Any], provider: SsoProvider) -> str | None:
    email = _text_claim(claims, provider, "email_claim", _MAX_EMAIL_LENGTH)
    # Only a plausible address; anything else is treated as missing.
    if email is not None and "@" in email and " " not in email:
        return email
    return None


def _email_verified(claims: Mapping[str, Any]) -> bool:
    """``True`` only for the JSON boolean ``true``; the string ``"true"`` does not count."""
    return claims.get("email_verified") is True


def _trusted_verified_email(provider: SsoProvider, claims: Mapping[str, Any]) -> bool:
    """The token's email may be taken as verified: the admin trusts this IdP's
    ``email_verified`` and the claim is the boolean ``true``. Only then is an
    email copied onto an account or marked verified."""
    return provider.trust_email_verified and _email_verified(claims)


def map_locale(value: object, available: list[str] | None = None) -> str | None:
    """A supported language code for a ``locale`` claim (``th``, ``th-TH``, ``th_TH``), or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    languages = available if available is not None else get_settings().available_languages
    tag = value.strip().replace("_", "-").lower()
    if tag in languages:
        return tag
    primary = tag.split("-", 1)[0]
    return primary if primary in languages else None


def sanitize_login(raw: str) -> str | None:
    """A login following the login rules (``[a-z0-9_-]``, 3+ characters) made from ``raw``, or ``None``.

    An email-shaped value keeps its local part; accents are dropped; runs of
    other characters become one ``-``. The result is at most
    ``_MAX_LOGIN_BASE_LENGTH`` characters, so a collision suffix still fits.
    """
    value = raw.strip()
    if "@" in value:
        value = value.split("@", 1)[0]
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    value = _LOGIN_INVALID_RUN.sub("-", value)
    value = _LOGIN_DASH_RUN.sub("-", value).strip("-_")
    if not value:
        return None
    value = value[:_MAX_LOGIN_BASE_LENGTH].rstrip("-_")
    if len(value) < _MIN_LOGIN_LENGTH:
        value = f"{value}-{_FALLBACK_LOGIN}"
    return value


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _refusal_reason(user: User, now: datetime) -> str | None:
    """Why ``user`` may not sign in through SSO, or ``None`` if they may.

    Two kinds of lock exist. A brute-force lockout sets only ``locked_until``
    and leaves ``status="active"``: it protects the password, so SSO is
    allowed. An admin lock sets ``status="locked"``, permanently
    (``locked_until`` NULL) or until a time: refused until that time has
    passed, after which the account is active again (as for password login).
    """
    if user.is_service_account:
        return "service_account"
    if user.is_anonymous:
        return "anonymous"
    if user.status == "active":
        return None
    if user.status == "locked":
        if user.locked_until is not None and user.locked_until <= now:
            return None
        return "admin_locked"
    return f"status_{user.status}"


def _check_status(user: User, provider: SsoProvider, sub_hash: str, now: datetime) -> None:
    reason = _refusal_reason(user, now)
    if reason is not None:
        logger.warning(
            "sso.account.refused provider=%s sub=%s user_id=%s reason=%s", provider.slug, sub_hash, user.id, reason
        )
        raise SsoAccountDisabledError(reason)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def _users_with_email(db: AsyncSession, email: str) -> list[User]:
    """Every user whose email equals ``email`` case-insensitively (rule 3 only)."""
    return list((await db.execute(select(User).where(func.lower(User.email) == email.lower()))).scalars().all())


async def _email_taken(db: AsyncSession, email: str, *, except_user_id: int | None = None) -> bool:
    stmt = select(User.id).where(func.lower(User.email) == email.lower())
    if except_user_id is not None:
        stmt = stmt.where(User.id != except_user_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def _has_identity_at(db: AsyncSession, user_id: int, provider: SsoProvider) -> bool:
    stmt = select(UserIdentity.id).where(UserIdentity.user_id == user_id, UserIdentity.provider_id == provider.id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def _unique_login(db: AsyncSession, base: str) -> str:
    """``base``, or ``base-2``, ``base-3`` ... whichever is free (case-insensitive)."""
    escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    lowered = func.lower(User.login)
    taken = set(
        (await db.execute(select(lowered).where((lowered == base) | lowered.like(f"{escaped}-%", escape="\\"))))
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _new_identity(user: User, provider: SsoProvider, id_token: IdTokenClaims, linked_via: str) -> UserIdentity:
    return UserIdentity(
        user_id=user.id,
        provider_id=provider.id,
        issuer=id_token.issuer,
        subject=id_token.subject,
        linked_via=linked_via,
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


async def _refresh_profile(
    db: AsyncSession, user: User, provider: SsoProvider, claims: Mapping[str, Any], sub_hash: str
) -> None:
    """Follow the token's display name, email and language when they changed.

    The email follows only a trusted, verified token email (otherwise an IdP
    user could claim an address they do not own); the new address is then
    marked verified now, so a verified flag never carries over to it.
    """
    name = _text_claim(claims, provider, "name_claim", _MAX_DISPLAY_NAME_LENGTH)
    if name is not None and name != user.display_name:
        user.display_name = name

    email = _email_from(claims, provider)
    if email is not None and email != user.email:
        if not _trusted_verified_email(provider, claims):
            logger.info("sso.profile.email_unverified provider=%s user_id=%s sub=%s", provider.slug, user.id, sub_hash)
        elif await _email_taken(db, email, except_user_id=user.id):
            logger.info("sso.profile.email_conflict provider=%s user_id=%s sub=%s", provider.slug, user.id, sub_hash)
        else:
            user.email = email
            user.email_verified_at = utcnow()

    language = map_locale(_claim(claims, provider, "locale_claim"))
    if language is not None and language != user.language:
        user.language = language


def _record_login(user: User, identity: UserIdentity, email: str | None, now: datetime) -> None:
    if user.status == "locked":
        # An admin lock whose time has passed: end it, as password login does.
        user.status = "active"
        user.locked_until = None
    user.last_login_at = now
    identity.last_login_at = now
    if email is not None:
        identity.email_at_last_login = email


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


async def _link_signed_in_user(
    db: AsyncSession,
    *,
    provider: SsoProvider,
    id_token: IdTokenClaims,
    claims: Mapping[str, Any],
    link_user_id: int | None,
    sub_hash: str,
    now: datetime,
) -> ResolvedAccount:
    """Rule 2: connect the identity to the user who started the link."""
    user = await db.get(User, link_user_id) if link_user_id is not None else None
    if user is None:
        logger.warning("sso.account.refused provider=%s sub=%s reason=link_without_user", provider.slug, sub_hash)
        raise SsoAccountNotProvisionedError("link_without_user")
    _check_status(user, provider, sub_hash, now)
    if await _has_identity_at(db, user.id, provider):
        logger.warning(
            "sso.account.refused provider=%s sub=%s user_id=%s reason=provider_already_linked",
            provider.slug,
            sub_hash,
            user.id,
        )
        raise SsoProviderAlreadyLinkedError("provider_already_linked")

    identity = _new_identity(user, provider, id_token, "user_link")
    identity.email_at_last_login = _email_from(claims, provider)
    db.add(identity)
    await db.flush()
    events.account_linked(
        provider_slug=provider.slug,
        user_id=user.id,
        subject=id_token.subject,
        method="user_link",
        issuer=provider.issuer,
    )
    return ResolvedAccount(user=user, identity=identity, outcome="linked")


async def _auto_link_target(
    db: AsyncSession, *, provider: SsoProvider, claims: Mapping[str, Any], sub_hash: str
) -> User | None:
    """Rule 3: the one user the verified email may link to, or ``None`` (with the reason logged)."""
    if provider.link_policy != "auto_verified_email":
        return None

    def skip(reason: str) -> None:
        logger.info("sso.account.auto_link_skipped provider=%s sub=%s reason=%s", provider.slug, sub_hash, reason)

    if not provider.trust_email_verified:
        skip("email_verified_not_trusted")
        return None
    if not _email_verified(claims):
        skip("email_not_verified")
        return None
    email = _email_from(claims, provider)
    if email is None:
        skip("no_email")
        return None
    matches = await _users_with_email(db, email)
    if len(matches) != 1:
        skip("no_single_match")
        return None
    target = matches[0]
    if target.is_admin or target.is_service_account or target.is_anonymous:
        skip("protected_account")
        return None
    if target.email_verified_at is None:
        # A local account whose owner never proved the address: someone may
        # have registered it first to receive the IdP identity (pre-account hijack).
        skip("target_email_unverified")
        return None
    if await _has_identity_at(db, target.id, provider):
        skip("already_linked_at_provider")
        return None
    return target


async def _provision(
    db: AsyncSession,
    *,
    provider: SsoProvider,
    id_token: IdTokenClaims,
    claims: Mapping[str, Any],
    sub_hash: str,
    now: datetime,
) -> User:
    """Rule 4: create the user just in time."""
    email = _email_from(claims, provider)
    if email is None:
        logger.warning("sso.account.refused provider=%s sub=%s reason=email_missing", provider.slug, sub_hash)
        raise SsoEmailMissingError("email_missing")
    if await _email_taken(db, email):
        logger.warning("sso.account.refused provider=%s sub=%s reason=email_conflict", provider.slug, sub_hash)
        raise SsoEmailConflictError("email_conflict")

    username = _text_claim(claims, provider, "username_claim", 512)
    base = (sanitize_login(username) if username else None) or sanitize_login(email.split("@", 1)[0]) or _FALLBACK_LOGIN
    settings = get_settings()
    display_name = _text_claim(claims, provider, "name_claim", _MAX_DISPLAY_NAME_LENGTH)
    language = map_locale(_claim(claims, provider, "locale_claim")) or settings.default_language

    for attempt in range(_INSERT_ATTEMPTS):
        login = await _unique_login(db, base)
        user = User(
            login=login,
            email=email,
            password_hash=None,
            display_name=display_name or login,
            language=language,
            timezone="UTC",
            status="active",
            is_admin=False,
            is_service_account=False,
            must_change_password=False,
            email_verified_at=now if _trusted_verified_email(provider, claims) else None,
        )
        # A savepoint: a concurrent sign-up that took the login or the email
        # between the checks and the insert only undoes this row.
        savepoint = await db.begin_nested()
        try:
            db.add(user)
            await db.flush()
        except IntegrityError:
            await savepoint.rollback()
            if await _email_taken(db, email):
                logger.warning("sso.account.refused provider=%s sub=%s reason=email_conflict", provider.slug, sub_hash)
                raise SsoEmailConflictError("email_conflict") from None
            if attempt + 1 == _INSERT_ATTEMPTS:
                raise
            continue
        if savepoint.is_active:
            await savepoint.commit()
        events.user_provisioned(
            provider_slug=provider.slug, user_id=user.id, subject=id_token.subject, issuer=provider.issuer
        )
        return user
    raise AssertionError("unreachable")  # pragma: no cover


async def resolve_user(
    db: AsyncSession,
    *,
    provider: SsoProvider,
    id_token: IdTokenClaims,
    claims: Mapping[str, Any],
    intent: Intent = "login",
    link_user_id: int | None = None,
) -> ResolvedAccount:
    """The account to sign in (or link) for a validated ID token at ``provider``.

    ``claims`` are the ID token claims merged with userinfo; they supply the
    email, ``email_verified`` and the profile values. ``intent`` and
    ``link_user_id`` come from the login transaction. Raises an ``SsoError``
    (``sso_account_not_provisioned``, ``sso_account_disabled``,
    ``sso_identity_in_use``, ``sso_provider_already_linked``,
    ``sso_email_missing``, ``sso_email_conflict``, and the policy codes
    ``sso_domain_not_allowed``, ``sso_not_in_required_group``,
    ``sso_groups_overage``) on refusal.
    """
    sub_hash = subject_hash(id_token.subject)
    now = utcnow()
    email = _email_from(claims, provider)

    # 1. The identity, by (iss, sub) only.
    row = (
        await db.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(UserIdentity.issuer == id_token.issuer, UserIdentity.subject == id_token.subject)
        )
    ).first()

    if row is not None:
        identity, user = row
        if identity.provider_id != provider.id:
            # (iss, sub) is unique, so this is a second provider row with the same
            # issuer: the identity belongs to the other registration.
            logger.warning("sso.account.refused provider=%s sub=%s reason=other_provider", provider.slug, sub_hash)
            raise SsoAccountNotProvisionedError("identity_of_other_provider")
        if intent == "link":
            if user.id != link_user_id:
                logger.warning("sso.account.refused provider=%s sub=%s reason=identity_in_use", provider.slug, sub_hash)
                raise SsoIdentityInUseError("identity_in_use")
            _check_status(user, provider, sub_hash, now)
            return ResolvedAccount(user=user, identity=identity, outcome="linked")
        _check_status(user, provider, sub_hash, now)
        groups = read_groups(claims, provider)
        await enforce_access(
            db,
            provider=provider,
            claims=claims,
            groups=groups,
            email=email,
            subject=id_token.subject,
            user=user,
            identity=identity,
        )
        await _refresh_profile(db, user, provider, claims, sub_hash)
        _record_login(user, identity, email, now)
        await db.flush()
        await apply_mapping(db, provider=provider, user=user, groups=groups, subject=id_token.subject)
        return ResolvedAccount(user=user, identity=identity, outcome="login")

    # 2. A signed-in user connecting this identity.
    if intent == "link":
        return await _link_signed_in_user(
            db,
            provider=provider,
            id_token=id_token,
            claims=claims,
            link_user_id=link_user_id,
            sub_hash=sub_hash,
            now=now,
        )

    groups = read_groups(claims, provider)

    # 3. Link by verified email, under every precondition.
    target = await _auto_link_target(db, provider=provider, claims=claims, sub_hash=sub_hash)
    if target is not None:
        _check_status(target, provider, sub_hash, now)
        await enforce_access(
            db, provider=provider, claims=claims, groups=groups, email=email, subject=id_token.subject, user=target
        )
        identity = _new_identity(target, provider, id_token, "auto_email")
        db.add(identity)
        await db.flush()
        events.account_linked(
            provider_slug=provider.slug,
            user_id=target.id,
            subject=id_token.subject,
            method="auto_email",
            issuer=provider.issuer,
        )
        await _refresh_profile(db, target, provider, claims, sub_hash)
        _record_login(target, identity, email, now)
        await db.flush()
        await apply_mapping(db, provider=provider, user=target, groups=groups, subject=id_token.subject)
        return ResolvedAccount(user=target, identity=identity, outcome="auto_linked")

    # 4. Just-in-time provisioning.
    if provider.allow_jit_provisioning:
        await enforce_access(db, provider=provider, claims=claims, groups=groups, email=email, subject=id_token.subject)
        user = await _provision(db, provider=provider, id_token=id_token, claims=claims, sub_hash=sub_hash, now=now)
        identity = _new_identity(user, provider, id_token, "jit")
        db.add(identity)
        await db.flush()
        _record_login(user, identity, user.email, now)
        await db.flush()
        await apply_mapping(db, provider=provider, user=user, groups=groups, subject=id_token.subject)
        return ResolvedAccount(user=user, identity=identity, outcome="provisioned")

    # 5. Nothing else may create or pick an account.
    logger.info("sso.account.not_provisioned provider=%s sub=%s", provider.slug, sub_hash)
    raise SsoAccountNotProvisionedError("no_identity")
