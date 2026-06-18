"""Unit tests for the assignee rotation helper.

Pure logic — no DB, no fixtures.
"""

from __future__ import annotations

import pytest

from specivo.services.recurrence.rotation import next_assignee

pytestmark = pytest.mark.unit


class TestNextAssignee:
    def test_round_robin_basic(self):
        cfg = {"user_ids": [3, 7, 9], "strategy": "round_robin"}
        assert next_assignee(cfg, 0) == (3, 1)
        assert next_assignee(cfg, 1) == (7, 2)
        assert next_assignee(cfg, 2) == (9, 3)

    def test_round_robin_wraps(self):
        cfg = {"user_ids": [3, 7, 9]}
        assert next_assignee(cfg, 3) == (3, 4)
        assert next_assignee(cfg, 4) == (7, 5)
        assert next_assignee(cfg, 5) == (9, 6)

    def test_large_index_wraps_by_modulo(self):
        cfg = {"user_ids": [10, 20]}
        assert next_assignee(cfg, 100) == (10, 101)
        assert next_assignee(cfg, 101) == (20, 102)

    def test_single_user(self):
        cfg = {"user_ids": [42]}
        assert next_assignee(cfg, 0) == (42, 1)
        assert next_assignee(cfg, 99) == (42, 100)

    def test_none_cfg_returns_none_and_unchanged_index(self):
        assert next_assignee(None, 4) == (None, 4)

    def test_empty_cfg_returns_none_and_unchanged_index(self):
        assert next_assignee({}, 7) == (None, 7)

    def test_missing_user_ids_returns_none_and_unchanged_index(self):
        assert next_assignee({"strategy": "round_robin"}, 2) == (None, 2)

    def test_empty_user_ids_returns_none_and_unchanged_index(self):
        assert next_assignee({"user_ids": []}, 5) == (None, 5)

    def test_unknown_strategy_defaults_to_round_robin(self):
        cfg = {"user_ids": [1, 2, 3], "strategy": "weighted_magic"}
        assert next_assignee(cfg, 0) == (1, 1)
        assert next_assignee(cfg, 1) == (2, 2)

    def test_index_always_advances_by_one(self):
        cfg = {"user_ids": [1, 2]}
        _, idx = next_assignee(cfg, 0)
        _, idx = next_assignee(cfg, idx)
        _, idx = next_assignee(cfg, idx)
        assert idx == 3
