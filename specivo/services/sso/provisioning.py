"""Resolve the Specivo account behind a validated external identity.

The identity key is ``(iss, sub)``, never the email. :func:`resolve_user`
is the single entry point the login callback uses; just-in-time
provisioning and account linking plug in here.

Rules applied today:

1. look up the ``UserIdentity`` by ``(issuer, subject)``;
2. none found: refuse with ``sso_account_not_provisioned``;
3. the account must be able to sign in: service accounts and the anonymous
   user never can (also when an identity row exists), a deactivated,
   unverified or admin-locked account cannot (``sso_account_disabled``). A
   brute-force lock (``locked_until`` set) protects the password only and
   does not block SSO;
4. on success ``last_login_at`` is updated on the user and the identity, and
   the identity's ``email_at_last_login`` is refreshed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.sso import SsoProvider, UserIdentity
from specivo.models.user import User
from specivo.services.sso.errors import SsoAccountDisabledError, SsoAccountNotProvisionedError
from specivo.services.sso.flow import Intent
from specivo.services.sso.id_token import IdTokenClaims
from specivo.services.sso.log import logger

__all__ = ["ResolvedAccount", "resolve_user", "subject_hash"]

_DEFAULT_EMAIL_CLAIM = "email"
_MAX_EMAIL_LENGTH = 255


@dataclass(frozen=True)
class ResolvedAccount:
    """The user who signs in and the identity they signed in with."""

    user: User
    identity: UserIdentity


def subject_hash(subject: str) -> str:
    """The first 16 hex characters of SHA-256(sub): enough to correlate log lines, not to identify."""
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]


def _email_from(claims: Mapping[str, Any], provider: SsoProvider) -> str | None:
    claim = (provider.claim_config or {}).get("email_claim") or _DEFAULT_EMAIL_CLAIM
    value = claims.get(claim) if isinstance(claim, str) else None
    if isinstance(value, str) and 0 < len(value) <= _MAX_EMAIL_LENGTH:
        return value
    return None


def _refusal_reason(user: User) -> str | None:
    """Why ``user`` may not sign in through SSO, or ``None`` if they may."""
    if user.is_service_account:
        return "service_account"
    if user.is_anonymous:
        return "anonymous"
    if user.status == "active":
        return None
    if user.status == "locked" and user.locked_until is not None:
        # A brute-force lock protects the password, not the IdP sign-in.
        return None
    return f"status_{user.status}"


async def resolve_user(
    db: AsyncSession,
    *,
    provider: SsoProvider,
    id_token: IdTokenClaims,
    claims: Mapping[str, Any],
    intent: Intent = "login",
    link_user_id: int | None = None,
) -> ResolvedAccount:
    """The account to sign in for a validated ID token at ``provider``.

    ``claims`` are the ID token claims merged with userinfo; they supply the
    profile values (email). ``intent`` and ``link_user_id`` come from the
    login transaction. Raises ``sso_account_not_provisioned`` or
    ``sso_account_disabled``.
    """
    sub_hash = subject_hash(id_token.subject)
    if intent != "login":
        logger.warning("sso.account.refused provider=%s sub=%s reason=intent_%s", provider.slug, sub_hash, intent)
        raise SsoAccountNotProvisionedError(f"intent_{intent}_unsupported")

    row = (
        await db.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(UserIdentity.issuer == id_token.issuer, UserIdentity.subject == id_token.subject)
        )
    ).first()
    if row is None:
        logger.info("sso.account.not_provisioned provider=%s sub=%s", provider.slug, sub_hash)
        raise SsoAccountNotProvisionedError("no_identity")
    identity, user = row

    if identity.provider_id != provider.id:
        # (iss, sub) is unique, so this is a second provider row with the same
        # issuer: the identity belongs to the other registration.
        logger.warning("sso.account.refused provider=%s sub=%s reason=other_provider", provider.slug, sub_hash)
        raise SsoAccountNotProvisionedError("identity_of_other_provider")

    reason = _refusal_reason(user)
    if reason is not None:
        logger.warning(
            "sso.account.refused provider=%s sub=%s user_id=%s reason=%s", provider.slug, sub_hash, user.id, reason
        )
        raise SsoAccountDisabledError(reason)

    now = utcnow()
    user.last_login_at = now
    identity.last_login_at = now
    email = _email_from(claims, provider)
    if email is not None:
        identity.email_at_last_login = email
    await db.flush()
    return ResolvedAccount(user=user, identity=identity)
