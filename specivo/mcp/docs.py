"""Dynamic setup guide generation for the Specivo MCP server.

Builds a concise agent configuration guide from the actual registered
MCP tools, keeping output under ~3 KB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# Tool-table rows are maintained here so the guide stays accurate even
# if a tool docstring is reworded.  Order matches the logical grouping.
_TOOL_SUMMARIES: dict[str, str] = {
    "specivo_whoami": "Return authenticated user identity (user_id, login, etc.)",
    "specivo_list_projects": "List all visible projects",
    "specivo_list_issues": "List issues with filtering, sorting, and optional sprint_id scope",
    "specivo_show_issue": "Show issue details; use search= for section extraction",
    "specivo_create_issue": "Create an issue (call list_lookups first for IDs)",
    "specivo_update_issue": "Update issue fields; lock_version handled automatically",
    "specivo_edit_description": "Search-and-replace in issue description (token-efficient)",
    "specivo_add_comment": "Add a journal comment to an issue",
    "specivo_search": "Full-text search across issues and wiki",
    "specivo_list_wiki_pages": "List wiki pages for a project",
    "specivo_read_wiki": "Read a wiki page by slug",
    "specivo_create_wiki": "Create a new wiki page (slug auto-derived from title)",
    "specivo_update_wiki_metadata": "Update wiki page parent, title, or protected flag",
    "specivo_delete_wiki": "Soft-delete a wiki page (moves to trash)",
    "specivo_restore_wiki": "Restore a wiki page from trash",
    "specivo_edit_wiki": "Search-and-replace in wiki page content",
    "specivo_append_wiki": "Append text to a wiki page (end or after a heading)",
    "specivo_read_wiki_section": "Read a single section from a wiki page by heading",
    "specivo_replace_wiki_section": "Replace a section body in a wiki page (heading preserved)",
    "specivo_list_lookups": "Get tracker/status/priority/activity IDs for this instance",
    "specivo_list_members": "List project members with roles and user IDs",
    "specivo_list_metadata_schemas": "Discover custom metadata fields for a project",
    "specivo_list_relations": "List relations for an issue",
    "specivo_add_relation": "Create a relation between two issues",
    "specivo_remove_relation": "Remove a relation by ID",
    "specivo_log_time": "Log time against a project or issue",
    "specivo_list_versions": "List versions/milestones",
    "specivo_create_version": "Create a version/milestone",
    "specivo_update_version": "Update a version/milestone",
    "specivo_delete_version": "Delete a version (fails if issues reference it)",
    "specivo_list_sprints": "List sprints/iterations for a project",
    "specivo_create_sprint": "Create a new sprint (starts as planned)",
    "specivo_update_sprint": "Update sprint name, goal, or dates",
    "specivo_start_sprint": "Start a planned sprint (one active per project)",
    "specivo_complete_sprint": "Complete an active sprint with velocity snapshot",
    "specivo_setup_guide": "Return this setup guide",
}


def generate_setup_guide(fmt: str = "generic", mcp_server: FastMCP | None = None) -> str:
    """Build the agent setup guide.

    Args:
        fmt: "claude" for CLAUDE.md, "cursor" for .cursorrules, "generic" otherwise.
        mcp_server: If provided, tool table is generated from actual registered tools.
    """
    tool_rows = _build_tool_table(mcp_server)
    header = _format_header(fmt)
    footer = _format_footer(fmt)

    return f"""{header}
## Key concepts

- **project_key** -- Uppercase identifier, e.g. `ACME`. Never use project name or numeric ID.
- **issue_ref** -- Display key like `ACME-12`. Never pass raw numeric IDs.
- **slug** -- URL-friendly wiki page identifier. Use `list_wiki_pages` to discover.
- **IDs are instance-specific** -- tracker/status/priority/activity IDs vary per install. Call `list_lookups`.

## Tools

| Tool | Description |
|------|-------------|
{tool_rows}

## Standard workflows

### Find context before coding
1. `specivo_search(query, project_key)` -- find relevant issues/wiki
2. `specivo_show_issue(issue_ref)` -- read issue details
3. `specivo_read_wiki(project_key, slug)` -- read knowledge base

### Create an issue
1. `specivo_list_lookups()` -- get tracker_id, priority_id
2. `specivo_list_members(project_key)` -- get assigned_to_id
3. `specivo_create_issue(project_key, tracker_id, subject, ...)`

### Update issue when done
1. `specivo_list_lookups()` -- get status_id for resolved/closed
2. `specivo_update_issue(issue_ref, status_id=N, done_ratio=100, notes="...")`
3. `specivo_log_time(project_key, hours, activity_id, issue_ref)`

### Patch description or wiki
1. `specivo_show_issue(issue_ref, search="keyword")` -- find exact text
2. `specivo_edit_description(issue_ref, search_text, replace_text)`

### Build large wiki pages incrementally
1. `specivo_create_wiki(project_key, title, text)` -- initial content
2. `specivo_append_wiki(project_key, slug, text)` -- add sections at end
3. `specivo_append_wiki(project_key, slug, text, position="after:## Heading")` -- insert after heading

### Read and update specific wiki sections
1. `specivo_read_wiki_section(project_key, slug, heading="## Design")` -- read one section
2. `specivo_replace_wiki_section(project_key, slug, heading="## Design", text="...")` -- replace body

## File uploads (attachments)

MCP tools work with text. For binary file uploads (images, PDFs, etc.), use the REST API directly via shell.

**Step 1**: Get the issue/page numeric ID:
```
specivo_show_issue(issue_ref="ACME-12", metadata_only=true)  # note the id field
```

**Step 2**: Upload via curl (works on macOS, Linux, Windows 10+):
```bash
curl -X POST https://YOUR-SPECIVO-HOST/api/v1/attachments/ \\
  -H "Authorization: Bearer spv_YOUR_API_KEY" \\
  -F "file=@/path/to/file.png" \\
  -F "container_type=Issue" \\
  -F "container_id=ID"
```

For wiki pages use `container_type=WikiPage`. On Windows use `curl.exe` instead of `curl`.

Reuse the API key (`spv_...`) and host from your MCP connection config.
Max file size is configured per instance (nginx `client_max_body_size`).

## REST API authentication

When you need to call the Specivo REST API directly (e.g. file uploads), reuse the
API key and host from your MCP connection config. The key is the `spv_...` token
in your `Authorization` header, and the host is the base URL of the MCP SSE endpoint.

Specivo uses **only** `Authorization: Bearer spv_...` for API authentication.
Other methods (X-Api-Key, query params, etc.) are NOT supported.
All API URLs require a trailing slash.

```bash
curl -H "Authorization: Bearer spv_YOUR_API_KEY" \\
  https://YOUR-SPECIVO-HOST/api/v1/projects/ACME/wiki/
```

## Anti-patterns

- Do NOT pass numeric IDs for `issue_ref` -- always use display key format `ACME-12`.
- Do NOT assume tracker/status/priority IDs -- call `specivo_list_lookups` first.
- Do NOT replace full description via `update_issue` -- use `edit_description` for patches.
- Do NOT call `log_time` without a valid `activity_id` from `list_lookups`.
- Do NOT use `X-Api-Key` or `?key=` for REST API auth -- only `Authorization: Bearer spv_...` works.
- Do NOT omit the trailing slash on REST API URLs -- FastAPI requires it.
{footer}"""


def _format_header(fmt: str) -> str:
    """Return format-specific header."""
    summary = "Specivo -- self-hosted platform for project tracking, knowledge base, and AI-safe automation."
    if fmt == "claude":
        return f"""# Specivo MCP -- Agent Setup Guide

> {summary}

Add to your CLAUDE.md or project instructions."""
    if fmt == "cursor":
        return f"""# Specivo MCP Rules

> {summary}

Add to your .cursorrules file."""
    return f"""# Specivo MCP -- Agent Setup Guide

> {summary}"""


def _format_footer(fmt: str) -> str:
    """Return format-specific footer."""
    if fmt == "claude":
        return """
## Connection

```json
{
  "mcpServers": {
    "specivo": {
      "type": "sse",
      "url": "https://your-specivo-host/mcp/sse/",
      "headers": { "Authorization": "Bearer spv_your_api_key_here" }
    }
  }
}
```"""
    return ""


def _build_tool_table(mcp_server: FastMCP | None = None) -> str:
    """Build markdown table rows from registered tools or fallback summaries."""
    summaries = dict(_TOOL_SUMMARIES)

    # Merge any tools from the actual server that are not in our static map
    if mcp_server is not None:
        try:
            registered = mcp_server._tool_manager._tools
            for name in registered:
                if name not in summaries:
                    desc = registered[name].description or ""
                    summaries[name] = desc.split("\n")[0][:80]
        except Exception:
            pass

    rows: list[str] = []
    for name, desc in summaries.items():
        rows.append(f"| `{name}` | {desc} |")
    return "\n".join(rows)
