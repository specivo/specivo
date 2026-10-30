"""The journal text for a linked commit, and the metadata append helper."""

from __future__ import annotations

import pytest

from specivo.models.repository import RepositoryCommit
from specivo.services.repository_hooks import append_unique, commit_journal_note, prune_payload

pytestmark = pytest.mark.unit

SHA = "0d1a26e67d8f5eaf1f6ba5c57fc3c7d91ac0fd1c"


def _commit(**kw) -> RepositoryCommit:
    values = {
        "sha": SHA,
        "message": "Add the login form\n\nDetails",
        "url": f"https://forge.example.com/o/r/commit/{SHA}",
        "author_name": "Alex Example",
        "branch": "main",
    } | kw
    return RepositoryCommit(**values)


def test_note_format() -> None:
    assert commit_journal_note(_commit()) == (
        f"Commit [`0d1a26e`](https://forge.example.com/o/r/commit/{SHA}) on `main` by Alex Example: Add the login form"
    )


def test_note_without_url_branch_or_author() -> None:
    assert commit_journal_note(_commit(url="", branch=None, author_name="")) == "Commit `0d1a26e`: Add the login form"


def test_unsafe_url_is_not_linked() -> None:
    assert commit_journal_note(_commit(url="javascript:alert(1)")).startswith("Commit `0d1a26e` on")


def test_parentheses_in_url_cannot_break_the_link() -> None:
    note = commit_journal_note(_commit(url="https://forge.example.com/a)(b"))
    assert "(https://forge.example.com/a%29%28b)" in note


def test_markdown_in_author_and_message_is_escaped() -> None:
    note = commit_journal_note(_commit(author_name="[x](https://evil.example.com)", message="**bold** <b>"))
    assert "\\[x\\]\\(https://evil.example.com\\)" in note
    assert note.endswith("\\*\\*bold\\*\\* \\<b\\>")


def test_backticks_in_branch_are_dropped() -> None:
    assert "on `feat/x`" in commit_journal_note(_commit(branch="feat/`x`"))


def test_long_first_line_is_cut() -> None:
    note = commit_journal_note(_commit(message="y" * 500))
    assert note.endswith("y" * 199 + "…")


@pytest.mark.parametrize(
    ("existing", "values", "expected"),
    [
        (None, ["a", "b"], ["a", "b"]),
        ([], ["a"], ["a"]),
        (["a"], ["a", "b", "b"], ["a", "b"]),
        (["x", 1], ["x"], ["x", 1]),
        ("not a list", ["a"], None),
        ({"k": 1}, ["a"], None),
    ],
)
def test_append_unique(existing, values, expected) -> None:
    assert append_unique(existing, values) == expected


def test_prune_payload_drops_file_lists_only() -> None:
    payload = {
        "ref": "refs/heads/main",
        "commits": [{"id": "1", "added": ["a"], "removed": [], "modified": ["m"], "message": "x"}, "junk"],
        "head_commit": {"id": "1", "added": ["a"]},
    }
    assert prune_payload(payload) == {
        "ref": "refs/heads/main",
        "commits": [{"id": "1", "message": "x"}, "junk"],
        "head_commit": {"id": "1"},
    }
    assert payload["commits"][0]["added"] == ["a"]
