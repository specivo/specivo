"""``specivo.core.secret_box``: purpose-bound encryption of secrets stored in the database."""

from __future__ import annotations

import pytest

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.secret_box import SecretUnreadableError

pytestmark = pytest.mark.unit

_KEY_A = "unit-test-secret-key-a-0123456789abcdef0123456789"
_KEY_B = "unit-test-secret-key-b-0123456789abcdef0123456789"
_PLAINTEXT = "client-secret-value-0123456789"


def test_round_trip_with_explicit_key() -> None:
    token = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    assert secret_box.decrypt(token, "sso-client-secret", secret_key=_KEY_A) == _PLAINTEXT


def test_round_trip_with_the_application_secret_key() -> None:
    token = secret_box.encrypt(_PLAINTEXT, "sso-client-secret")
    assert secret_box.decrypt(token, "sso-client-secret") == _PLAINTEXT
    assert secret_box.decrypt(token, "sso-client-secret", secret_key=get_settings().secret_key) == _PLAINTEXT


def test_round_trip_preserves_unicode() -> None:
    value = 'secret with spaces, quotes " and non-ASCII: สวัสดี'
    token = secret_box.encrypt(value, "sso-client-secret", secret_key=_KEY_A)
    assert secret_box.decrypt(token, "sso-client-secret", secret_key=_KEY_A) == value


def test_each_encryption_is_randomised() -> None:
    first = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    second = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    assert first != second


def test_ciphertext_never_contains_the_plaintext() -> None:
    token = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    assert isinstance(token, str)
    assert _PLAINTEXT not in token
    assert _PLAINTEXT.encode() not in token.encode()


def test_a_different_purpose_cannot_decrypt() -> None:
    token = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    with pytest.raises(SecretUnreadableError):
        secret_box.decrypt(token, "sso-id-token", secret_key=_KEY_A)


def test_purposes_derive_different_keys() -> None:
    assert secret_box.derive_key(_KEY_A, "sso-client-secret") != secret_box.derive_key(_KEY_A, "webhook-secret")
    assert secret_box.derive_key(_KEY_A, "sso-client-secret") == secret_box.derive_key(_KEY_A, "sso-client-secret")


def test_a_different_secret_key_raises_secret_unreadable() -> None:
    token = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    with pytest.raises(SecretUnreadableError) as excinfo:
        secret_box.decrypt(token, "sso-client-secret", secret_key=_KEY_B)
    # The error names the purpose but never echoes the token or the plaintext.
    message = str(excinfo.value)
    assert "sso-client-secret" in message
    assert token not in message
    assert _PLAINTEXT not in message


@pytest.mark.parametrize("garbage", ["", "not-a-fernet-token", "gAAAAA" + "x" * 40, "ünïcode"])
def test_garbage_input_raises_secret_unreadable(garbage: str) -> None:
    with pytest.raises(SecretUnreadableError):
        secret_box.decrypt(garbage, "sso-client-secret", secret_key=_KEY_A)


def test_secret_unreadable_is_not_a_value_error_leak() -> None:
    # Callers catch exactly this type to show "secret unreadable, re-enter it".
    assert issubclass(SecretUnreadableError, Exception)
    token = secret_box.encrypt(_PLAINTEXT, "sso-client-secret", secret_key=_KEY_A)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretUnreadableError):
        secret_box.decrypt(tampered, "sso-client-secret", secret_key=_KEY_A)


@pytest.mark.parametrize("purpose", ["", "Upper", "has space", "x" * 65])
def test_invalid_purpose_labels_are_rejected(purpose: str) -> None:
    with pytest.raises(ValueError):
        secret_box.encrypt(_PLAINTEXT, purpose, secret_key=_KEY_A)
    with pytest.raises(ValueError):
        secret_box.derive_key(_KEY_A, purpose)
