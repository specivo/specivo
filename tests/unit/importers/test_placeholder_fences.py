"""Placeholders inside fenced code are left alone, with fences paired as CommonMark pairs them.

A fence is a whole line of three or more backticks or tildes, indented by at
most three spaces. It is closed by a line of the same character, at least as
long, with nothing after it but spaces; one that is never closed runs to the
end of the text. The migration SDK keeps the same cases in its
``tests/test_placeholder_fences.py``, so the two find the same placeholders.
"""

from __future__ import annotations

import pytest

from specivo.importers.core.placeholders import expand_placeholders, find_placeholders

pytestmark = pytest.mark.unit

FENCE_CASES = {
    "closed-backticks": ("{{user:u1}}\n```\n{{user:u2}}\n```\n{{user:u3}}", ["u1", "u3"]),
    "closed-tildes": ("{{user:u1}}\n~~~\n{{user:u2}}\n~~~\n{{user:u3}}", ["u1", "u3"]),
    # A closing fence is at least as long as the opening one.
    "shorter-backtick-line-is-code": ("````\n```\n{{user:u1}}\n```\n````\n{{user:u2}}", ["u2"]),
    "shorter-tilde-line-is-code": ("~~~~\n~~~\n{{user:u1}}\n~~~\n~~~~\n{{user:u2}}", ["u2"]),
    "longer-closing-fence": ("```\n{{user:u1}}\n`````\n{{user:u2}}", ["u2"]),
    # A closing fence uses the opening fence's character.
    "other-character-is-code": ("```\n{{user:u1}}\n~~~\n{{user:u2}}\n```\n{{user:u3}}", ["u3"]),
    # An unclosed fence runs to the end of the text.
    "unclosed-fence": ("{{user:u1}}\n```\n{{user:u2}}", ["u1"]),
    "unclosed-longer-fence": ("````\n{{user:u1}}\n```\n{{user:u2}}", []),
    # A closing fence has nothing after it but spaces.
    "closing-fence-with-text-does-not-close": ("```\n{{user:u1}}\n``` x\n{{user:u2}}", []),
    "closing-fence-with-trailing-spaces": ("```\n{{user:u1}}\n```   \n{{user:u2}}", ["u2"]),
    # Fences are whole lines, indented by at most three spaces.
    "indented-three-spaces": ("   ```\n{{user:u1}}\n   ```\n{{user:u2}}", ["u2"]),
    "indented-four-spaces-is-no-fence": ("    ```\n{{user:u1}}\n    ```", ["u1"]),
    "backticks-inside-a-line-are-no-fence": ("Use ``` here {{user:u1}} and ``` there", ["u1"]),
    "backticks-across-lines-are-no-fence": ("a ```b\n{{user:u1}}\nc``` d", ["u1"]),
    # A backtick fence's info string holds no backtick; a tilde fence's may.
    "backtick-in-backtick-info-is-no-fence": ("```a`b\n{{user:u1}}", ["u1"]),
    "backtick-in-tilde-info": ("~~~ a`b\n{{user:u1}}\n~~~\n{{user:u2}}", ["u2"]),
    "info-string": ("```python\n{{user:u1}}\n```\n{{user:u2}}", ["u2"]),
    "crlf-line-endings": ("```\r\n{{user:u1}}\r\n```\r\n{{user:u2}}", ["u2"]),
    # Only fenced blocks are code here: an inline code span is expanded.
    "inline-code-is-expanded": ("`{{user:u1}}` and ``{{user:u2}}``", ["u1", "u2"]),
}


@pytest.mark.parametrize(("text", "refs"), list(FENCE_CASES.values()), ids=list(FENCE_CASES))
def test_placeholders_in_fenced_code_are_not_found(text, refs):
    assert [placeholder.ref for placeholder in find_placeholders(text)] == refs


@pytest.mark.parametrize(("text", "refs"), list(FENCE_CASES.values()), ids=list(FENCE_CASES))
def test_expansion_skips_what_finding_skips(text, refs):
    expanded, unresolved = expand_placeholders(text, lambda placeholder: f"@{placeholder.ref}")
    assert unresolved == []
    for ref in refs:
        assert f"@{ref}" in expanded
    assert expanded.count("{{user:") == text.count("{{user:") - len(refs)


def test_expansion_keeps_a_longer_fence_intact():
    text = "Before {{user:u1}}\n\n````markdown\n```\n{{user:u2}}\n```\n````\n\nAfter {{issue:i1|the bug}}"
    expanded, _ = expand_placeholders(text, lambda placeholder: f"<{placeholder.ref}>")
    assert expanded == "Before <u1>\n\n````markdown\n```\n{{user:u2}}\n```\n````\n\nAfter <i1>"


def test_expansion_leaves_an_unclosed_fence_to_the_end():
    text = "{{user:u1}}\n```\n{{user:u2}}\n\nstill code {{user:u3}}"
    expanded, _ = expand_placeholders(text, lambda placeholder: "@x")
    assert expanded == "@x\n```\n{{user:u2}}\n\nstill code {{user:u3}}"
