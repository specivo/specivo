"""The labelled file of the suggestion evaluation: JSON or 'ISSUE: page, page' lines; and the report maths."""

from __future__ import annotations

import pytest

from specivo.services.suggestion_eval import Case, CaseResult, Report, parse_labels

pytestmark = [pytest.mark.unit]


def test_lines_with_comments_and_blank_lines():
    raw = "# issue: pages\n\nACME-1: runbook, wiki:ACME/deploy-guide\n  ACME-2 :architecture  \n"
    assert parse_labels(raw) == [
        Case("ACME-1", ["runbook", "wiki:ACME/deploy-guide"]),
        Case("ACME-2", ["architecture"]),
    ]


def test_json_object_and_list():
    assert parse_labels('{"ACME-1": ["runbook"]}') == [Case("ACME-1", ["runbook"])]
    assert parse_labels('[{"issue": "ACME-2", "pages": ["a", "b"]}]') == [Case("ACME-2", ["a", "b"])]


@pytest.mark.parametrize("raw", ["ACME-1 runbook", "ACME-1:", '{"ACME-1": []}', '{"ACME-1": "runbook"}', "3"])
def test_bad_labels_are_refused(raw):
    with pytest.raises(ValueError):
        parse_labels(raw)


def _case(ref: str, expected: list[str], tops: dict[str, list[str]]) -> CaseResult:
    case = CaseResult(ref, expected)
    for scope in ("all", "wiki"):
        case.top[scope] = dict(tops)
        case.hits[scope] = {m: sum(1 for p in expected if p in top) for m, top in tops.items()}
    return case


def test_recall_is_the_mean_over_cases_and_lists_what_only_suggestions_found():
    one = _case(
        "ACME-1",
        ["wiki:ACME/a", "wiki:ACME/b"],
        {"chunk_mean": ["wiki:ACME/a"], "chunk_mean_threshold": [], "best_chunk": ["wiki:ACME/b"], "search": []},
    )
    two = _case(
        "ACME-2",
        ["wiki:ACME/c"],
        {"chunk_mean": [], "chunk_mean_threshold": [], "best_chunk": [], "search": ["wiki:ACME/c"]},
    )
    skipped = CaseResult("ACME-3", [], error="issue not found")
    report = Report(k=5, model="mock", cases=[one, two, skipped])
    assert report.recall("wiki", "chunk_mean") == pytest.approx(0.25)
    assert report.recall("wiki", "search") == pytest.approx(0.5)
    assert report.found_only_by_suggestions("all") == [("ACME-1", "wiki:ACME/a"), ("ACME-1", "wiki:ACME/b")]
    text = report.as_text()
    assert "chunk_mean             0.250" in text
    assert "ACME-3: skipped (issue not found)" in text
    assert report.as_dict()["evaluated"] == 2
