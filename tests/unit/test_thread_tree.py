"""Unit tests for journal thread tree builder."""

import pytest

from specivo.web.thread_tree import build_thread_tree


@pytest.mark.unit
class TestBuildThreadTree:
    def test_flat_journals_no_replies(self):
        journals = [
            _fake(id=1, reply_to_id=None),
            _fake(id=2, reply_to_id=None),
        ]
        tree = build_thread_tree(journals)
        assert len(tree) == 2
        assert tree[0]["journal"].id == 1
        assert tree[0]["replies"] == []

    def test_single_reply(self):
        journals = [
            _fake(id=1, reply_to_id=None),
            _fake(id=2, reply_to_id=1),
        ]
        tree = build_thread_tree(journals)
        assert len(tree) == 1
        assert len(tree[0]["replies"]) == 1
        assert tree[0]["replies"][0]["journal"].id == 2

    def test_two_level_max_flattens(self):
        journals = [
            _fake(id=1, reply_to_id=None),
            _fake(id=2, reply_to_id=1),
            _fake(id=3, reply_to_id=2),  # reply-to-reply
        ]
        tree = build_thread_tree(journals)
        assert len(tree) == 1
        # Both replies at same level under parent
        assert len(tree[0]["replies"]) == 2

    def test_orphan_reply_becomes_root(self):
        journals = [
            _fake(id=2, reply_to_id=99),  # parent not in list
        ]
        tree = build_thread_tree(journals)
        assert len(tree) == 1  # treated as root


class _FakeJournal:
    def __init__(self, id, reply_to_id):
        self.id = id
        self.reply_to_id = reply_to_id


def _fake(id, reply_to_id):
    return _FakeJournal(id, reply_to_id)
