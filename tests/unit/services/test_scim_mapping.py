"""Unit tests for the SCIM User -> Specivo account mapping (``specivo.services.scim.mapping``).

``userName`` becomes the login (normalised like an SSO login, never one
starting with ``$``); the primary, else the work, else the first email is the
account's email; ``displayName``, ``name.formatted``, given and family name
give the display name; ``active`` may be a boolean or the strings Entra ID
sends; ``preferredLanguage`` / ``locale`` and ``timezone`` are used only when
valid. Password, admin rights or the service-account flag are never read.
"""

from __future__ import annotations

from typing import Any

import pytest

from specivo.services.scim.errors import ScimError
from specivo.services.scim.mapping import coerce_active, group_attributes, user_attributes

pytestmark = pytest.mark.unit

LANGUAGES = ["en", "th"]


def _user(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": "alice@example.com",
        "externalId": "ext-1",
        "name": {"formatted": "Alice Example", "givenName": "Alice", "familyName": "Example"},
        "displayName": "Alice E.",
        "emails": [{"type": "work", "value": "alice@example.com", "primary": True}],
        "active": True,
    }
    body.update(overrides)
    return body


def test_full_mapping() -> None:
    attrs = user_attributes(_user(preferredLanguage="th-TH", timezone="Asia/Bangkok"), languages=LANGUAGES)
    assert attrs.user_name == "alice@example.com"
    assert attrs.login == "alice"
    assert attrs.email == "alice@example.com"
    assert attrs.display_name == "Alice E."
    assert attrs.active is True
    assert attrs.language == "th"
    assert attrs.timezone == "Asia/Bangkok"
    assert attrs.external_id == "ext-1"


def test_login_is_normalised() -> None:
    assert user_attributes(_user(userName="Bob.Smith"), languages=LANGUAGES).login == "bob-smith"
    assert user_attributes(_user(userName="carol_x"), languages=LANGUAGES).login == "carol_x"


@pytest.mark.parametrize("user_name", ["$git", "$anonymous", " $x"])
def test_reserved_login_is_refused(user_name: str) -> None:
    with pytest.raises(ScimError) as exc:
        user_attributes(_user(userName=user_name), languages=LANGUAGES)
    assert exc.value.status == 400
    assert exc.value.scim_type == "invalidValue"


@pytest.mark.parametrize("user_name", [None, "", "   ", 42, "@@@", "x" * 300])
def test_unusable_user_name_is_refused(user_name: Any) -> None:
    with pytest.raises(ScimError) as exc:
        user_attributes(_user(userName=user_name), languages=LANGUAGES)
    assert exc.value.scim_type == "invalidValue"


def test_email_prefers_primary_then_work_then_first() -> None:
    emails = [
        {"type": "home", "value": "home@example.com"},
        {"type": "work", "value": "work@example.com"},
        {"type": "other", "value": "primary@example.com", "primary": True},
    ]
    assert user_attributes(_user(emails=emails), languages=LANGUAGES).email == "primary@example.com"
    assert user_attributes(_user(emails=emails[:2]), languages=LANGUAGES).email == "work@example.com"
    assert user_attributes(_user(emails=emails[:1]), languages=LANGUAGES).email == "home@example.com"


@pytest.mark.parametrize("emails", [None, [], [{"type": "work"}], [{"value": "not-an-email"}], "a@example.com"])
def test_missing_or_bad_email_is_refused(emails: Any) -> None:
    body = _user(emails=emails)
    if emails is None:
        del body["emails"]
    with pytest.raises(ScimError) as exc:
        user_attributes(body, languages=LANGUAGES)
    assert exc.value.scim_type == "invalidValue"


def test_display_name_fallbacks() -> None:
    body = _user()
    del body["displayName"]
    assert user_attributes(body, languages=LANGUAGES).display_name == "Alice Example"
    del body["name"]
    body["displayName"] = "Alice E."
    assert user_attributes(body, languages=LANGUAGES).display_name == "Alice E."
    del body["displayName"]
    body["name"] = {"givenName": "Alice", "familyName": "Example"}
    assert user_attributes(body, languages=LANGUAGES).display_name == "Alice Example"
    del body["name"]
    assert user_attributes(body, languages=LANGUAGES).display_name == "alice"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), ("True", True), ("False", False), ("false", False), ("TRUE", True), (None, True)],
)
def test_active_values(value: Any, expected: bool) -> None:
    assert coerce_active(value) is expected


@pytest.mark.parametrize("value", ["no", 0, 1, "", [], {}])
def test_bad_active_is_refused(value: Any) -> None:
    with pytest.raises(ScimError) as exc:
        coerce_active(value)
    assert exc.value.scim_type == "invalidValue"


def test_invalid_language_and_timezone_are_ignored() -> None:
    attrs = user_attributes(_user(preferredLanguage="xx", locale="yy", timezone="Mars/Olympus"), languages=LANGUAGES)
    assert attrs.language is None
    assert attrs.timezone is None


def test_locale_used_when_preferred_language_missing() -> None:
    assert user_attributes(_user(locale="th_TH"), languages=LANGUAGES).language == "th"


def test_privileged_attributes_are_never_read() -> None:
    attrs = user_attributes(
        _user(password="secret-value-1", isAdmin=True, is_admin=True, roles=[{"value": "admin"}]),
        languages=LANGUAGES,
    )
    assert not hasattr(attrs, "password")
    assert not hasattr(attrs, "is_admin")


def test_group_attributes() -> None:
    attrs = group_attributes(
        {"displayName": " Engineering ", "externalId": "g-1", "members": [{"value": "u1"}, {"value": "u2"}]}
    )
    assert attrs.display_name == "Engineering"
    assert attrs.external_id == "g-1"
    assert attrs.member_ids == ["u1", "u2"]


@pytest.mark.parametrize("name", [None, "", "  ", 3, "x" * 300])
def test_group_needs_a_display_name(name: Any) -> None:
    with pytest.raises(ScimError) as exc:
        group_attributes({"displayName": name})
    assert exc.value.scim_type == "invalidValue"


@pytest.mark.parametrize("members", ["u1", [{"display": "x"}], [{"value": 3}], [1]])
def test_group_bad_members(members: Any) -> None:
    with pytest.raises(ScimError) as exc:
        group_attributes({"displayName": "Eng", "members": members})
    assert exc.value.scim_type == "invalidValue"
