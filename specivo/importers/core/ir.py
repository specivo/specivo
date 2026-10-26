"""Intermediate representation (IR) shared by every import source.

The IR is the seam that keeps source adapters and loaders independent. An
adapter reads its own database (Redmine today, RT or Jira later) and emits
these dataclasses; loaders consume them and never learn where they came from.

Two rules make that separation work:

1. **Every entity carries a ``source_ref``** — the source system's own primary
   key, stringified. It is the key used in ``import_id_map`` and the way IR
   objects reference each other (``project_ref``, ``author_ref``, ...). Loaders
   translate refs to Specivo ids through the id map, so the IR never holds a
   Specivo id.
2. **Field names describe Specivo concepts, not source concepts.** Where the
   two disagree the adapter does the translation (for example Redmine's binary
   ``is_closed`` becomes the four-way :attr:`IRStatus.category`).

Streams are flat rather than deeply nested: issues, journals, relations,
watchers and attachments are separate extraction streams so each import phase
can run in its own transaction without holding a whole project in memory. Wiki
versions are the exception — a page's history is small and is always loaded
together with the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class EntityType(StrEnum):
    """Vocabulary for ``import_id_map.entity_type``.

    Shared by adapters and loaders so a mapping written by one is found by the
    other. Values are stable identifiers and must not be renamed once an import
    has run against a database.
    """

    SYSTEM = "system"
    ROLE = "role"
    TRACKER = "tracker"
    STATUS = "status"
    PRIORITY = "priority"
    ACTIVITY = "activity"
    USER = "user"
    GROUP = "group"
    PROJECT = "project"
    VERSION = "version"
    CATEGORY = "category"
    CUSTOM_FIELD = "custom_field"
    ISSUE = "issue"
    JOURNAL = "journal"
    RELATION = "relation"
    WATCHER = "watcher"
    ATTACHMENT = "attachment"
    WIKI_PAGE = "wiki_page"
    WIKI_VERSION = "wiki_version"
    TIME_ENTRY = "time_entry"
    MEMBERSHIP = "membership"
    # A project's anonymous permissions set from the source. Recorded once per
    # project, so a re-run never reapplies what an administrator has since
    # changed.
    ANONYMOUS_ACCESS = "anonymous_access"


class ValueKind(StrEnum):
    """How a custom-field value must be resolved before it is stored.

    ``USER_REF`` and ``VERSION_REF`` values hold a source ref that only becomes
    meaningful after the referenced entity is imported, so the issue loader
    writes them verbatim and a later pass rewrites them to Specivo ids.
    """

    SCALAR = "scalar"
    USER_REF = "user_ref"
    VERSION_REF = "version_ref"


class ContainerKind(StrEnum):
    """Entity an attachment hangs off."""

    ISSUE = "issue"
    WIKI_PAGE = "wiki_page"


class PrincipalKind(StrEnum):
    """Who a membership grants roles to."""

    USER = "user"
    GROUP = "group"


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IRTracker:
    """Issue type (Redmine tracker, Jira issue type)."""

    source_ref: str
    name: str
    description: str | None = None
    is_in_roadmap: bool = True
    default_status_ref: str | None = None
    disabled_core_fields: list[str] = field(default_factory=list)
    position: int = 0
    # New issues of this type start private.
    private_by_default: bool = False


@dataclass(slots=True)
class IRStatus:
    """Issue status.

    ``category`` is one of ``backlog``, ``active``, ``done``, ``closed`` —
    Specivo's four-way grouping. Sources with a coarser notion of "closed" are
    mapped by their adapter, which may take an operator-supplied override.
    """

    source_ref: str
    name: str
    category: str
    position: int = 0
    default_done_ratio: int | None = None


@dataclass(slots=True)
class IRPriority:
    source_ref: str
    name: str
    position: int = 0
    is_default: bool = False
    active: bool = True


@dataclass(slots=True)
class IRActivity:
    """Time-tracking activity."""

    source_ref: str
    name: str
    is_default: bool = False
    active: bool = True


@dataclass(slots=True)
class IRRole:
    """A role.

    ``builtin`` follows Specivo's encoding: 0 for an ordinary role, 1 for the
    non-member role, 2 for the anonymous one. Sources without that concept leave
    it at 0.

    ``users_visibility`` (``all`` / ``members_of_visible_projects``) and
    ``time_entries_visibility`` (``all`` / ``own``) use Specivo's values; None
    means the source has no such setting and Specivo's default applies.
    """

    source_ref: str
    name: str
    builtin: int = 0
    permissions: list[str] = field(default_factory=list)
    users_visibility: str | None = None
    time_entries_visibility: str | None = None


@dataclass(slots=True)
class IRLookups:
    """Instance-wide lookup tables, small enough to load eagerly."""

    trackers: list[IRTracker] = field(default_factory=list)
    statuses: list[IRStatus] = field(default_factory=list)
    priorities: list[IRPriority] = field(default_factory=list)
    activities: list[IRActivity] = field(default_factory=list)
    roles: list[IRRole] = field(default_factory=list)


@dataclass(slots=True)
class IRPublicAccess:
    """What the source lets people without a membership read.

    Already translated into Specivo's model by the adapter: a per-project
    list of anonymous permissions rather than an instance-wide role.

    ``anonymous_allowed`` is whether the source served anonymous visitors at
    all, and ``anonymous_source_setting`` describes the setting that decided
    it, for the report. ``anonymous_permissions`` maps a project ref to what an
    anonymous visitor could read there, limited to ``view_issues`` /
    ``view_wiki``; it is empty unless anonymous access was allowed, and holds
    public projects only.

    ``non_member_permissions`` is what the source grants a signed-in
    non-member on a public project, in Specivo's names, restricted to the
    permissions Specivo has an equivalent for; ``non_member_source_name`` names
    that role for the report.

    ``not_carried_over`` lists, one human-readable line each, the source
    permissions that have no place in Specivo's model.
    """

    anonymous_allowed: bool = False
    anonymous_source_setting: str = ""
    anonymous_permissions: dict[str, list[str]] = field(default_factory=dict)
    non_member_source_name: str = "Non member"
    non_member_permissions: list[str] = field(default_factory=list)
    not_carried_over: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Principals
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IRCredential:
    """A source account's password, as the source system stored it.

    ``algorithm`` names the legacy hasher that understands the source's scheme
    (``redmine_sha1``, or an id a plugin registers); ``fields`` holds the source
    row's raw credential columns under the names that hasher reads (a salt and a
    digest, say). It is never a ready-made Specivo hash: an adapter does no
    cryptography, and the loader turns this into a stored value with
    ``PasswordHasher.from_source`` — and only when the operator asked for
    passwords to be imported and enabled the algorithm.

    ``fields`` is secret material. It must never be logged or put in a report.
    """

    algorithm: str
    fields: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"IRCredential(algorithm={self.algorithm!r}, fields=<{len(self.fields)} hidden>)"


@dataclass(slots=True)
class IRUser:
    """A person.

    ``credential`` is the source's password when the adapter can read one and
    the account authenticates locally; ``None`` for an account bound to an
    external directory (LDAP) or with no usable password. Whether it is used is
    the loader's decision: without ``--import-passwords``, or for an
    administrator, the account gets an unusable hash and the operator is told
    which logins need a reset.

    ``must_change_password`` and ``password_changed_at`` carry the source's own
    state, used only when the credential is kept.
    """

    source_ref: str
    login: str
    display_name: str
    email: str | None = None
    status: str = "active"
    is_admin: bool = False
    language: str | None = None
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    credential: IRCredential | None = None
    must_change_password: bool = False
    password_changed_at: datetime | None = None
    # True when the account authenticates against an external directory
    # (LDAP) rather than a local password, so it has no credential to carry.
    external_auth: bool = False


@dataclass(slots=True)
class IRGroup:
    """A source-side group of users.

    Becomes a Specivo ``UserGroup``, which is a membership principal in its own
    right: an :class:`IRMembership` with ``principal_kind`` ``GROUP`` names one
    of these, and the roles it is granted reach everyone in ``member_refs``.
    """

    source_ref: str
    name: str
    member_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IRMembership:
    """Role grant on a project, held by a user or a group."""

    source_ref: str
    project_ref: str
    principal_ref: str
    principal_kind: PrincipalKind
    role_refs: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Projects and project-scoped lookups
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IRProject:
    """A project. ``status`` follows Specivo's 1 active / 5 closed / 9 archived."""

    source_ref: str
    identifier: str
    name: str
    description: str | None = None
    is_public: bool = False
    parent_ref: str | None = None
    status: int = 1
    modules: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class IRVersion:
    source_ref: str
    project_ref: str
    name: str
    description: str | None = None
    status: str = "open"
    effective_date: date | None = None
    sharing: str = "none"
    wiki_page_title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class IRCategory:
    source_ref: str
    project_ref: str
    name: str
    assigned_to_ref: str | None = None


@dataclass(slots=True)
class IRCustomField:
    """A custom field definition, translated to a JSON Schema fragment.

    Specivo stores custom data in the ``issue_metadata`` JSONB column validated
    by a ``MetadataSchema``, so an adapter is responsible for turning its own
    field-format vocabulary into ``json_schema``. ``key`` is the JSON key the
    values are stored under.
    """

    source_ref: str
    name: str
    key: str
    field_format: str
    json_schema: dict[str, Any] = field(default_factory=dict)
    is_required: bool = False
    multiple: bool = False
    is_for_all: bool = False
    tracker_refs: list[str] = field(default_factory=list)
    project_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IRCustomValue:
    """One custom-field value on one issue."""

    field_ref: str
    key: str
    value: Any
    value_kind: ValueKind = ValueKind.SCALAR


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IRIssue:
    """An issue. ``description`` is already converted to Markdown.

    Cross-issue references such as Redmine's ``#123`` are left verbatim; the
    pipeline rewrites them to Specivo display keys once every issue exists.
    """

    source_ref: str
    project_ref: str
    tracker_ref: str
    status_ref: str
    priority_ref: str
    subject: str
    author_ref: str | None = None
    assigned_to_ref: str | None = None
    description: str | None = None
    parent_ref: str | None = None
    category_ref: str | None = None
    fixed_version_ref: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    estimated_hours: Decimal | None = None
    done_ratio: int = 0
    is_private: bool = False
    custom_values: list[IRCustomValue] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(slots=True)
class IRJournalDetail:
    """One field change inside a journal entry.

    ``property`` is ``attr``, ``cf``, ``attachment`` or ``relation`` — the same
    vocabulary Specivo's ``journal_details`` table uses.
    """

    property: str
    prop_key: str
    old_value: str | None = None
    new_value: str | None = None


@dataclass(slots=True)
class IRJournalEntry:
    """A comment, a set of field changes, or both.

    Sequence numbers are assigned by the loader from ``created_at`` order, not
    carried over from the source.
    """

    source_ref: str
    issue_ref: str
    user_ref: str | None = None
    notes: str | None = None
    is_private: bool = False
    created_at: datetime | None = None
    details: list[IRJournalDetail] = field(default_factory=list)


@dataclass(slots=True)
class IRRelation:
    """A relation between two issues.

    ``relation_type`` is already canonical: one of ``relates``, ``duplicates``,
    ``blocks``, ``precedes``, ``copied_to``. Adapters swap the endpoints of
    reverse forms rather than emitting them.
    """

    source_ref: str
    from_ref: str
    to_ref: str
    relation_type: str
    delay: int | None = None


@dataclass(slots=True)
class IRWatcher:
    """A user subscribed to an issue or a wiki page."""

    container_kind: ContainerKind
    container_ref: str
    user_ref: str


@dataclass(slots=True)
class IRAttachment:
    """A file attached to an issue or wiki page.

    ``storage_key`` is an opaque, source-defined locator that the adapter turns
    into a filesystem path. Size and hash are recomputed from the copied bytes,
    so ``filesize`` is advisory only.
    """

    source_ref: str
    container_kind: ContainerKind
    container_ref: str
    filename: str
    storage_key: str
    content_type: str | None = None
    filesize: int | None = None
    description: str | None = None
    author_ref: str | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------
# Wiki
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IRWikiVersion:
    """One revision of a wiki page, text already converted to Markdown."""

    source_ref: str
    version: int
    text: str
    comments: str | None = None
    author_ref: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class IRWikiPage:
    """A wiki page with its full history, ordered oldest version first."""

    source_ref: str
    project_ref: str
    title: str
    parent_ref: str | None = None
    protected: bool = False
    versions: list[IRWikiVersion] = field(default_factory=list)


# --------------------------------------------------------------------------
# Time tracking
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IRTimeEntry:
    source_ref: str
    project_ref: str
    hours: Decimal
    spent_on: date
    issue_ref: str | None = None
    user_ref: str | None = None
    activity_ref: str | None = None
    comments: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
