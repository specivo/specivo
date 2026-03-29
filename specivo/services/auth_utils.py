"""Password hashing utilities using bcrypt directly.

Uses the `bcrypt` library (not passlib) to avoid compatibility issues
with bcrypt 5.x and the deprecated `crypt` module.
"""

import bcrypt

from specivo.core.config import get_settings


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


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a stored bcrypt hash.

    Uses constant-time comparison (bcrypt.checkpw does this internally).
    """
    pwd_bytes = plain_password.encode("utf-8")[:72]
    hash_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(pwd_bytes, hash_bytes)


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
