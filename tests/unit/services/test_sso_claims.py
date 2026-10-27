"""Unit tests for the groups claim resolver (``specivo.services.sso.claims``).

The groups claim is read with a dotted path (``groups``,
``realm_access.roles``, ``resource_access.specivo.roles``). The value must be
a list of strings or a single string; anything else counts as missing and is
logged. An Entra group overage (``_claim_names`` pointing at the claim, or
``hasgroups``) makes the groups unknown.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from specivo.models.sso import SsoProvider
from specivo.services.sso.claims import Known, Missing, Overage, read_groups, resolve_path

pytestmark = pytest.mark.unit


def _provider(groups_claim: str | None = None) -> SsoProvider:
    config: dict[str, Any] = {} if groups_claim is None else {"groups_claim": groups_claim}
    return SsoProvider(slug="claims-idp", issuer="https://idp.example.com", client_id="c", claim_config=config)


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------


def test_top_level_list() -> None:
    assert resolve_path({"groups": ["a", "b"]}, "groups") == ["a", "b"]


def test_single_string_becomes_a_one_item_list() -> None:
    assert resolve_path({"groups": "staff"}, "groups") == ["staff"]


def test_keycloak_realm_roles() -> None:
    claims = {"realm_access": {"roles": ["offline_access", "app-admins"]}}
    assert resolve_path(claims, "realm_access.roles") == ["offline_access", "app-admins"]


def test_keycloak_client_roles() -> None:
    claims = {"resource_access": {"specivo": {"roles": ["editor"]}, "account": {"roles": ["view-profile"]}}}
    assert resolve_path(claims, "resource_access.specivo.roles") == ["editor"]


def test_empty_list_is_present_and_empty() -> None:
    assert resolve_path({"groups": []}, "groups") == []


def test_literal_key_with_dots_wins() -> None:
    claims = {"https://example.com/groups": ["dev"]}
    assert resolve_path(claims, "https://example.com/groups") == ["dev"]


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"realm_access": {}},
        {"realm_access": "not-a-mapping"},
        {"realm_access": {"roles": None}},
    ],
    ids=["absent", "absent_leaf", "intermediate_not_mapping", "null"],
)
def test_absent_values_are_missing(claims: dict[str, Any]) -> None:
    assert resolve_path(claims, "realm_access.roles") is None


@pytest.mark.parametrize(
    "value",
    [42, True, {"a": 1}, ["ok", 3], [["nested"]]],
    ids=["int", "bool", "mapping", "mixed_list", "nested_list"],
)
def test_wrong_types_are_missing_and_logged(value: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="specivo.sso"):
        assert resolve_path({"groups": value}, "groups") is None
    assert "sso.claims.invalid_type" in caplog.text
    assert "path=groups" in caplog.text


# ---------------------------------------------------------------------------
# read_groups
# ---------------------------------------------------------------------------


def test_default_claim_is_groups() -> None:
    assert read_groups({"groups": ["a", "a", "b"]}, _provider()) == Known(frozenset({"a", "b"}))


def test_configured_path_is_used() -> None:
    claims = {"groups": ["ignored"], "realm_access": {"roles": ["r1"]}}
    assert read_groups(claims, _provider("realm_access.roles")) == Known(frozenset({"r1"}))


def test_group_names_are_case_sensitive() -> None:
    assert read_groups({"groups": ["Admins", "admins"]}, _provider()) == Known(frozenset({"Admins", "admins"}))


def test_missing_claim() -> None:
    assert isinstance(read_groups({"email": "x@example.com"}, _provider()), Missing)


def test_invalid_claim_is_missing() -> None:
    assert isinstance(read_groups({"groups": 7}, _provider()), Missing)


def test_entra_overage_from_claim_names() -> None:
    claims = {
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {"src1": {"endpoint": "https://graph.example.com/v1.0/users/x/getMemberObjects"}},
    }
    assert isinstance(read_groups(claims, _provider()), Overage)


def test_entra_hasgroups_is_overage() -> None:
    assert isinstance(read_groups({"hasgroups": True}, _provider()), Overage)


def test_overage_wins_over_a_partial_value() -> None:
    claims = {"groups": ["one"], "_claim_names": {"groups": "src1"}}
    assert isinstance(read_groups(claims, _provider()), Overage)


def test_overage_of_another_claim_is_ignored() -> None:
    # App roles are configured; the groups overage marker does not concern them.
    claims = {"roles": ["Specivo.Admin"], "_claim_names": {"groups": "src1"}, "hasgroups": True}
    assert read_groups(claims, _provider("roles")) == Known(frozenset({"Specivo.Admin"}))
