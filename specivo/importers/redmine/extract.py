"""Turn Redmine rows into intermediate-representation objects.

Every function here is pure: a row dict in, an IR object out. That keeps the
translation decisions — and there are a lot of them — testable against literal
dicts, with no Redmine instance and no database in the way.

The awkward cases are documented where they occur. The recurring ones:

* Redmine stores timestamps as naive UTC, and Specivo's columns are
  timezone-aware, so every timestamp is stamped UTC on the way through.
* Redmine's ``users`` table holds people, groups and the anonymous account,
  discriminated by ``type``.
* Several fields have no Specivo equivalent and are dropped deliberately rather
  than guessed at.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from specivo.importers.core.ir import (
    IRActivity,
    IRGroup,
    IRPriority,
    IRRole,
    IRStatus,
    IRTracker,
    IRUser,
)

# Order matters: ``trackers.fields_bits`` sets bit i when CORE_FIELDS[i] is
# disabled. Copied from Redmine's Tracker::CORE_FIELDS, where the comment
# reads "Other (future) fields should be appended, not inserted!".
REDMINE_CORE_FIELDS: tuple[str, ...] = (
    "assigned_to_id",
    "category_id",
    "fixed_version_id",
    "parent_issue_id",
    "start_date",
    "due_date",
    "estimated_hours",
    "done_ratio",
    "description",
    "priority_id",
)

# The one core field Specivo names differently.
_CORE_FIELD_RENAMES: dict[str, str] = {"parent_issue_id": "parent_id"}

# Redmine's Principal::STATUS_* constants.
USER_STATUS_ANONYMOUS = 0
USER_STATUS_ACTIVE = 1
USER_STATUS_REGISTERED = 2
USER_STATUS_LOCKED = 3

_USER_STATUS_MAP: dict[int, str] = {
    USER_STATUS_ACTIVE: "active",
    # Registered means signed up but not yet confirmed.
    USER_STATUS_REGISTERED: "pending_verification",
    USER_STATUS_LOCKED: "locked",
}

# Status-name hints used to place a status in Specivo's four-way grouping.
# Redmine only records whether a status closes an issue, so everything else is
# inference — the operator can override any of it per status name.
_BACKLOG_HINTS = frozenset({"new", "open", "backlog", "todo", "to do", "unconfirmed", "submitted"})
_DONE_HINTS = frozenset({"resolved", "done", "fixed", "completed", "complete", "verified", "ready for release"})

# Redmine's principal subclasses.
TYPE_USER = "User"
TYPE_GROUP = "Group"
TYPE_ANONYMOUS = "AnonymousUser"


def as_utc(value: datetime | None) -> datetime | None:
    """Stamp a naive Redmine timestamp as UTC.

    Rails writes UTC into ``timestamp without time zone``, so the value is
    already UTC and only the marker is missing. A value that somehow arrives
    with a zone is left alone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def decode_disabled_core_fields(fields_bits: int | None) -> list[str]:
    """Return the core fields a tracker hides, from Redmine's bitmask.

    Bit *i* set means ``REDMINE_CORE_FIELDS[i]`` is disabled. Specivo stores the
    same idea as a list of names, so only the one differing name is translated.
    """
    bits = fields_bits or 0
    return [
        _CORE_FIELD_RENAMES.get(name, name) for index, name in enumerate(REDMINE_CORE_FIELDS) if bits & (1 << index)
    ]


def status_category(name: str, is_closed: bool, overrides: dict[str, str] | None = None) -> str:
    """Place a Redmine status in Specivo's backlog/active/done/closed grouping.

    Redmine records only ``is_closed``, so the rest is inferred from the name.
    The inference is deliberately shallow — an operator override, matched on the
    status name case-insensitively, always wins, and the import report lists
    what was guessed so it can be corrected.
    """
    if overrides:
        override = overrides.get(name.strip().lower())
        if override:
            return override

    if is_closed:
        return "closed"

    normalised = name.strip().lower()
    if normalised in _BACKLOG_HINTS:
        return "backlog"
    if normalised in _DONE_HINTS:
        return "done"
    return "active"


def map_user_status(status: int | None) -> str:
    """Map Redmine's numeric user status to Specivo's status string.

    Anything unrecognised becomes ``locked``: refusing sign-in to an account
    whose state we could not read is the safe direction to be wrong in.
    """
    return _USER_STATUS_MAP.get(status if status is not None else -1, "locked")


def normalise_language(language: str | None) -> str | None:
    """Return a bare language code, or ``None`` when Redmine has no preference.

    Redmine stores codes like ``en``, ``pt-BR`` and ``zh-TW``; Specivo's
    catalogs are per language. The region is dropped and the loader decides
    whether the result is one it ships, falling back to the instance default.
    """
    if not language:
        return None
    return language.strip().lower().split("-")[0] or None


def display_name(row: dict[str, Any]) -> str:
    """Build a display name from a Redmine principal row.

    Groups carry their name in ``lastname`` and leave ``firstname`` empty, so
    joining the two parts covers both people and groups. An account with
    neither falls back to its login, since a blank name is worse than a login.
    """
    parts = [(row.get("firstname") or "").strip(), (row.get("lastname") or "").strip()]
    name = " ".join(part for part in parts if part)
    return name or (row.get("login") or "").strip() or f"user-{row['id']}"


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def extract_tracker(row: dict[str, Any]) -> IRTracker:
    """Build an :class:`IRTracker` from a ``trackers`` row.

    ``private_by_default`` is dropped: Specivo has no per-tracker default for
    issue privacy.
    """
    return IRTracker(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        description=row.get("description") or None,
        is_in_roadmap=bool(row.get("is_in_roadmap")),
        default_status_ref=str(row["default_status_id"]) if row.get("default_status_id") else None,
        disabled_core_fields=decode_disabled_core_fields(row.get("fields_bits")),
        position=row.get("position") or 0,
    )


def extract_status(row: dict[str, Any], overrides: dict[str, str] | None = None) -> IRStatus:
    """Build an :class:`IRStatus`, resolving its Specivo category."""
    name = (row.get("name") or "").strip()
    return IRStatus(
        source_ref=str(row["id"]),
        name=name,
        category=status_category(name, bool(row.get("is_closed")), overrides),
        position=row.get("position") or 0,
        default_done_ratio=row.get("default_done_ratio"),
    )


def extract_priority(row: dict[str, Any]) -> IRPriority:
    """Build an :class:`IRPriority` from an ``enumerations`` row."""
    return IRPriority(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        position=row.get("position") or 0,
        is_default=bool(row.get("is_default")),
        active=bool(row.get("active")),
    )


def extract_activity(row: dict[str, Any]) -> IRActivity:
    """Build an :class:`IRActivity` from an ``enumerations`` row."""
    return IRActivity(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        is_default=bool(row.get("is_default")),
        active=bool(row.get("active")),
    )


def extract_role(row: dict[str, Any]) -> IRRole:
    """Build an :class:`IRRole` from a ``roles`` row.

    Permissions are not carried over. Redmine stores them as serialised Ruby
    YAML naming Redmine's own permissions, and the two systems' permission
    vocabularies do not correspond one to one — translating them would be
    guesswork that quietly widens or narrows access. The loader matches roles by
    name against the roles already in Specivo, and anything unmatched is created
    with no permissions and listed in the import report for an administrator to
    fill in.

    ``users_visibility`` and ``time_entries_visibility`` are dropped for the same
    reason: Specivo has no equivalent yet.

    ``builtin`` carries over directly — both systems use 0 for an ordinary role,
    1 for the non-member role and 2 for the anonymous one.
    """
    return IRRole(
        source_ref=str(row["id"]),
        name=(row.get("name") or "").strip(),
        builtin=row.get("builtin") or 0,
        permissions=[],
    )


# --------------------------------------------------------------------------
# Principals
# --------------------------------------------------------------------------


def extract_user(row: dict[str, Any], email: str | None = None) -> IRUser:
    """Build an :class:`IRUser` from a ``users`` row and its default address.

    No credential is carried: Redmine salts and hashes with SHA1 and Specivo
    uses bcrypt, so the loader gives every imported account an unusable password
    and reports the logins that need one set.
    """
    return IRUser(
        source_ref=str(row["id"]),
        login=(row.get("login") or "").strip(),
        display_name=display_name(row),
        email=(email or "").strip() or None,
        status=map_user_status(row.get("status")),
        is_admin=bool(row.get("admin")),
        language=normalise_language(row.get("language")),
        last_login_at=as_utc(row.get("last_login_on")),
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
    )


def extract_group(row: dict[str, Any], member_refs: list[str]) -> IRGroup:
    """Build an :class:`IRGroup` from a ``users`` row of type ``Group``."""
    return IRGroup(
        source_ref=str(row["id"]),
        name=display_name(row),
        member_refs=list(member_refs),
    )
