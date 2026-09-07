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

import re
from datetime import UTC, datetime
from typing import Any

from specivo.importers.core.ir import (
    IRActivity,
    IRCategory,
    IRCustomField,
    IRGroup,
    IRMembership,
    IRPriority,
    IRProject,
    IRRole,
    IRStatus,
    IRTracker,
    IRUser,
    IRVersion,
    PrincipalKind,
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


# --------------------------------------------------------------------------
# Projects and project-scoped entities
# --------------------------------------------------------------------------

# Redmine and Specivo happen to use the same project status encoding. Mapped
# explicitly anyway: relying on two systems agreeing by coincidence is how a
# closed project quietly comes back to life.
_PROJECT_STATUS_MAP: dict[int, int] = {1: 1, 5: 5, 9: 9}

# Redmine modules that have a Specivo equivalent. Everything else — repository,
# boards, calendar, gantt, news, documents — belongs to a feature Specivo does
# not have, so it is dropped and counted rather than mapped onto something else.
MODULE_MAP: dict[str, str] = {
    "issue_tracking": "issue_tracking",
    "wiki": "wiki",
    "time_tracking": "time_tracking",
}


def extract_project(row: dict[str, Any]) -> IRProject:
    """Build an :class:`IRProject` from a ``projects`` row.

    ``homepage`` and the various default_* columns are dropped: Specivo has no
    equivalent for them.
    """
    return IRProject(
        source_ref=str(row["id"]),
        identifier=(row.get("identifier") or "").strip().lower(),
        name=(row.get("name") or "").strip(),
        description=row.get("description") or None,
        is_public=bool(row.get("is_public")),
        parent_ref=str(row["parent_id"]) if row.get("parent_id") else None,
        status=_PROJECT_STATUS_MAP.get(row.get("status") or 1, 1),
        modules=[],
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
    )


def map_modules(names: list[str]) -> tuple[list[str], list[str]]:
    """Split Redmine module names into ones Specivo has and ones it does not."""
    mapped = [MODULE_MAP[name] for name in names if name in MODULE_MAP]
    dropped = [name for name in names if name not in MODULE_MAP]
    return sorted(set(mapped)), sorted(set(dropped))


def extract_version(row: dict[str, Any]) -> IRVersion:
    """Build an :class:`IRVersion`.

    ``status`` and ``sharing`` use the same vocabulary in both systems, so they
    carry across unchanged.
    """
    return IRVersion(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        name=(row.get("name") or "").strip(),
        description=row.get("description") or None,
        status=(row.get("status") or "open").strip(),
        effective_date=row.get("effective_date"),
        sharing=(row.get("sharing") or "none").strip(),
        wiki_page_title=row.get("wiki_page_title") or None,
        created_at=as_utc(row.get("created_on")),
        updated_at=as_utc(row.get("updated_on")),
    )


def extract_category(row: dict[str, Any]) -> IRCategory:
    """Build an :class:`IRCategory` from an ``issue_categories`` row."""
    return IRCategory(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        name=(row.get("name") or "").strip(),
        assigned_to_ref=str(row["assigned_to_id"]) if row.get("assigned_to_id") else None,
    )


def extract_membership(
    row: dict[str, Any],
    role_refs: list[str],
    principal_kind: PrincipalKind,
) -> IRMembership:
    """Build an :class:`IRMembership` from a ``members`` row.

    Redmine keeps people and groups in one table, so the caller decides which
    kind this membership belongs to.
    """
    return IRMembership(
        source_ref=str(row["id"]),
        project_ref=str(row["project_id"]),
        principal_ref=str(row["user_id"]),
        principal_kind=principal_kind,
        role_refs=list(role_refs),
    )


# --------------------------------------------------------------------------
# Custom fields
# --------------------------------------------------------------------------

# Redmine's custom_fields.type discriminator. Only issue fields have a target.
ISSUE_CUSTOM_FIELD_TYPE = "IssueCustomField"

# One YAML list item, as Redmine serialises possible_values.
_YAML_ITEM_RE = re.compile(r"^\s*-\s+(.*?)\s*$")


def parse_possible_values(raw: str | None) -> list[str]:
    """Return the choices of a list-format custom field.

    Redmine stores them as a serialised Ruby YAML array. Parsed by hand rather
    than with a YAML library: the payload is a flat list of strings, and a full
    parser would be a dependency and an attack surface for the sake of one
    column.
    """
    if not raw:
        return []
    values: list[str] = []
    for line in raw.splitlines():
        if line.strip() in {"---", ""}:
            continue
        match = _YAML_ITEM_RE.match(line)
        if match:
            values.append(_strip_quotes(match.group(1)))
        else:
            # Very old instances stored a plain newline-separated list.
            values.append(_strip_quotes(line.strip()))
    return [value for value in values if value]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def metadata_key(name: str) -> str:
    """Turn a custom field name into a JSON key.

    Snake case, matching how the rest of Specivo's JSONB keys are written. A
    name with no usable characters — one written entirely in a non-Latin script,
    say — has no sensible key, so the caller is expected to supply one.
    """
    key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return key


def json_schema_for(row: dict[str, Any], choices: list[str] | None = None) -> dict[str, Any]:
    """Translate a Redmine field format into a JSON Schema fragment.

    ``user`` and ``version`` fields hold a reference. They become integers
    because the value stored is a Specivo id, resolved once the referenced
    entity has been imported.

    An unrecognised format falls back to a string, which stores the value
    faithfully even when its shape is not understood.
    """
    field_format = (row.get("field_format") or "string").strip()
    multiple = bool(row.get("multiple"))

    if field_format in {"list", "enumeration"}:
        item: dict[str, Any] = {"type": "string"}
        if choices:
            item["enum"] = list(choices)
        return {"type": "array", "items": item} if multiple else item

    if field_format in {"user", "version"}:
        item = {"type": "integer"}
        return {"type": "array", "items": item} if multiple else item

    simple: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "date": {"type": "string", "format": "date"},
        "link": {"type": "string", "format": "uri"},
        "text": {"type": "string"},
        "string": {"type": "string"},
    }
    schema = dict(simple.get(field_format, {"type": "string"}))

    if field_format in {"string", "text", "link"}:
        if row.get("min_length"):
            schema["minLength"] = row["min_length"]
        if row.get("max_length"):
            schema["maxLength"] = row["max_length"]

    return {"type": "array", "items": schema} if multiple else schema


def extract_custom_field(
    row: dict[str, Any],
    tracker_refs: list[str],
    project_refs: list[str],
    choices: list[str] | None = None,
    key: str | None = None,
) -> IRCustomField:
    """Build an :class:`IRCustomField` with its JSON Schema fragment."""
    name = (row.get("name") or "").strip()
    return IRCustomField(
        source_ref=str(row["id"]),
        name=name,
        key=key or metadata_key(name) or f"field_{row['id']}",
        field_format=(row.get("field_format") or "string").strip(),
        json_schema=json_schema_for(row, choices),
        is_required=bool(row.get("is_required")),
        multiple=bool(row.get("multiple")),
        is_for_all=bool(row.get("is_for_all")),
        tracker_refs=list(tracker_refs),
        project_refs=list(project_refs),
    )
