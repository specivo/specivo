"""Tests for app/services/auth_utils.py.

Pure unit tests — no database required. All tests are synchronous.
"""

from specivo.services.auth_utils import hash_password, password_needs_rehash, verify_password


class TestHashPassword:
    def test_returns_string(self):
        result = hash_password("mypassword123")
        assert isinstance(result, str)

    def test_bcrypt_format(self):
        """Hash must start with the bcrypt modular crypt identifier."""
        result = hash_password("mypassword123")
        # bcrypt hashes start with $2b$ (or $2a$ on some platforms)
        assert result.startswith("$2"), f"Unexpected hash format: {result[:6]}"

    def test_hash_is_not_plain_text(self):
        password = "mypassword123"
        result = hash_password(password)
        assert password not in result

    def test_different_calls_produce_different_salts(self):
        """bcrypt salts must be random — two hashes of the same password differ."""
        h1 = hash_password("samepassword")
        h2 = hash_password("samepassword")
        assert h1 != h2

    def test_minimum_length_password(self):
        """10-char password (policy minimum) must hash without error."""
        result = hash_password("a" * 10)
        assert result.startswith("$2")

    def test_long_password(self):
        """128-char password (policy maximum) must hash without error."""
        result = hash_password("x" * 128)
        assert result.startswith("$2")


class TestVerifyPassword:
    def test_correct_password_returns_true(self):
        password = "correct_password99"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_wrong_password_returns_false(self):
        hashed = hash_password("correct_password99")
        assert verify_password("wrong_password99", hashed) is False

    def test_empty_string_does_not_match(self):
        hashed = hash_password("some_password_here")
        assert verify_password("", hashed) is False

    def test_case_sensitive(self):
        """Passwords are case-sensitive — 'Password' != 'password'."""
        hashed = hash_password("Password1234")
        assert verify_password("password1234", hashed) is False
        assert verify_password("Password1234", hashed) is True

    def test_verify_is_consistent_across_calls(self):
        """Same inputs always produce the same result."""
        password = "consistent_test_pass"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True
        assert verify_password(password, hashed) is True

    def test_hash_from_different_salt_still_verifies(self):
        """Verification works regardless of which salt was used for hashing."""
        password = "salt_independence_test"
        h1 = hash_password(password)
        h2 = hash_password(password)
        # Both hashes verify the same password even though they differ
        assert verify_password(password, h1) is True
        assert verify_password(password, h2) is True


class TestPasswordNeedsRehash:
    def test_fresh_hash_does_not_need_rehash(self):
        """A hash created with current settings should not need upgrading."""
        hashed = hash_password("fresh_password99")
        assert password_needs_rehash(hashed) is False

    def test_low_rounds_hash_needs_rehash(self):
        """A hash created with fewer rounds than configured needs rehash."""
        import bcrypt as _bcrypt

        low_salt = _bcrypt.gensalt(rounds=4)
        low_cost_hash = _bcrypt.hashpw(b"upgrade_me", low_salt).decode("utf-8")
        assert password_needs_rehash(low_cost_hash) is True
