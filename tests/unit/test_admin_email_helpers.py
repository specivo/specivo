"""Unit tests for admin email masking helpers."""

from __future__ import annotations

import pytest

from specivo.web.pages.admin import mask_smtp_host, mask_smtp_user

pytestmark = pytest.mark.unit


class TestMaskSmtpHost:
    def test_multi_part_domain(self):
        assert mask_smtp_host("smtp.mail.example.com") == "s***.mail.example.com"

    def test_two_part_domain(self):
        assert mask_smtp_host("mail.example.com") == "m***.example.com"

    def test_single_word_host(self):
        assert mask_smtp_host("localhost") == "l********"

    def test_single_char_first_part(self):
        assert mask_smtp_host("a.example.com") == "a.example.com"

    def test_empty_string(self):
        assert mask_smtp_host("") == ""


class TestMaskSmtpUser:
    def test_long_username(self):
        result = mask_smtp_user("abcdefghijklmnop")
        assert result.startswith("abcd")
        assert result.endswith("nop")
        assert "*" in result
        assert len(result) == len("abcdefghijklmnop")

    def test_short_username(self):
        result = mask_smtp_user("abc")
        assert result == "a**"

    def test_exactly_seven_chars(self):
        result = mask_smtp_user("abcdefg")
        assert result == "a******"

    def test_eight_chars(self):
        result = mask_smtp_user("abcdefgh")
        assert result == "abcd*fgh"

    def test_empty_string(self):
        # Edge case: empty user means unconfigured, but should not crash
        result = mask_smtp_user("")
        assert result == ""
