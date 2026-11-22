"""The permission catalogue: complete, acyclic, and consistent with every role the product seeds."""

from __future__ import annotations

import pytest

from specivo.cli.seed import _DEFAULT_ROLES
from specivo.core.exceptions import ValidationError
from specivo.services.permission_catalogue import (
    CATALOGUE_ORDER,
    FULL_ACCESS,
    NON_MEMBER_ALLOWED,
    PERMISSION_GROUPS,
    PERMISSION_REQUIRES,
    catalogue_payload,
    normalise_permissions,
    not_allowed_for_non_member,
    requires_closure,
)
from specivo.services.permission_service import Permission

pytestmark = pytest.mark.unit

# Roles the migrations create rather than the seed (0046 Automation, 0032 Non member).
_AUTOMATION = ["view_issues", "edit_issues", "add_issue_notes"]
_NON_MEMBER = ["view_issues"]


def test_every_permission_is_in_exactly_one_group() -> None:
    keys = [info.key for group in PERMISSION_GROUPS for info in group.permissions]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(Permission)
    assert len(CATALOGUE_ORDER) == len(Permission) == 28


def test_group_keys_are_unique() -> None:
    keys = [group.key for group in PERMISSION_GROUPS]
    assert len(keys) == len(set(keys))


def test_unenforced_permissions_are_marked() -> None:
    unenforced = {info.key for group in PERMISSION_GROUPS for info in group.permissions if not info.enforced}
    assert unenforced == {Permission.MANAGE_SUBTASKS, Permission.DELETE_OWN_NOTES, Permission.DELETE_NOTES}


def test_requires_map_is_valid_and_acyclic() -> None:
    for key, prerequisites in PERMISSION_REQUIRES.items():
        assert isinstance(key, Permission)
        assert prerequisites and all(isinstance(p, Permission) for p in prerequisites)
        assert key not in requires_closure(key), f"{key} depends on itself"


def test_closure_is_transitive() -> None:
    assert requires_closure(Permission.DELETE_WIKI_PAGES) == {Permission.MANAGE_WIKI, Permission.VIEW_WIKI}
    assert requires_closure(Permission.MANAGE_RECURRING_TASKS) == {Permission.ADD_ISSUES, Permission.VIEW_ISSUES}
    assert requires_closure(Permission.EDIT_OWN_NOTES) == {Permission.ADD_ISSUE_NOTES, Permission.VIEW_ISSUES}
    assert requires_closure(Permission.VIEW_ISSUES) == frozenset()


def test_non_member_allowed_is_closed_under_requires() -> None:
    for permission in NON_MEMBER_ALLOWED:
        assert requires_closure(permission) <= NON_MEMBER_ALLOWED, permission
    assert len(NON_MEMBER_ALLOWED) == 13


@pytest.mark.parametrize(
    "permissions",
    [pytest.param(role["permissions"], id=role["name"]) for role in _DEFAULT_ROLES]
    + [pytest.param(_AUTOMATION, id="Automation"), pytest.param(_NON_MEMBER, id="Non member")],
)
def test_seeded_roles_are_closed(permissions: list[str]) -> None:
    """A first save of an untouched role adds nothing."""
    assert set(normalise_permissions(permissions)) == set(permissions)


def test_seeded_developer_can_edit_the_wiki() -> None:
    developer = next(r for r in _DEFAULT_ROLES if r["name"] == "Developer")
    assert "manage_wiki" in developer["permissions"]


def test_non_member_seed_is_within_the_ceiling() -> None:
    assert not_allowed_for_non_member(_NON_MEMBER) == []


class TestNormalise:
    def test_adds_the_closure(self) -> None:
        assert normalise_permissions(["delete_wiki_pages"]) == ["view_wiki", "manage_wiki", "delete_wiki_pages"]

    def test_full_access_collapses(self) -> None:
        assert normalise_permissions(["view_issues", FULL_ACCESS, "log_time"]) == [FULL_ACCESS]

    def test_full_access_wins_over_unknown_keys(self) -> None:
        assert normalise_permissions(["nope", FULL_ACCESS]) == [FULL_ACCESS]

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            normalise_permissions(["view_issues", "fly", "teleport"])
        assert excinfo.value.field == "permissions"
        assert excinfo.value.details == {"unknown": ["fly", "teleport"]}

    def test_dedupes_and_orders_by_catalogue(self) -> None:
        result = normalise_permissions(["manage_project", "log_time", "view_issues", "log_time", "add_issues"])
        assert result == ["view_issues", "add_issues", "view_time_entries", "log_time", "manage_project"]

    def test_empty(self) -> None:
        assert normalise_permissions([]) == []


class TestNonMemberCeiling:
    def test_full_access_is_refused_first(self) -> None:
        assert not_allowed_for_non_member(["delete_issues", FULL_ACCESS]) == [FULL_ACCESS, "delete_issues"]

    def test_allowed_set_passes(self) -> None:
        assert not_allowed_for_non_member([p.value for p in NON_MEMBER_ALLOWED]) == []

    def test_management_and_delete_are_refused(self) -> None:
        refused = not_allowed_for_non_member(["view_issues", "manage_members", "delete_wiki_pages", "manage_wiki"])
        assert refused == ["delete_wiki_pages", "manage_members"]


def test_payload_shape() -> None:
    payload = catalogue_payload()
    assert payload["total"] == 28
    assert [g["key"] for g in payload["groups"]] == [
        "issues",
        "comments",
        "wiki",
        "planning",
        "time",
        "ai_usage",
        "project",
    ]
    wiki = next(g for g in payload["groups"] if g["key"] == "wiki")
    assert wiki["module"] == "wiki"
    delete = next(p for p in wiki["permissions"] if p["key"] == "delete_wiki_pages")
    assert delete == {
        "key": "delete_wiki_pages",
        "label": "Delete wiki pages",
        "description": "Delete wiki pages.",
        "enforced": True,
        "requires": ["view_wiki", "manage_wiki"],
    }
    assert all(isinstance(p["label"], str) for g in payload["groups"] for p in g["permissions"])
    assert set(payload["non_member_allowed"]) == {p.value for p in NON_MEMBER_ALLOWED}
