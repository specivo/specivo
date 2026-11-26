"""Pure rules of suggested links: the per-project threshold and the centred score it is checked on, the
stored order of a pair, and the link type an accept may use for each kind of pair."""

from __future__ import annotations

import pytest

from specivo.core.exceptions import ValidationError
from specivo.models.link_suggestion import pair_key
from specivo.services.link_suggestion_service import (
    THRESHOLD_SETTING,
    _Neighbour,
    _pick,
    check_link_type,
    similarity_threshold,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, 0.30),
        ({}, 0.30),
        ({THRESHOLD_SETTING: 0.5}, 0.5),
        ({THRESHOLD_SETTING: 0}, 0.0),
        ({THRESHOLD_SETTING: 1}, 0.95),  # clamped to the bounds a PATCH accepts
        ({THRESHOLD_SETTING: 0.8}, 0.8),  # a value stored before the scores were centred is kept
        ({THRESHOLD_SETTING: -0.2}, 0.0),
        ({THRESHOLD_SETTING: "0.9"}, 0.30),  # not a number: the default
        ({THRESHOLD_SETTING: True}, 0.30),
    ],
)
def test_threshold(settings, expected):
    assert similarity_threshold(settings) == pytest.approx(expected)


def test_pick_gates_on_the_centred_score_and_keeps_the_raw_one():
    root, typical, closer, page = ("issue", 1), ("issue", 2), ("issue", 3), ("wiki_page", 4)
    neighbours = [  # best raw score first, as the neighbour query returns them
        _Neighbour(root, 7, typical, score=0.97, centred=0.05),  # high raw, typical of the project: dropped
        _Neighbour(root, 7, closer, score=0.91, centred=0.45),
        _Neighbour(root, 7, page, score=0.60, centred=float("nan")),  # equal to the mean: never passes
    ]
    chosen = _pick(neighbours, {7: 0.30}, set())
    assert chosen == {pair_key(root, closer): (0.91, 7)}
    # The threshold is the project's: at 0.5 even the closer one is out.
    assert _pick(neighbours, {7: 0.5}, set()) == {}


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
