"""Unit tests for the safe_int helper."""

import pytest

from specivo.core.utils import safe_int


@pytest.mark.unit
class TestSafeInt:
    def test_valid_int_string(self):
        assert safe_int("42") == 42

    def test_valid_negative(self):
        assert safe_int("-1") == -1

    def test_zero(self):
        assert safe_int("0") == 0

    def test_actual_int(self):
        assert safe_int(5) == 5

    def test_empty_string_returns_default(self):
        assert safe_int("") is None

    def test_none_returns_default(self):
        assert safe_int(None) is None

    def test_whitespace_returns_default(self):
        assert safe_int("  ") is None

    def test_non_numeric_returns_default(self):
        assert safe_int("abc") is None

    def test_float_string_returns_default(self):
        assert safe_int("3.14") is None

    def test_sql_injection_returns_default(self):
        assert safe_int("1; DROP TABLE users") is None

    def test_custom_default(self):
        assert safe_int("", default=0) == 0
        assert safe_int("bad", default=-1) == -1

    def test_html_injection_returns_default(self):
        assert safe_int("<script>alert(1)</script>") is None
