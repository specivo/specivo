"""Pure rules of suggested links: the per-project threshold, the stored order of a pair, and the link
type an accept may use for each kind of pair."""

from __future__ import annotations

import pytest

from specivo.core.exceptions import ValidationError
from specivo.models.link_suggestion import pair_key
from specivo.services.link_suggestion_service import THRESHOLD_SETTING, check_link_type, similarity_threshold

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, 0.80),
        ({}, 0.80),
        ({THRESHOLD_SETTING: 0.9}, 0.9),
        ({THRESHOLD_SETTING: 1}, 0.99),  # clamped to the bounds a PATCH accepts
        ({THRESHOLD_SETTING: 0.1}, 0.5),
        ({THRESHOLD_SETTING: "0.9"}, 0.80),  # not a number: the default
        ({THRESHOLD_SETTING: True}, 0.80),
    ],
)
def test_threshold(settings, expected):
    assert similarity_threshold(settings) == pytest.approx(expected)


def test_a_pair_is_stored_in_one_order():
    issue, page = ("issue", 9), ("wiki_page", 2)
    assert pair_key(issue, page) == pair_key(page, issue) == (issue, page)
    assert pair_key(("issue", 5), ("issue", 3)) == (("issue", 3), ("issue", 5))


def test_link_types_fit_the_pair():
    issue, other, page = ("issue", 1), ("issue", 2), ("wiki_page", 3)
    assert check_link_type(issue, other, None) == "relates"
    assert check_link_type(issue, other, "blocked") == "blocked"
    assert check_link_type(issue, page, None) == "see_also"
    assert check_link_type(page, issue, "documents") == "documents"
    assert check_link_type(page, ("wiki_page", 4), "supersedes") == "supersedes"
    for args in ((issue, other, "documents"), (issue, page, "relates"), (page, page, "bogus")):
        with pytest.raises(ValidationError):
            check_link_type(*args)
