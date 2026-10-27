"""Password utilities: hashing, verification, upgrading and the password policy.

Hashing goes through the hasher registry in
``specivo.services.password_hashers``: new hashes are always bcrypt, and a hash
imported from another system is verified by the legacy hasher its prefix names,
then replaced with a bcrypt one on the first successful sign-in
(:func:`verify_and_update`).

``verify_password`` never raises. An unknown, malformed, disabled or expired
hash is simply "no match". Every verification, matching or not, costs at least
one bcrypt at ``BCRYPT_ROUNDS``, so the time a sign-in takes says nothing about
whether the account exists or how its hash is stored.

``validate_password_policy`` is the single definition of what makes a new
password acceptable. Every endpoint that sets a password (reset via token,
self-service change) calls it — never re-implement the check inline.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from specivo.core.config import get_settings
from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.services.password_hashers import (
    bcrypt_cost_spent,
    get_password_hasher_registry,
    is_wellformed_bcrypt,
    start_cost_tracking,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from specivo.models.user import User

logger = logging.getLogger(__name__)

# A bcrypt hash of a value nobody knows, at the configured cost, checked
# whenever a verification fails without running a real bcrypt: a missing,
# unknown, disabled or expired hash. Cached per cost so a changed
# ``bcrypt_rounds`` is followed.
_burn_hashes: dict[int, str] = {}

# Unusable hashes are logged once per (account, reason), not on every attempt,
# so a stray value cannot flood the log. Bounded: past the cap it stops adding.
_WARNED_CAP = 10_000
_warned: set[tuple[int, str]] = set()


def _warn_once(user_id: int | None, reason: str, algorithm: str | None) -> None:
    if user_id is not None:
        key = (user_id, reason)
        if key in _warned:
            return
        if len(_warned) < _WARNED_CAP:
            _warned.add(key)
    # The hash itself is never logged; the algorithm id is enough to act on.
    logger.warning("Password hash for user %s not verified: %s (algorithm=%s)", user_id, reason, algorithm)


def burn_password_check(password: str) -> None:
    """Spend one bcrypt at the configured cost, for a sign-in with no account.

    Used when there is no hash to check at all (an unknown login), so the
    response takes as long as a real wrong-password attempt.
    """
    _burn_bcrypt(password)


def _burn_bcrypt(password: str) -> None:
    """Spend one bcrypt at the configured cost and discard the result."""
    registry = get_password_hasher_registry()
    preferred = registry.preferred
    rounds = get_settings().bcrypt_rounds
    burn = _burn_hashes.get(rounds)
    if burn is None:
        burn = _burn_hashes[rounds] = preferred.encode(secrets.token_urlsafe(16))
    preferred.verify(password, burn)


def hash_password(password: str) -> str:
    """Hash a plain-text password with the preferred hasher (bcrypt).

    Returns a bcrypt hash string (``$2b$12$...``).
    Truncates to 72 bytes (bcrypt limit) silently — callers should
    validate max length via Pydantic schema.
    """
    return get_password_hasher_registry().preferred.encode(password)


def is_legacy_hash(hashed_password: str | None) -> bool:
    """Return whether *hashed_password* is stored under a non-native algorithm."""
    registry = get_password_hasher_registry()
    algorithm = registry.algorithm_of(hashed_password)
    return algorithm is not None and algorithm != registry.preferred.algorithm


def legacy_hashes_expired() -> bool:
    """Return whether ``LEGACY_PASSWORD_HASHES_EXPIRE_AT`` has passed."""
    cutoff = get_settings().legacy_password_hashes_expire_at
    return cutoff is not None and utcnow() >= cutoff


def verify_password(plain_password: str, hashed_password: str | None, *, user_id: int | None = None) -> bool:
    """Verify a plain-text password against a stored hash. Never raises.

    The hasher is picked by the hash's prefix. A native bcrypt hash always
    verifies normally. A legacy hash verifies only when its algorithm is
    registered, listed in ``LEGACY_PASSWORD_ALGORITHMS`` and the cutoff
    (``LEGACY_PASSWORD_HASHES_EXPIRE_AT``) has not passed.

    A missing hash (``None`` or empty) never matches: service accounts have
    ``password_hash = None`` and must not be verifiable by any input. Every
    path that does not reach a real bcrypt check runs a throwaway one, so a
    failure takes the same time whatever the reason. *user_id* is used only
    to log an unusable hash once per account.
    """
    if not hashed_password:
        _burn_bcrypt(plain_password)
        return False

    registry = get_password_hasher_registry()
    algorithm = registry.algorithm_of(hashed_password)
    hasher = registry.identify(hashed_password)

    if hasher is None:
        _warn_once(user_id, "unrecognised hash format" if algorithm is None else "unregistered algorithm", algorithm)
        _burn_bcrypt(plain_password)
        return False

    if hasher is registry.preferred:
        if not is_wellformed_bcrypt(hashed_password):
            _warn_once(user_id, "malformed bcrypt hash", algorithm)
            _burn_bcrypt(plain_password)
            return False
    else:
        if algorithm not in get_settings().legacy_password_algorithms:
            _warn_once(user_id, "algorithm not enabled in LEGACY_PASSWORD_ALGORITHMS", algorithm)
            _burn_bcrypt(plain_password)
            return False
        if legacy_hashes_expired():
            _burn_bcrypt(plain_password)
            return False

    start_cost_tracking()
    try:
        matched = bool(hasher.verify(plain_password, hashed_password))
    except Exception:  # noqa: BLE001 - a third-party hasher must never turn a sign-in into a 500
        logger.exception("Password hasher %r raised while verifying user %s; treated as no match", algorithm, user_id)
        matched = False
    # The timing floor, enforced here so no hasher can opt out: a check that
    # did not spend one bcrypt at the configured cost (PBKDF2, a cheaper
    # bcrypt, a plugin doing its own thing, a hasher that bailed out early)
    # is padded with a throwaway one. Nothing answers faster than a native
    # account, so response times reveal neither existence nor legacy status.
    if bcrypt_cost_spent() < get_settings().bcrypt_rounds:
        _burn_bcrypt(plain_password)
    return matched


def password_needs_rehash(hashed_password: str | None) -> bool:
    """Return whether *hashed_password* should be replaced by a fresh preferred hash.

    True for any legacy hash, for a bcrypt hash at a cost other than
    ``BCRYPT_ROUNDS`` (or under a non-native prefix), and for anything
    unrecognised. Only meaningful right after the password verified.
    """
    if not hashed_password:
        return False
    registry = get_password_hasher_registry()
    hasher = registry.identify(hashed_password)
    if hasher is None or hasher is not registry.preferred:
        return True
    return hasher.must_update(hashed_password)


def hash_upgrade_source(hashed_password: str | None) -> str | None:
    """Return the algorithm *hashed_password* would be upgraded from, if it needs it."""
    if not password_needs_rehash(hashed_password):
        return None
    return get_password_hasher_registry().algorithm_of(hashed_password) or "unknown"


def rehash_if_needed(user: User, password: str) -> str | None:
    """Replace *user*'s hash with a preferred one if it needs it.

    Call only once *password* has verified against the stored hash. Returns
    the algorithm the account was on when it was upgraded, ``None`` when
    nothing changed. ``password_changed_at`` is deliberately left alone: the
    password is the same, only how it is stored changed.
    """
    from_algorithm = hash_upgrade_source(user.password_hash)
    if from_algorithm is not None:
        user.password_hash = hash_password(password)
    return from_algorithm


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """Outcome of :func:`verify_and_update`."""

    matched: bool
    # The algorithm the hash was upgraded from, or ``None`` if it was not.
    upgraded_from: str | None = None

    def __bool__(self) -> bool:
        return self.matched


def verify_and_update(user: User, password: str) -> PasswordCheck:
    """Verify *password* for *user* and upgrade the stored hash on success.

    Django's ``check_password(setter=...)``: if the hash verifies and
    :func:`password_needs_rehash` says so, ``user.password_hash`` is replaced
    in the caller's session, so the upgrade commits or rolls back with
    whatever the caller does next. The caller writes the audit row.
    """
    if not verify_password(password, user.password_hash, user_id=user.id):
        return PasswordCheck(matched=False)
    return PasswordCheck(matched=True, upgraded_from=rehash_if_needed(user, password))


def validate_password_policy(new_password: str, *, field: str = "new_password") -> None:
    """Validate *new_password* against the configured password policy.

    Raises ``AppError`` (422) when the password is unacceptable. This is the
    only place the policy is expressed; callers must not duplicate it.
    """
    settings = get_settings()
    if len(new_password) < settings.password_min_length:
        raise AppError(
            code="validation_error",
            message=_("Password must be at least %(count)d characters") % {"count": settings.password_min_length},
            status_code=422,
            field=field,
        )


async def is_sso_only_user(session: AsyncSession, user: User) -> bool:
    """``True`` for an account that signs in only through SSO: no password and a connected identity.

    The rules that depend on SSO (API keys for SSO users, no self-service
    password) use this one definition. A service account is never SSO-only: it
    cannot hold an identity. The cheap checks run first, so a password user
    or a service account costs no query.
    """
    if user.is_service_account or user.password_hash:
        return False
    from sqlalchemy import select

    from specivo.models.sso import UserIdentity

    stmt = select(UserIdentity.id).where(UserIdentity.user_id == user.id).limit(1)
    return (await session.execute(stmt)).first() is not None
