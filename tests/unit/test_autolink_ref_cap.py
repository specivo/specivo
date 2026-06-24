"""Unit tests for the autolink ref-candidate cap (stored-DoS guard)."""

from __future__ import annotations

from specivo.services.issue_service import MAX_AUTOLINK_REFS, IssueService


def test_pairs_capped_at_limit():
    # Far more distinct refs than the cap.
    text = " ".join(f"AAA-{i}" for i in range(1, MAX_AUTOLINK_REFS * 3))
    pairs = IssueService._autolink_ref_pairs((text,))
    assert len(pairs) == MAX_AUTOLINK_REFS


def test_pairs_deterministic_sorted_subset():
    text = " ".join(f"AAA-{i}" for i in range(1, MAX_AUTOLINK_REFS * 3))
    first = IssueService._autolink_ref_pairs((text,))
    second = IssueService._autolink_ref_pairs((text,))
    assert first == second  # stable across calls
    assert first == IssueService._autolink_ref_pairs((text,), limit=MAX_AUTOLINK_REFS)


def test_custom_limit_respected():
    text = " ".join(f"AAA-{i}" for i in range(1, 100))
    assert len(IssueService._autolink_ref_pairs((text,), limit=10)) == 10


def test_below_cap_returns_all_distinct():
    pairs = IssueService._autolink_ref_pairs(("See ACME-1 and BCME-2 and ACME-1 again",))
    assert sorted(pairs) == [("ACME", 1), ("BCME", 2)]


def test_combines_multiple_texts():
    pairs = IssueService._autolink_ref_pairs(("ACME-1", None, "BCME-2"))
    assert sorted(pairs) == [("ACME", 1), ("BCME", 2)]


def test_empty_texts():
    assert IssueService._autolink_ref_pairs((None, "")) == []
