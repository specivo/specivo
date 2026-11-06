"""Exchange rates on the AI spend report page are rounded for reading; the API and CSV keep them exact."""

from decimal import Decimal

import pytest

from specivo.web.pages.ai_spend_reports import display_rate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("rate", "shown"),
    [
        ("0.0298151460942158616577221228", "0.0298151"),
        ("33.5400000000", "33.54"),
        ("32.412345678", "32.4123"),
        ("1234.5678", "1234.57"),
        ("152345.678", "152346"),
        ("0.0000391234567", "0.0000391235"),
        ("1", "1"),
        ("0.5", "0.5"),
        ("0", "0"),
    ],
)
def test_rates_are_rounded_to_six_significant_digits_in_plain_notation(rate: str, shown: str) -> None:
    assert display_rate(Decimal(rate)) == shown


def test_never_uses_an_exponent() -> None:
    for rate in ("0.00000001234567", "123456789.5", "1E-7"):
        assert "E" not in display_rate(Decimal(rate)).upper()
