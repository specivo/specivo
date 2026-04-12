"""Unit tests for attachment content hash calculation."""

from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.unit


class TestContentHash:
    def test_sha256_hex_length(self):
        """SHA-256 hex digest is always 64 characters."""
        digest = hashlib.sha256(b"test content").hexdigest()
        assert len(digest) == 64

    def test_same_content_same_hash(self):
        """Identical content produces identical hash."""
        content = b"duplicate file content"
        assert hashlib.sha256(content).hexdigest() == hashlib.sha256(content).hexdigest()

    def test_different_content_different_hash(self):
        """Different content produces different hash."""
        h1 = hashlib.sha256(b"file A").hexdigest()
        h2 = hashlib.sha256(b"file B").hexdigest()
        assert h1 != h2

    def test_empty_file_has_hash(self):
        """Even an empty file has a deterministic hash."""
        digest = hashlib.sha256(b"").hexdigest()
        assert len(digest) == 64
        # Known SHA-256 of empty input
        assert digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_hash_is_lowercase_hex(self):
        """Hash is lowercase hexadecimal."""
        digest = hashlib.sha256(b"any content").hexdigest()
        assert digest == digest.lower()
        assert all(c in "0123456789abcdef" for c in digest)
