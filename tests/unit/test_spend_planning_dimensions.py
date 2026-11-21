"""The AI spend report's grouping keys: every dimension maps to an entry column or to a planning lookup."""

from __future__ import annotations

import pytest

from specivo.services.ai_spend_planning import PLANNING_DIMENSIONS, PlanningFilter
from specivo.services.ai_spend_report_service import _COLUMNS, GROUP_BY, ReportFilters, parse_group_by

pytestmark = pytest.mark.unit


def test_every_dimension_has_one_source() -> None:
    for dim in GROUP_BY:
        assert (dim in _COLUMNS) != (dim in PLANNING_DIMENSIONS), dim
    # The internal groupings never become public dimensions.
    assert "subscription" not in GROUP_BY and "entry" not in GROUP_BY


def test_release_and_sprint_lead_the_canonical_order() -> None:
    assert PLANNING_DIMENSIONS == ("version", "sprint")
    assert parse_group_by("user, sprint, version") == ("version", "sprint", "user")
    assert parse_group_by(["model", "version"]) == ("version", "model")


def test_planning_filters_narrow_and_leave_seat_fees_out() -> None:
    assert not ReportFilters().planning.active
    assert not ReportFilters().excludes_seats
    for filters in (
        ReportFilters(version_id=3),
        ReportFilters(without_version=True),
        ReportFilters(sprint_id=4),
        ReportFilters(without_sprint=True),
    ):
        assert filters.planning.active
        assert filters.excludes_seats
    assert ReportFilters(version_id=3, without_sprint=True).planning == PlanningFilter(
        version_id=3, without_sprint=True
    )
    assert PlanningFilter(version_id=3, without_sprint=True).wanted() == [("version", 3, False), ("sprint", None, True)]


def test_cache_parts_tell_release_and_sprint_filters_apart() -> None:
    keys = {
        str(f.cache_parts())
        for f in (
            ReportFilters(),
            ReportFilters(version_id=3),
            ReportFilters(sprint_id=3),
            ReportFilters(without_version=True),
            ReportFilters(without_sprint=True),
        )
    }
    assert len(keys) == 5
