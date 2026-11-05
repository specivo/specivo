"""How a source number is named, and which numbers a search query names."""

from __future__ import annotations

import pytest

from specivo.services.issue_source_ref_service import source_ref_label
from specivo.services.search_service import extract_source_numbers

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("key", "label"),
    [("123", "RT #123"), ("PROJ-7", "PROJ-7"), ("12a", "12a"), ("٣", "٣")],
)
def test_the_label_rule(key: str, label: str) -> None:
    assert source_ref_label("RT", key) == label


@pytest.mark.parametrize(
    ("query", "numbers"),
    [
        ("RT #123", [("rt", "123")]),
        ("#123", [(None, "123")]),
        ("broken RT #123 printer", [("rt", "123")]),
        ("#1 #2", [(None, "1"), (None, "2")]),
        ("rt #5 RT #5", [("rt", "5")]),
        ("ACME-42 #7", [(None, "7")]),  # an identifier is no system name
        ("123", []),
        ("repo#123", []),
        ("&#123;", []),
        ("##123", []),
        ("#123abc", []),
        ("#١٢٣", []),
    ],
)
def test_the_numbers_a_query_names(query: str, numbers: list) -> None:
    assert extract_source_numbers(query) == numbers


def test_the_lookup_is_bounded() -> None:
    assert len(extract_source_numbers(" ".join(f"#{n}" for n in range(50)))) == 10


def test_a_hostile_query_stays_cheap() -> None:
    """Hyphenated words offer the optional word group many places to start."""
    extract_source_numbers("a-" * 500)
