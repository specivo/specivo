"""Unit tests for SCIM PATCH (``specivo.services.scim.patch``).

A PATCH request is applied to the resource's current SCIM representation (a
dict); the result is then validated like a PUT. Covered here: the path
grammar (``attr``, ``attr.sub``, ``attr[sub eq "v"]``, ``attr[sub eq
"v"].sub``, schema URN prefixes), the three operations, case-insensitive
``op`` values, and the forms Microsoft Entra ID and Okta send: a ``replace``
without a path whose value object has dotted or filtered keys, ``active`` as
the string ``"False"``, and member removal both as ``members[value eq "x"]``
and as ``path: members`` with a value list.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from specivo.services.scim.errors import ScimError
from specivo.services.scim.patch import ScimPath, apply_patch, parse_path

pytestmark = pytest.mark.unit

PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _request(*operations: dict[str, Any]) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": list(operations)}


def _user() -> dict[str, Any]:
    return {
        "userName": "alice@example.com",
        "externalId": "ext-1",
        "active": True,
        "displayName": "Alice",
        "name": {"formatted": "Alice Example", "givenName": "Alice"},
        "emails": [{"type": "work", "value": "alice@example.com", "primary": True}],
    }


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("active", ScimPath("active")),
        ("ACTIVE", ScimPath("active")),
        ("userName", ScimPath("userName")),
        ("name.givenName", ScimPath("name", sub_attribute="givenName")),
        ("name.familyname", ScimPath("name", sub_attribute="familyName")),
        ('emails[type eq "work"].value', ScimPath("emails", value_filter=("type", "work"), sub_attribute="value")),
        ('members[value eq "abc"]', ScimPath("members", value_filter=("value", "abc"))),
        ("urn:ietf:params:scim:schemas:core:2.0:User:displayName", ScimPath("displayName")),
        (
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:employeeNumber",
            ScimPath("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:employeeNumber"),
        ),
        (
            'addresses[type eq "work"].formatted',
            ScimPath("addresses", value_filter=("type", "work"), sub_attribute="formatted"),
        ),
    ],
)
def test_parse_path(text: str, expected: ScimPath) -> None:
    assert parse_path(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "emails[",
        'emails[type ne "work"].value',
        "emails[type eq work].value",
        "a..b",
        "1abc",
        'x[a eq "b"]]',
        "a b",
    ],
)
def test_bad_paths_are_invalid_path(text: str) -> None:
    with pytest.raises(ScimError) as exc:
        parse_path(text)
    assert exc.value.status == 400
    assert exc.value.scim_type == "invalidPath"


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def test_replace_simple_attribute_and_case_insensitive_op() -> None:
    result = apply_patch(_user(), _request({"op": "Replace", "path": "displayName", "value": "Al"}))
    assert result["displayName"] == "Al"


def test_entra_value_only_replace_with_string_active() -> None:
    # Entra ID disables a user with a replace whose value object holds ``active`` as a string.
    result = apply_patch(_user(), _request({"op": "Replace", "value": {"active": "False"}}))
    assert result["active"] == "False"


def test_entra_value_object_with_dotted_and_filtered_keys() -> None:
    result = apply_patch(
        _user(),
        _request(
            {
                "op": "replace",
                "value": {
                    "name.givenName": "Alicia",
                    'emails[type eq "work"].value': "alicia@example.com",
                    "displayName": "Alicia",
                },
            }
        ),
    )
    assert result["name"]["givenName"] == "Alicia"
    assert result["emails"][0]["value"] == "alicia@example.com"
    assert result["displayName"] == "Alicia"


def test_filtered_path_adds_an_element_when_none_matches() -> None:
    user = _user()
    user["emails"] = []
    result = apply_patch(
        user, _request({"op": "add", "path": 'emails[type eq "work"].value', "value": "a@example.com"})
    )
    assert result["emails"] == [{"type": "work", "value": "a@example.com"}]


def test_add_to_complex_attribute_creates_it() -> None:
    user = _user()
    del user["name"]
    result = apply_patch(user, _request({"op": "add", "path": "name.familyName", "value": "Example"}))
    assert result["name"] == {"familyName": "Example"}


def test_remove_simple_attribute() -> None:
    result = apply_patch(_user(), _request({"op": "remove", "path": "displayName"}))
    assert "displayName" not in result


def test_patch_does_not_change_the_input() -> None:
    user = _user()
    before = copy.deepcopy(user)
    apply_patch(user, _request({"op": "replace", "path": "displayName", "value": "Other"}))
    assert user == before


def test_add_members_extends_the_list_without_duplicates() -> None:
    group = {"displayName": "Eng", "members": [{"value": "u1"}]}
    result = apply_patch(group, _request({"op": "add", "path": "members", "value": [{"value": "u2"}, {"value": "u1"}]}))
    assert [m["value"] for m in result["members"]] == ["u1", "u2"]


def test_remove_members_by_filter_okta_style() -> None:
    group = {"displayName": "Eng", "members": [{"value": "u1"}, {"value": "u2"}]}
    result = apply_patch(group, _request({"op": "remove", "path": 'members[value eq "u1"]'}))
    assert [m["value"] for m in result["members"]] == ["u2"]


def test_remove_members_by_value_list_entra_style() -> None:
    group = {"displayName": "Eng", "members": [{"value": "u1"}, {"value": "u2"}, {"value": "u3"}]}
    result = apply_patch(
        group, _request({"op": "Remove", "path": "members", "value": [{"value": "u1"}, {"value": "u3"}]})
    )
    assert [m["value"] for m in result["members"]] == ["u2"]


def test_remove_all_members() -> None:
    group = {"displayName": "Eng", "members": [{"value": "u1"}]}
    result = apply_patch(group, _request({"op": "remove", "path": "members"}))
    assert result.get("members", []) == []


def test_replace_members_replaces_the_list() -> None:
    group = {"displayName": "Eng", "members": [{"value": "u1"}]}
    result = apply_patch(group, _request({"op": "replace", "path": "members", "value": [{"value": "u9"}]}))
    assert result["members"] == [{"value": "u9"}]


def test_value_only_add_merges_members() -> None:
    group = {"displayName": "Eng", "members": [{"value": "u1"}]}
    result = apply_patch(group, _request({"op": "add", "value": {"members": [{"value": "u2"}]}}))
    assert [m["value"] for m in result["members"]] == ["u1", "u2"]


@pytest.mark.parametrize(
    ("request_body", "scim_type"),
    [
        ({"Operations": [{"op": "replace", "path": "active", "value": False}]}, "invalidSyntax"),  # no schemas
        (_request(), "invalidValue"),  # no operations
        (_request({"op": "move", "path": "active", "value": False}), "invalidSyntax"),
        (_request({"path": "active", "value": False}), "invalidSyntax"),
        (_request({"op": "remove"}), "noTarget"),
        (_request({"op": "replace", "value": "x"}), "invalidValue"),
        (_request({"op": "add", "path": "displayName"}), "invalidValue"),
        (_request({"op": "replace", "path": 'emails[type eq "work"', "value": "x"}), "invalidPath"),
        ({"schemas": [PATCH_SCHEMA], "Operations": "nope"}, "invalidSyntax"),
    ],
)
def test_malformed_requests(request_body: dict[str, Any], scim_type: str) -> None:
    with pytest.raises(ScimError) as exc:
        apply_patch(_user(), request_body)
    assert exc.value.status == 400
    assert exc.value.scim_type == scim_type


def test_too_many_operations_is_refused() -> None:
    ops = [{"op": "replace", "path": "displayName", "value": str(i)} for i in range(1001)]
    with pytest.raises(ScimError) as exc:
        apply_patch(_user(), _request(*ops))
    assert exc.value.status == 400
