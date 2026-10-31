"""SCIM User and Group bodies -> the values Specivo stores, validated.

:func:`user_attributes` reads a User resource (from a POST, a PUT, or a PATCH
applied to the current representation):

* ``userName`` -> login, normalised like an SSO login
  (:func:`specivo.services.sso.provisioning.sanitize_login`: lower-case,
  ``[a-z0-9_-]``, an email keeps its local part). A ``userName`` starting with
  ``$`` (the system principals' prefix) is refused.
* ``emails`` -> email: the ``primary`` one, else the ``work`` one, else the
  first. Required: every Specivo account has an email.
* ``displayName``, else ``name.formatted``, else given and family name, else
  the login -> display name.
* ``active`` -> account status: a boolean, or ``"True"`` / ``"False"`` in any
  case (Entra ID sends strings).
* ``preferredLanguage``, else ``locale`` -> language, and ``timezone``, only
  when they name a supported language or an IANA time zone; otherwise ignored.

Passwords, administrator rights and the service-account flag are never read
from SCIM, whatever the body holds. :func:`group_attributes` does the same
for a Group (``displayName`` -> user group name, ``members`` -> SCIM ids).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from specivo.core.locales import ALL_TIMEZONES
from specivo.services.anonymous_user_service import RESERVED_LOGIN_PREFIX
from specivo.services.scim.errors import ScimError
from specivo.services.sso.provisioning import map_locale, sanitize_login

__all__ = ["GroupAttributes", "UserAttributes", "coerce_active", "group_attributes", "user_attributes"]

_MAX_USER_NAME = 255
_MAX_EMAIL = 255
_MAX_DISPLAY_NAME = 255
_MAX_EXTERNAL_ID = 512


@dataclass(frozen=True)
class UserAttributes:
    """What a SCIM User sets on a Specivo account."""

    user_name: str
    login: str
    email: str
    display_name: str
    active: bool
    language: str | None
    timezone: str | None
    external_id: str | None


@dataclass(frozen=True)
class GroupAttributes:
    """What a SCIM Group sets on a Specivo user group."""

    display_name: str
    external_id: str | None
    member_ids: list[str] = field(default_factory=list)
    members_present: bool = False


def _get(body: dict[str, Any], name: str) -> Any:
    lowered = name.lower()
    for key, value in body.items():
        if key.lower() == lowered:
            return value
    return None


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if 0 < len(text) <= limit:
            return text
    return None


def coerce_active(value: Any) -> bool:
    """``active`` as a boolean; missing means active. Raises ``invalidValue`` otherwise."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError.invalid_value("active must be true or false.")


def _external_id(body: dict[str, Any]) -> str | None:
    value = _get(body, "externalId")
    if value is None:
        return None
    text = _text(value, _MAX_EXTERNAL_ID)
    if text is None:
        raise ScimError.invalid_value(f"externalId must be a string of at most {_MAX_EXTERNAL_ID} characters.")
    return text


def _email(body: dict[str, Any]) -> str:
    emails = _get(body, "emails")
    if not isinstance(emails, list):
        raise ScimError.invalid_value("A user needs an email address (emails).")
    entries = [e for e in emails if isinstance(e, dict)]
    primary = [e for e in entries if _get(e, "primary") is True]
    work = [e for e in entries if isinstance(_get(e, "type"), str) and _get(e, "type").lower() == "work"]
    for entry in (*primary, *work, *entries):
        value = _text(_get(entry, "value"), _MAX_EMAIL)
        if value is not None:
            if "@" not in value or " " in value or value.startswith("@") or value.endswith("@"):
                raise ScimError.invalid_value("The email address is not valid.")
            return value
    raise ScimError.invalid_value("A user needs an email address (emails).")


def _display_name(body: dict[str, Any], login: str) -> str:
    display = _text(_get(body, "displayName"), _MAX_DISPLAY_NAME)
    if display:
        return display
    name = _get(body, "name")
    name = name if isinstance(name, dict) else {}
    formatted = _text(_get(name, "formatted"), _MAX_DISPLAY_NAME)
    if formatted:
        return formatted
    parts = [p for p in (_text(_get(name, "givenName"), 120), _text(_get(name, "familyName"), 120)) if p]
    return " ".join(parts) if parts else login


def user_attributes(body: dict[str, Any], *, languages: Sequence[str]) -> UserAttributes:
    """Validate a SCIM User body; raises :class:`ScimError` ``invalidValue``."""
    raw = _get(body, "userName")
    if not isinstance(raw, str) or not raw.strip():
        raise ScimError.invalid_value("userName is required.")
    user_name = raw.strip()
    if len(user_name) > _MAX_USER_NAME:
        raise ScimError.invalid_value(f"userName must be at most {_MAX_USER_NAME} characters.")
    if user_name.startswith(RESERVED_LOGIN_PREFIX):
        raise ScimError.invalid_value(f"userName must not start with {RESERVED_LOGIN_PREFIX}.")
    login = sanitize_login(user_name)
    if login is None:
        raise ScimError.invalid_value("userName has no characters usable in a login.")

    language = map_locale(_get(body, "preferredLanguage"), list(languages)) or map_locale(
        _get(body, "locale"), list(languages)
    )
    timezone = _get(body, "timezone")
    return UserAttributes(
        user_name=user_name,
        login=login,
        email=_email(body),
        display_name=_display_name(body, login),
        active=coerce_active(_get(body, "active")),
        language=language,
        timezone=timezone if isinstance(timezone, str) and timezone in ALL_TIMEZONES else None,
        external_id=_external_id(body),
    )


def group_attributes(body: dict[str, Any]) -> GroupAttributes:
    """Validate a SCIM Group body; raises :class:`ScimError` ``invalidValue``."""
    name = _text(_get(body, "displayName"), _MAX_DISPLAY_NAME)
    if name is None:
        raise ScimError.invalid_value(f"displayName is required (at most {_MAX_DISPLAY_NAME} characters).")
    members = _get(body, "members")
    present = members is not None
    ids: list[str] = []
    if present:
        if not isinstance(members, list):
            raise ScimError.invalid_value("members must be a list.")
        for member in members:
            value = _get(member, "value") if isinstance(member, dict) else None
            if not isinstance(value, str) or not value:
                raise ScimError.invalid_value("Each member needs a value (the member's id).")
            if value not in ids:
                ids.append(value)
    return GroupAttributes(display_name=name, external_id=_external_id(body), member_ids=ids, members_present=present)
