"""Behaviour hints for every MCP tool: ``readOnlyHint``, ``destructiveHint``, ``idempotentHint``.

MCP clients use these hints to decide what they may do without asking. Hermes, for
example, replays only read-only tools after a dropped Streamable HTTP session, asks for
approval before any other tool of an untrusted server, and refuses them in cron jobs.
A client treats anything but an explicit ``readOnlyHint: true`` as a write, so a read
tool without hints costs an approval.

The table is the one place the classification lives. Each ``@mcp.tool`` in
``specivo.mcp.server`` passes ``annotations=TOOL_ANNOTATIONS["<name>"]``, and
``tests/unit/test_mcp_tool_annotations.py`` fails when a registered tool has no entry.

Rules:

- **read-only**: the tool never changes state (lists, shows, reads, search, reports).
- **destructive**: the tool can delete or irreversibly change existing data.
- **idempotent**: calling it again with the same arguments has no further effect
  (set or update by key, deletes, transitions that are refused when repeated).
- Tools with an ``op`` argument get the hints of their most dangerous op, since hints
  are static and cannot depend on arguments.
- ``openWorldHint`` is false everywhere: every tool acts on this instance only.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations


def _read() -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )


def _write(*, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


_READ_ONLY = (
    "specivo_whoami",
    "specivo_setup_guide",
    "specivo_list_projects",
    "specivo_list_issues",
    "specivo_list_lookups",
    "specivo_list_members",
    "specivo_list_comments",
    "specivo_list_relations",
    "specivo_list_tags",
    "specivo_list_versions",
    "specivo_list_version_issues",
    "specivo_list_sprints",
    "specivo_list_sprint_issues",
    "specivo_list_wiki_pages",
    "specivo_list_metadata_schemas",
    "specivo_list_recurring_patterns",
    "specivo_list_recurrence_occurrences",
    "specivo_show_issue",
    "specivo_issue_development",
    "specivo_search",
    "specivo_get_links",
    "specivo_read_wiki",
    "specivo_read_wiki_section",
    "specivo_ai_usage_report",
)

# Deletes, and changes that cannot be undone through the tools. Repeating one has no
# further effect (the second call finds nothing to delete or is refused).
_DESTRUCTIVE = (
    "specivo_delete_wiki",
    "specivo_delete_metadata_schema",
    "specivo_delete_version",
    "specivo_delete_tag",
    "specivo_delete_recurring_pattern",
    "specivo_remove_relation",
    "specivo_complete_sprint",
    # Removes an untouched issue already generated for the skipped occurrence.
    "specivo_skip_recurrence_occurrence",
)

# Writes that leave the same state when repeated with the same arguments.
_IDEMPOTENT = (
    "specivo_update_issue",
    "specivo_move_issue",
    "specivo_replace_wiki_section",
    "specivo_update_wiki_metadata",
    "specivo_restore_wiki",
    "specivo_update_version",
    "specivo_update_tag",
    "specivo_update_sprint",
    "specivo_start_sprint",
    "specivo_update_recurring_pattern",
    "specivo_update_metadata_schema",
    "specivo_add_relation",
    # Ops get, add, remove, set: adding a tag twice or removing it twice changes nothing.
    "specivo_tag",
    # Ops list, get, update, exclude, include: none of them adds or deletes entries.
    "specivo_ai_usage",
    # Ops set, get: setting the same external id and parent again changes nothing.
    "specivo_agent_session",
)

# Writes that add something or apply a relative change each time they run.
_ADDITIVE = (
    "specivo_create_issue",
    "specivo_create_wiki",
    "specivo_create_tag",
    "specivo_create_version",
    "specivo_create_sprint",
    "specivo_create_metadata_schema",
    "specivo_create_recurring_pattern",
    "specivo_add_comment",
    "specivo_append_wiki",
    # Search-and-replace of the first occurrence: a repeat can hit the next one.
    "specivo_edit_description",
    "specivo_edit_wiki",
    "specivo_log_time",
    "specivo_log_ai_usage",
    # Ops get, start, stop, discard: start and stop log time.
    "specivo_timer",
    # Ops get, set, delete, append, remove: append grows the list on every call. A
    # deleted key can be set again, so the tool is not destructive.
    "specivo_metadata",
    # Ops list, create, update, deactivate: create adds an account; nothing is ever
    # deleted (a deactivated account is switched on again with update).
    "specivo_ai_billing_accounts",
)

# Ops list, presets, add, update, end, delete, members, add_member, end_member,
# remove_member: add and add_member are not idempotent, and delete and
# remove_member remove a plan or a seat for good, together with its share of past
# spend reports.
_ADDITIVE_AND_DESTRUCTIVE = ("specivo_ai_subscriptions",)


TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    **{name: _read() for name in _READ_ONLY},
    **{name: _write(destructive=True, idempotent=True) for name in _DESTRUCTIVE},
    **{name: _write(idempotent=True) for name in _IDEMPOTENT},
    **{name: _write() for name in _ADDITIVE},
    **{name: _write(destructive=True) for name in _ADDITIVE_AND_DESTRUCTIVE},
}
