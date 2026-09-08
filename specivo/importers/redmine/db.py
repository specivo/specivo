"""Read-only access to a Redmine database.

Redmine runs on either MySQL or PostgreSQL and the schema is identical on both
(Rails owns the DDL), so one set of table definitions serves both and only the
driver differs.

The tables are declared by hand rather than reflected. Reflection needs a live
database, which would put a Redmine instance in the way of every unit test, and
it hides the moment a column this importer depends on disappears: a hand-written
definition fails with a name we recognise instead of a KeyError deep in a
mapping function. Only the columns the importer reads are declared.

Verified against Redmine 7.0.1.

Nothing here writes. The importer is pointed at somebody's live tracker, so the
connection exists only to SELECT.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Select,
    String,
    Table,
    Text,
    make_url,
)
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

# Async drivers this importer knows how to load. A URL naming a sync driver is
# upgraded to its async equivalent, since an operator copying a connection
# string out of Redmine's database.yml has no reason to know about that.
_ASYNC_DRIVERS: dict[str, str] = {
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mysql+pymysql": "mysql+aiomysql",
    "mysql+mysqldb": "mysql+aiomysql",
    "mysql2": "mysql+aiomysql",
}

# Driver package needed per dialect, and the extra that installs it.
_DRIVER_REQUIREMENTS: dict[str, tuple[str, str]] = {
    "aiomysql": ("aiomysql", "specivo[importers]"),
    "asyncpg": ("asyncpg", "specivo"),
}

# Comfortably inside MySQL's default eight-hour idle timeout.
_MYSQL_POOL_RECYCLE_SECONDS = 3600

metadata = MetaData()

# --------------------------------------------------------------------------
# Projects and lookups
# --------------------------------------------------------------------------

projects = Table(
    "projects",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("description", Text),
    Column("homepage", String),
    Column("is_public", Boolean),
    Column("parent_id", Integer),
    Column("created_on", DateTime),
    Column("updated_on", DateTime),
    Column("identifier", String),
    Column("status", Integer),
    # Redmine keeps projects in a nested set; ordering by lft yields parents
    # before children, which is exactly the order the importer needs.
    Column("lft", Integer),
    Column("rgt", Integer),
    Column("inherit_members", Boolean),
    Column("default_version_id", Integer),
    Column("default_assigned_to_id", Integer),
)

enabled_modules = Table(
    "enabled_modules",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer),
    Column("name", String),
)

projects_trackers = Table(
    "projects_trackers",
    metadata,
    Column("project_id", Integer),
    Column("tracker_id", Integer),
)

trackers = Table(
    "trackers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("position", Integer),
    Column("is_in_roadmap", Boolean),
    # Bitmask of disabled core fields. See extract.decode_disabled_core_fields.
    Column("fields_bits", Integer),
    Column("default_status_id", Integer),
    Column("description", String),
    Column("private_by_default", Boolean),
)

issue_statuses = Table(
    "issue_statuses",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("is_closed", Boolean),
    Column("position", Integer),
    Column("default_done_ratio", Integer),
    Column("description", String),
)

# Priorities, time-tracking activities and document categories share one table,
# discriminated by ``type``.
enumerations = Table(
    "enumerations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("position", Integer),
    Column("is_default", Boolean),
    Column("type", String),
    Column("active", Boolean),
    Column("project_id", Integer),
    Column("parent_id", Integer),
)

issue_categories = Table(
    "issue_categories",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer),
    Column("name", String),
    Column("assigned_to_id", Integer),
)

versions = Table(
    "versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer),
    Column("name", String),
    Column("description", String),
    Column("effective_date", Date),
    Column("created_on", DateTime),
    Column("updated_on", DateTime),
    Column("wiki_page_title", String),
    Column("status", String),
    Column("sharing", String),
)

settings = Table(
    "settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("value", Text),
    Column("updated_on", DateTime),
)

# --------------------------------------------------------------------------
# Principals
# --------------------------------------------------------------------------

# Users and groups share this table, discriminated by ``type``
# ("User", "Group", "AnonymousUser").
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("login", String),
    Column("firstname", String),
    Column("lastname", String),
    Column("admin", Boolean),
    Column("status", Integer),
    Column("last_login_on", DateTime),
    Column("language", String),
    Column("created_on", DateTime),
    Column("updated_on", DateTime),
    Column("type", String),
    Column("must_change_passwd", Boolean),
    Column("passwd_changed_on", DateTime),
)

# Redmine moved addresses off users in 3.2; the default row is the account's
# primary address.
email_addresses = Table(
    "email_addresses",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer),
    Column("address", String),
    Column("is_default", Boolean),
    Column("notify", Boolean),
)

groups_users = Table(
    "groups_users",
    metadata,
    Column("group_id", Integer),
    Column("user_id", Integer),
)

roles = Table(
    "roles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("position", Integer),
    Column("assignable", Boolean),
    # 0 = a normal role, 1 = Non member, 2 = Anonymous.
    Column("builtin", Integer),
    # Serialised Ruby YAML. Deliberately not translated; see extract.extract_role.
    Column("permissions", Text),
    Column("issues_visibility", String),
    Column("users_visibility", String),
    Column("time_entries_visibility", String),
)

members = Table(
    "members",
    metadata,
    Column("id", Integer, primary_key=True),
    # Points at a user or a group: both live in ``users``.
    Column("user_id", Integer),
    Column("project_id", Integer),
    Column("created_on", DateTime),
    Column("mail_notification", Boolean),
)

member_roles = Table(
    "member_roles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("member_id", Integer),
    Column("role_id", Integer),
    # Set when the grant came from a group or an ancestor project.
    Column("inherited_from", Integer),
)

# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------

issues = Table(
    "issues",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tracker_id", Integer),
    Column("project_id", Integer),
    Column("subject", String),
    Column("description", Text),
    Column("due_date", Date),
    Column("category_id", Integer),
    Column("status_id", Integer),
    Column("assigned_to_id", Integer),
    Column("priority_id", Integer),
    Column("fixed_version_id", Integer),
    Column("author_id", Integer),
    Column("created_on", DateTime),
    Column("updated_on", DateTime),
    Column("start_date", Date),
    Column("done_ratio", Integer),
    Column("estimated_hours", Float),
    Column("parent_id", Integer),
    Column("is_private", Boolean),
    Column("closed_on", DateTime),
)

journals = Table(
    "journals",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("journalized_id", Integer),
    Column("journalized_type", String),
    Column("user_id", Integer),
    Column("notes", Text),
    Column("created_on", DateTime),
    Column("private_notes", Boolean),
    Column("updated_on", DateTime),
    Column("updated_by_id", Integer),
)

journal_details = Table(
    "journal_details",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("journal_id", Integer),
    Column("property", String),
    Column("prop_key", String),
    Column("old_value", Text),
    # Renamed to new_value in Specivo's journal_details.
    Column("value", Text),
)

issue_relations = Table(
    "issue_relations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("issue_from_id", Integer),
    Column("issue_to_id", Integer),
    Column("relation_type", String),
    Column("delay", Integer),
)

watchers = Table(
    "watchers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("watchable_type", String),
    Column("watchable_id", Integer),
    Column("user_id", Integer),
)

# --------------------------------------------------------------------------
# Custom fields
# --------------------------------------------------------------------------

custom_fields = Table(
    "custom_fields",
    metadata,
    Column("id", Integer, primary_key=True),
    # "IssueCustomField", "UserCustomField", "ProjectCustomField", ...
    Column("type", String),
    Column("name", String),
    Column("field_format", String),
    Column("possible_values", Text),
    Column("min_length", Integer),
    Column("max_length", Integer),
    Column("is_required", Boolean),
    Column("is_for_all", Boolean),
    Column("position", Integer),
    Column("default_value", Text),
    Column("multiple", Boolean),
    Column("description", Text),
)

custom_values = Table(
    "custom_values",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("customized_type", String),
    Column("customized_id", Integer),
    Column("custom_field_id", Integer),
    Column("value", Text),
)

custom_fields_trackers = Table(
    "custom_fields_trackers",
    metadata,
    Column("custom_field_id", Integer),
    Column("tracker_id", Integer),
)

custom_fields_projects = Table(
    "custom_fields_projects",
    metadata,
    Column("custom_field_id", Integer),
    Column("project_id", Integer),
)

custom_field_enumerations = Table(
    "custom_field_enumerations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("custom_field_id", Integer),
    Column("name", String),
    Column("active", Boolean),
    Column("position", Integer),
)

# --------------------------------------------------------------------------
# Wiki
# --------------------------------------------------------------------------

wikis = Table(
    "wikis",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer),
    Column("start_page", String),
    Column("status", Integer),
)

wiki_pages = Table(
    "wiki_pages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("wiki_id", Integer),
    Column("title", String),
    Column("created_on", DateTime),
    Column("protected", Boolean),
    Column("parent_id", Integer),
)

wiki_contents = Table(
    "wiki_contents",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("page_id", Integer),
    Column("author_id", Integer),
    Column("text", Text),
    Column("comments", String),
    Column("updated_on", DateTime),
    Column("version", Integer),
)

# History. ``data`` is bytes and may be gzip-compressed, which ``compression``
# reports; the current version is duplicated here, so this table alone is the
# complete history.
wiki_content_versions = Table(
    "wiki_content_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("wiki_content_id", Integer),
    Column("page_id", Integer),
    Column("author_id", Integer),
    Column("data", LargeBinary),
    Column("compression", String),
    Column("comments", String),
    Column("updated_on", DateTime),
    Column("version", Integer),
)

wiki_redirects = Table(
    "wiki_redirects",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("wiki_id", Integer),
    Column("title", String),
    Column("redirects_to", String),
    Column("created_on", DateTime),
    Column("redirects_to_wiki_id", Integer),
)

# --------------------------------------------------------------------------
# Attachments and time tracking
# --------------------------------------------------------------------------

attachments = Table(
    "attachments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("container_id", Integer),
    Column("container_type", String),
    Column("filename", String),
    Column("disk_filename", String),
    Column("filesize", Integer),
    Column("content_type", String),
    Column("digest", String),
    Column("author_id", Integer),
    Column("created_on", DateTime),
    Column("description", String),
    # "YYYY/MM" on modern instances, empty on ones that predate the change.
    Column("disk_directory", String),
)

time_entries = Table(
    "time_entries",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer),
    Column("user_id", Integer),
    Column("issue_id", Integer),
    Column("hours", Float),
    Column("comments", String),
    Column("activity_id", Integer),
    Column("spent_on", Date),
    Column("created_on", DateTime),
    Column("updated_on", DateTime),
    Column("author_id", Integer),
)


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


class SourceDriverMissingError(RuntimeError):
    """The driver for the source database is not installed."""


def normalise_source_url(url: str | URL) -> URL:
    """Return *url* with an async driver, leaving an explicit one alone.

    Operators copy connection strings out of Redmine's ``database.yml``, which
    names no Python driver at all.

    A :class:`~sqlalchemy.engine.URL` is returned rather than a string on
    purpose. ``str(URL)`` masks the password, so round-tripping through a string
    silently replaces the credential with asterisks and the connection then
    fails authentication for no visible reason.
    """
    parsed = make_url(url)
    replacement = _ASYNC_DRIVERS.get(parsed.drivername)
    if replacement is None:
        return parsed
    return parsed.set(drivername=replacement)


def safe_url(url: str | URL) -> str:
    """Return *url* with the password masked, for logs and error messages."""
    return make_url(url).render_as_string(hide_password=True)


def _check_driver(url: URL) -> None:
    """Fail early and clearly when the source driver is not installed."""
    import importlib.util

    driver = url.get_driver_name()
    requirement = _DRIVER_REQUIREMENTS.get(driver)
    if requirement is None:
        return
    module, extra = requirement
    if importlib.util.find_spec(module) is None:
        raise SourceDriverMissingError(
            f"The {driver} driver is required to read this source database but is not installed."
            f" Install it with: uv pip install '{extra}'"
        )


def create_source_engine(url: str | URL, **engine_kwargs: Any) -> AsyncEngine:
    """Return a read-only engine for the Redmine database at *url*.

    Accepts either dialect with or without an explicit async driver.

    An import can run for hours and a source database is entitled to drop an
    idle connection in that time, so connections are checked before use. On
    PostgreSQL that is ``pool_pre_ping``. On MySQL it cannot be: SQLAlchemy's
    aiomysql adapter and aiomysql disagree about the signature of ``ping``, so
    pre-ping raises on the first checkout. Connections are recycled by age
    instead, well inside MySQL's default idle timeout, which covers the same
    risk without the broken path.
    """
    normalised = normalise_source_url(url)
    _check_driver(normalised)
    logger.info("Connecting to source database %s", safe_url(normalised))

    if normalised.get_backend_name() == "mysql":
        engine_kwargs.setdefault("pool_recycle", _MYSQL_POOL_RECYCLE_SECONDS)
    else:
        engine_kwargs.setdefault("pool_pre_ping", True)

    return create_async_engine(normalised, **engine_kwargs)


async def fetch_setting(conn: AsyncConnection, name: str) -> str | None:
    """Return one Redmine setting value, or ``None`` when it is unset.

    An unset setting means Redmine is using its built-in default, which the
    caller has to supply.
    """
    stmt = settings.select().with_only_columns(settings.c.value).where(settings.c.name == name)
    return (await conn.execute(stmt)).scalar_one_or_none()


async def stream(
    conn: AsyncConnection,
    stmt: Select,
    key_column: Column,
    batch_size: int = 500,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every row of *stmt*, a page at a time, as plain dicts.

    Pages by ``key_column > last seen`` rather than OFFSET: an instance with
    100k issues would otherwise make the database re-scan and discard a growing
    prefix for every page. *stmt* must not already carry its own ordering or
    limit.
    """
    last: Any = None
    while True:
        page = stmt
        if last is not None:
            page = page.where(key_column > last)
        page = page.order_by(key_column).limit(batch_size)

        rows = (await conn.execute(page)).mappings().all()
        if not rows:
            return

        for row in rows:
            yield dict(row)

        if len(rows) < batch_size:
            return
        last = rows[-1][key_column.name]
