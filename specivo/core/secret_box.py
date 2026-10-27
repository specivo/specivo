"""Purpose-bound symmetric encryption for secrets stored in the database.

Secrets that Specivo has to read back (an SSO client secret, an ID token kept
for ``id_token_hint``) are stored as Fernet tokens. The Fernet key is never
configured on its own: it is derived from ``SECRET_KEY`` with HKDF-SHA256 and
a *purpose* label, so every kind of secret gets an independent key. A token
written for one purpose cannot be decrypted as another, and leaking one
derived key reveals nothing about the others or about ``SECRET_KEY``.

Rotating ``SECRET_KEY`` makes every stored token unreadable. ``decrypt`` then
raises :class:`SecretUnreadableError`; callers turn that into "secret
unreadable, re-enter it" instead of a server error.
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from specivo.core.config import get_settings

__all__ = ["SecretUnreadableError", "decrypt", "derive_key", "encrypt"]

# Fixed, public HKDF salt. It only separates this use of SECRET_KEY from any
# other HKDF use; the purpose label goes into ``info``.
_HKDF_SALT = b"specivo-secret-box-v1"
_PURPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SecretUnreadableError(Exception):
    """A stored secret cannot be decrypted with the current key and purpose.

    Raised for a rotated ``SECRET_KEY``, a token written for another purpose,
    and tampered or malformed input. The message never contains the token.
    """

    def __init__(self, purpose: str) -> None:
        super().__init__(f"stored secret for purpose {purpose!r} cannot be decrypted with the current SECRET_KEY")
        self.purpose = purpose


def _check_purpose(purpose: str) -> None:
    if not _PURPOSE_RE.fullmatch(purpose):
        raise ValueError("purpose must be 1-64 lowercase letters, digits or '-', starting with a letter or digit")


@lru_cache(maxsize=32)
def _fernet(secret_key: str, purpose: str) -> Fernet:
    return Fernet(derive_key(secret_key, purpose))


def derive_key(secret_key: str, purpose: str) -> bytes:
    """Return the urlsafe-base64 Fernet key for ``purpose`` derived from ``secret_key``."""
    _check_purpose(purpose)
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=b"specivo:" + purpose.encode("ascii"),
    ).derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str, purpose: str, *, secret_key: str | None = None) -> str:
    """Encrypt ``plaintext`` for ``purpose``; returns an ASCII Fernet token.

    ``secret_key`` defaults to the application ``SECRET_KEY``.
    """
    _check_purpose(purpose)
    key = secret_key if secret_key is not None else get_settings().secret_key
    return _fernet(key, purpose).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, purpose: str, *, secret_key: str | None = None) -> str:
    """Decrypt a token produced by :func:`encrypt` for the same ``purpose``.

    Raises :class:`SecretUnreadableError` when the token cannot be read with
    the current key (rotated ``SECRET_KEY``, other purpose, tampering).
    """
    _check_purpose(purpose)
    key = secret_key if secret_key is not None else get_settings().secret_key
    try:
        return _fernet(key, purpose).decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        raise SecretUnreadableError(purpose) from None
