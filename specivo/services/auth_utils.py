"""Password utilities: bcrypt hashing, verification and the password policy.

Uses the `bcrypt` library (not passlib) to avoid compatibility issues
with bcrypt 5.x and the deprecated `crypt` module.

``validate_password_policy`` is the single definition of what makes a new
password acceptable. Every endpoint that sets a password (reset via token,
self-service change) calls it — never re-implement the check inline.
"""

import bcrypt

from specivo.core.config import get_settings
from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _


def hash_password(password: str) -> str:
    """Hash a plain-text password using bcrypt.

    Returns a bcrypt hash string (``$2b$12$...``).
    Truncates to 72 bytes (bcrypt limit) silently — callers should
    validate max length via Pydantic schema.
    """
    settings = get_settings()
    pwd_bytes = password.encode("utf-8")[:72]  # bcrypt 72-byte limit
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """Verify a plain-text password against a stored bcrypt hash.

    Uses constant-time comparison (bcrypt.checkpw does this internally).

    A missing hash (``None`` or empty) never matches: service accounts have
    ``password_hash = None`` and must not be verifiable by any input.
    """
    if not hashed_password:
        return False
    pwd_bytes = plain_password.encode("utf-8")[:72]
    hash_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(pwd_bytes, hash_bytes)


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


def password_needs_rehash(hashed_password: str) -> bool:
    """Check if hash uses outdated cost factor.

    Extracts the rounds from the stored hash and compares with configured rounds.
    """
    settings = get_settings()
    # bcrypt hash format: $2b$12$... — rounds are between the 2nd and 3rd $
    parts = hashed_password.split("$")
    if len(parts) >= 3:
        stored_rounds = int(parts[2])
        return stored_rounds != settings.bcrypt_rounds
    return True  # Unknown format — rehash
