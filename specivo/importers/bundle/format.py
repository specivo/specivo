"""Constants of import bundle format version 1.

The format is specified in ``docs/import-bundle/v1.md``; this module is the
part of that document the code needs: the version, the file names, and which IR
dataclass each record file carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from specivo.importers.core.ir import (
    EntityType,
    IRAttachment,
    IRCategory,
    IRCustomField,
    IRGroup,
    IRIssue,
    IRJournalEntry,
    IRMembership,
    IRProject,
    IRRelation,
    IRTimeEntry,
    IRUser,
    IRVersion,
    IRWatcher,
    IRWikiPage,
    IRWikiRedirect,
)

FORMAT_NAME = "specivo-import-bundle"
FORMAT_MAJOR = 1
FORMAT_MINOR = 0
FORMAT_VERSION = f"{FORMAT_MAJOR}.{FORMAT_MINOR}"

MANIFEST = "manifest.json"
LOOKUPS = "lookups.json"
PUBLIC_ACCESS = "public_access.json"
FILES_DIR = "files"

# Source systems a manifest may not name. The identifier map is keyed by the
# manifest's system, and ``bundle`` is what bundle imports were keyed by
# before that, so a manifest naming it would land among those rows.
RESERVED_SOURCE_SYSTEMS: frozenset[str] = frozenset({"bundle"})

# A stored file is named after the SHA-256 of its bytes, lower-case hex.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The schemas core validates against. The same files are published under
# docs/import-bundle/v1/, and a unit test keeps the two copies identical.
SCHEMA_DIR = Path(__file__).parent / "schemas" / "v1"
SCHEMA_ID_BASE = "https://specivo.io/schemas/import-bundle/v1/"


@dataclass(frozen=True, slots=True)
class RecordFile:
    """One JSON-lines file of the bundle.

    ``entity_type`` is the namespace its ``source_ref`` values are unique in,
    and ``None`` for the two record types that have no ``source_ref``
    (watchers and wiki redirects, which are identified by what they join).
    """

    name: str
    ir_type: type
    entity_type: EntityType | None

    @property
    def stem(self) -> str:
        """The file name without ``.jsonl``: the key used in the manifest's counts."""
        return self.name.removesuffix(".jsonl")

    @property
    def schema(self) -> str:
        return f"{self.stem}.schema.json"


USERS = RecordFile("users.jsonl", IRUser, EntityType.USER)
GROUPS = RecordFile("groups.jsonl", IRGroup, EntityType.GROUP)
PROJECTS = RecordFile("projects.jsonl", IRProject, EntityType.PROJECT)
MEMBERSHIPS = RecordFile("memberships.jsonl", IRMembership, EntityType.MEMBERSHIP)
VERSIONS = RecordFile("versions.jsonl", IRVersion, EntityType.VERSION)
CATEGORIES = RecordFile("categories.jsonl", IRCategory, EntityType.CATEGORY)
CUSTOM_FIELDS = RecordFile("custom_fields.jsonl", IRCustomField, EntityType.CUSTOM_FIELD)
ISSUES = RecordFile("issues.jsonl", IRIssue, EntityType.ISSUE)
JOURNALS = RecordFile("journals.jsonl", IRJournalEntry, EntityType.JOURNAL)
RELATIONS = RecordFile("relations.jsonl", IRRelation, EntityType.RELATION)
WIKI_PAGES = RecordFile("wiki_pages.jsonl", IRWikiPage, EntityType.WIKI_PAGE)
WIKI_REDIRECTS = RecordFile("wiki_redirects.jsonl", IRWikiRedirect, None)
WATCHERS = RecordFile("watchers.jsonl", IRWatcher, None)
ATTACHMENTS = RecordFile("attachments.jsonl", IRAttachment, EntityType.ATTACHMENT)
TIME_ENTRIES = RecordFile("time_entries.jsonl", IRTimeEntry, EntityType.TIME_ENTRY)

# Every record file, in the order a reader checks them: each file refers only
# to files before it, except where a record points at another of its own kind
# (a parent) or text names a record by placeholder.
RECORD_FILES: tuple[RecordFile, ...] = (
    USERS,
    GROUPS,
    PROJECTS,
    MEMBERSHIPS,
    VERSIONS,
    CATEGORIES,
    CUSTOM_FIELDS,
    ISSUES,
    JOURNALS,
    RELATIONS,
    WIKI_PAGES,
    WIKI_REDIRECTS,
    WATCHERS,
    ATTACHMENTS,
    TIME_ENTRIES,
)

# The lists in lookups.json, with the id-map namespace of each.
LOOKUP_LISTS: dict[str, EntityType] = {
    "trackers": EntityType.TRACKER,
    "statuses": EntityType.STATUS,
    "priorities": EntityType.PRIORITY,
    "activities": EntityType.ACTIVITY,
    "roles": EntityType.ROLE,
}

# Files a bundle may contain at its top level besides files/.
KNOWN_FILES: frozenset[str] = frozenset({MANIFEST, LOOKUPS, PUBLIC_ACCESS, *(f.name for f in RECORD_FILES)})

# IR field names that the format spells differently, as (IR class, IR field)
# to JSON property. The IR's opaque storage locator is, in a bundle, the hash
# that names the file under files/.
RENAMED_FIELDS: dict[tuple[type, str], str] = {
    (IRAttachment, "storage_key"): "sha256",
}

# The one property every record schema has that no IR dataclass carries:
# source data with no place in Specivo, kept with the record it came from.
EXTRA = "extra"
