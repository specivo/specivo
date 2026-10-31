"""Unit tests for the SCIM filter parser (``specivo.services.scim.filters``).

Only equality filters on a few attributes are supported (``userName``,
``externalId``, ``id``, ``emails[type eq "work"].value`` / ``emails.value``
for users; ``displayName``, ``externalId``, ``id`` for groups), joined with
``and``. Attribute names and the operator are case-insensitive (RFC 7644
section 3.4.2.2), the value is a JSON string. Anything else is a 400
``invalidFilter``: the parser never builds SQL from the text.
"""

from __future__ import annotations

import pytest

from specivo.services.scim.errors import ScimError
from specivo.services.scim.filters import Comparison, parse_filter

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('userName eq "alice"', [Comparison("userName", "alice")]),
        ('USERNAME EQ "alice@example.com"', [Comparison("userName", "alice@example.com")]),
        (
            'externalId eq "0a21f0f2-8d2a-4f8e-bf98-7363c4aed4ef"',
            [Comparison("externalId", "0a21f0f2-8d2a-4f8e-bf98-7363c4aed4ef")],
        ),
        ('id eq "abc"', [Comparison("id", "abc")]),
        ('emails[type eq "work"].value eq "a@example.com"', [Comparison("emails.value", "a@example.com")]),
        ('emails.value eq "a@example.com"', [Comparison("emails.value", "a@example.com")]),
        (
            'urn:ietf:params:scim:schemas:core:2.0:User:userName eq "alice"',
            [Comparison("userName", "alice")],
        ),
        ('  userName   eq   "a b"  ', [Comparison("userName", "a b")]),
        ('userName eq "say \\"hi\\""', [Comparison("userName", 'say "hi"')]),
        (
            'userName eq "alice" and externalId eq "x1"',
            [Comparison("userName", "alice"), Comparison("externalId", "x1")],
        ),
    ],
)
def test_supported_user_filters(text: str, expected: list[Comparison]) -> None:
    assert parse_filter(text, "User") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('displayName eq "Engineering"', [Comparison("displayName", "Engineering")]),
        ('externalId eq "g-1"', [Comparison("externalId", "g-1")]),
        ('id eq "abc"', [Comparison("id", "abc")]),
    ],
)
def test_supported_group_filters(text: str, expected: list[Comparison]) -> None:
    assert parse_filter(text, "Group") == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "userName",
        'userName ne "alice"',
        'userName co "ali"',
        'userName sw "a"',
        "userName pr",
        'userName gt "a"',
        'userName eq "alice" or externalId eq "x"',
        'not (userName eq "alice")',
        '(userName eq "alice")',
        "userName eq alice",
        "userName eq 42",
        "userName eq true",
        "userName eq null",
        'displayName eq "x"',  # not a user filter
        'emails[type eq "home"].value eq "a@example.com"',
        'emails[value eq "a@example.com"]',
        'emails eq "a@example.com"',
        'name.givenName eq "Alice"',
        'userName eq "alice" and',
        'userName eq "alice" and and externalId eq "x"',
        'userName eq "unterminated',
        'userName eq "alice"; DROP TABLE users; --',
        "userName eq 'alice'",
        'userName eq "alice" OR 1=1',
        'meta.lastModified gt "2011-05-13T04:42:34Z"',
        "x" * 2000,
    ],
)
def test_unsupported_user_filters_are_invalid_filter(text: str) -> None:
    with pytest.raises(ScimError) as exc:
        parse_filter(text, "User")
    assert exc.value.status == 400
    assert exc.value.scim_type == "invalidFilter"


@pytest.mark.parametrize("text", ['userName eq "x"', 'members[value eq "x"]', 'displayName co "x"'])
def test_unsupported_group_filters_are_invalid_filter(text: str) -> None:
    with pytest.raises(ScimError) as exc:
        parse_filter(text, "Group")
    assert exc.value.scim_type == "invalidFilter"
