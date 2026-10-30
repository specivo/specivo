"""Issue references in commit messages and branch names (``specivo.hooks.refs``)."""

from __future__ import annotations

import time

import pytest

from specivo.hooks.refs import Ref, extract_branch_refs, extract_refs

pytestmark = pytest.mark.unit

KEYS = frozenset({"ACME", "WEB2"})


def _pairs(refs: list[Ref]) -> list[tuple[str, str]]:
    return [(r.display_key, r.keyword) for r in refs]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ACME-1 tidy the parser", [("ACME-1", "refs")]),
        ("Tidy the parser (#ACME-12)", [("ACME-12", "refs")]),
        ("refs #ACME-1", [("ACME-1", "refs")]),
        ("References ACME-1", [("ACME-1", "refs")]),
        ("ref: ACME-1", [("ACME-1", "refs")]),
        ("See ACME-1", [("ACME-1", "refs")]),
        ("Fixes ACME-1", [("ACME-1", "fixes")]),
        ("fixed #ACME-1", [("ACME-1", "fixes")]),
        ("fix: ACME-1", [("ACME-1", "fixes")]),
        ("Closes ACME-1", [("ACME-1", "fixes")]),
        ("closed ACME-1", [("ACME-1", "fixes")]),
        ("close ACME-1", [("ACME-1", "fixes")]),
        ("Resolves ACME-1", [("ACME-1", "fixes")]),
        ("resolved ACME-1", [("ACME-1", "fixes")]),
        ("RESOLVE ACME-1", [("ACME-1", "fixes")]),
        ("Fixes ACME-1, ACME-2 and #ACME-3", [("ACME-1", "fixes"), ("ACME-2", "fixes"), ("ACME-3", "fixes")]),
        ("Fixes ACME-1; also touches ACME-2", [("ACME-1", "fixes"), ("ACME-2", "refs")]),
        ("ACME-1 and WEB2-7", [("ACME-1", "refs"), ("WEB2-7", "refs")]),
        ("ACME-1, later fixes ACME-1", [("ACME-1", "fixes")]),
        ("Merge ACME-1\n\nCloses ACME-2", [("ACME-1", "refs"), ("ACME-2", "fixes")]),
        ("prefix fixes ACME-1", [("ACME-1", "fixes")]),
        ("suffixes ACME-1", [("ACME-1", "refs")]),
    ],
)
def test_extract_refs(message: str, expected: list[tuple[str, str]]) -> None:
    assert _pairs(extract_refs(message, KEYS)) == expected


@pytest.mark.parametrize(
    "message",
    [
        "",
        "no references here",
        "acme-1 lowercase key in a message",
        "Acme-1 mixed case",
        "OTHER-1 out of scope",
        "HTTP-200 is a status, not an issue",
        "see https://example.com/browse/ACME-1 for context",
        "path src/ACME-1/readme",
        "XACME-1 longer word",
        "ACME-1x glued suffix",
        "ACME_1 underscore",
        "ACME-0 is never an issue number",
        "ACME- missing number",
    ],
)
def test_extract_refs_finds_nothing(message: str) -> None:
    assert extract_refs(message, KEYS) == []


def test_extract_refs_keeps_number_as_int() -> None:
    (ref,) = extract_refs("ACME-007", KEYS)
    assert (ref.key, ref.number, ref.display_key) == ("ACME", 7, "ACME-7")


def test_extract_refs_with_no_known_keys() -> None:
    assert extract_refs("Fixes ACME-1", frozenset()) == []


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("ACME-12", ["ACME-12"]),
        ("feat/ACME-12-slug", ["ACME-12"]),
        ("fix/ACME-12", ["ACME-12"]),
        ("ACME-12/slug", ["ACME-12"]),
        ("key-12-slug".replace("key", "acme"), ["ACME-12"]),
        ("feature/acme-12-add-login", ["ACME-12"]),
        ("users/someone/WEB2-3-thing", ["WEB2-3"]),
        ("ACME-1-and-ACME-2", ["ACME-1", "ACME-2"]),
    ],
)
def test_extract_branch_refs(branch: str, expected: list[str]) -> None:
    refs = extract_branch_refs(branch, KEYS)
    assert [r.display_key for r in refs] == expected
    assert all(r.keyword == "refs" for r in refs)


@pytest.mark.parametrize(
    "branch",
    ["main", "release/1.2", "OTHER-12-slug", "hotfix-12", "feat/acmex-12", "v2-ACME", ""],
)
def test_extract_branch_refs_finds_nothing(branch: str) -> None:
    assert extract_branch_refs(branch, KEYS) == []


def test_long_blank_gap_is_parsed_in_linear_time() -> None:
    # Two adjacent blank quantifiers used to make this quadratic: a commit
    # message could keep a worker busy for hours.
    message = "fix" + " " * 200_000 + "x\nKEY-1"
    started = time.perf_counter()
    refs = extract_refs(message, {"KEY"})
    assert time.perf_counter() - started < 1.0
    assert _pairs(refs) == [("KEY-1", "refs")]


@pytest.mark.parametrize("message", ["KEY-" + "9" * 5000, "KEY-9999999999", "fixes KEY-12345678901"])
def test_numbers_too_long_for_an_issue_are_not_references(message: str) -> None:
    assert extract_refs(message, {"KEY"}) == []


def test_branch_numbers_too_long_for_an_issue_are_not_references() -> None:
    assert extract_branch_refs("feat/key-" + "9" * 5000, {"KEY"}) == []
    assert extract_branch_refs("feat/KEY-999999999-slug", {"KEY"})[0].number == 999_999_999
