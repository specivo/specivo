"""Unit tests for ReactionService."""

from __future__ import annotations

import pytest

from specivo.core.constants import REACTION_EMOJI


@pytest.mark.unit
class TestReactionEmoji:
    def test_emoji_dict_has_six_entries(self):
        assert len(REACTION_EMOJI) == 6

    def test_all_values_are_strings(self):
        for key, val in REACTION_EMOJI.items():
            assert isinstance(key, str)
            assert isinstance(val, str)

    def test_known_keys(self):
        assert "thumbs_up" in REACTION_EMOJI
        assert "rocket" in REACTION_EMOJI
        assert "heart" in REACTION_EMOJI
