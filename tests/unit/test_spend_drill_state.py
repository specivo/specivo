"""The drill-down state of the AI spend report pages: the URL form, the drill order and the colour slots."""

from __future__ import annotations

import pytest

from specivo.services.ai_usage_chart_service import NEUTRAL_SLOT, NO_PROJECT, OTHER, color_slots
from specivo.web.pages.ai_spend_reports import SESSIONS, ReportForm, next_group

pytestmark = pytest.mark.unit


def test_next_group_follows_the_drill_order() -> None:
    assert next_group("project", {"project"}, instance=True) == "user"
    assert next_group("user", {"project", "user"}, instance=True) == "issue"
    assert next_group("issue", {"issue"}, instance=True) == SESSIONS
    assert next_group("tool", {"tool"}, instance=True) == "user"
    assert next_group("tool", {"tool", "user"}, instance=True) == "issue"
    assert next_group("model", {"model", "user", "issue"}, instance=True) == SESSIONS
    assert next_group("provider", {"provider"}, instance=False) == "model"


def test_form_reads_the_drill_state_and_drops_junk() -> None:
    form = ReportForm.read(group="sessions", group2="user", project="rda", issue="rda-1", offset="25", limit="7")
    assert form.sessions and form.group2 == "" and form.group_by == ["model"]
    assert form.project == "RDA" and form.issue == "RDA-1"
    assert form.offset == 25 and form.limit == 25  # 7 is not a page size
    assert ReportForm.read(group="salary").group == "user"
    assert ReportForm.read(offset="-3").offset == 0
    # No grouping asked for: the scope decides.
    assert ReportForm.read(group=None).for_scope(instance=True).group == "project"
    assert ReportForm.read(group=None).for_scope(instance=False).group == "user"
    # A project report never takes a project filter.
    assert ReportForm.read(project="RDB").for_scope(instance=False).project == ""
    # The page only travels with the sessions level.
    assert "offset" not in ReportForm.read(group="user", offset="25").query()
    assert "offset=25" in ReportForm.read(group="sessions", offset="25").query()


def test_color_slots_rank_and_keep_grey_for_the_rest() -> None:
    keys = ["a", "b", NO_PROJECT, "c", "d", "e", "f", "g", "h", OTHER]
    slots = color_slots(keys)
    assert [slots[k] for k in ("a", "b", "c", "d", "e", "f", "g")] == [0, 1, 2, 3, 4, 5, 6]
    assert slots["h"] == slots[OTHER] == slots[NO_PROJECT] == NEUTRAL_SLOT
