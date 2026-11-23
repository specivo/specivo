"""MCP tools: wiki pages and their sections."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.mcp.tools._common import _extract_section, _get_project, _log_tool, _require_permission, _wiki_svc
from specivo.mcp.tools.links import _link_lines
from specivo.mcp.tools.tags import _add_tags, _clean_tag_names
from specivo.models.user import User
from specivo.services.markdown_sections import find_section, split_into_sections
from specivo.services.security_audit_service import AuditEvent


async def _read_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    metadata_only: bool = False,
    search: str | None = None,
) -> str:
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "view_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    lines = [
        f"Wiki: {page.title}",
        f"Slug: {page.slug}",
        f"Version: {content.version}",
        f"Lock version: {page.lock_version}",
    ]
    lines.extend(await _link_lines(session, user, ("wiki_page", page.id)))
    if not metadata_only:
        text = content.text or ""
        if search and text:
            section = _extract_section(text, search)
            if section is not None:
                lines.append("")
                lines.append(f"Content (section matching '{search}'):")
                lines.append(section)
            else:
                lines.append("")
                lines.append(f"Content ('{search}' not found in text):")
                lines.append(text)
        else:
            lines.append("")
            lines.append("Content:")
            lines.append(text)
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_READ,
        "read_wiki",
        {"project_key": project_key, "slug": slug},
        project_id=project.id,
    )
    return "\n".join(lines)


async def _list_wiki_pages(
    session: AsyncSession,
    user: User,
    project_key: str,
) -> str:
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "view_wiki")
    pages = await _wiki_svc.list_pages(session, project.id)
    lines = [f"Wiki pages for {project.key} ({len(pages)} total):", ""]
    for p in pages:
        lines.append(f"  {p.slug}  —  {p.title}")  # noqa: PERF401
    if not pages:
        lines.append("  (none)")
    await _log_tool(session, user, AuditEvent.WIKI_LISTED, "list_wiki_pages", {"project_key": project_key})
    return "\n".join(lines)


async def _edit_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    search_text: str,
    replace_text: str,
) -> str:
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""
    if search_text not in current:
        raise ValidationError(
            f"search text not found in wiki page '{page.title}'.\nContent length: {len(current)} chars.",
            field="search_text",
        )
    new_text = current.replace(search_text, replace_text, 1)
    page, new_content = await _wiki_svc.update_page(session, page.id, new_text, user, lock_version=page.lock_version)
    await session.flush()
    await _log_tool(session, user, AuditEvent.WIKI_UPDATED, "edit_wiki", {"project_key": project_key, "slug": slug})
    return (
        f"Updated wiki page '{page.title}' (version {new_content.version}).\n"
        f"Replaced '{search_text}' -> '{replace_text}'."
    )


# ---------------------------------------------------------------------------
# Wiki — create
# ---------------------------------------------------------------------------


async def _create_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    title: str,
    text: str,
    parent_slug: str | None = None,
    tags: list[str] | None = None,
) -> str:
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "manage_wiki")
    tag_names = _clean_tag_names(tags)
    page, content = await _wiki_svc.create_page(
        session,
        project.id,
        title,
        text,
        user,
        parent_slug=parent_slug,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_CREATED,
        "create_wiki",
        {"project_key": project_key, "slug": page.slug, "title": title},
        project_id=project.id,
    )
    result = (
        f"Created wiki page '{page.title}' (slug: {page.slug}).\n"
        f"Version: {content.version}\nLock version: {page.lock_version}"
    )
    if tag_names:
        target_ref = f"wiki:{project.key}/{page.slug}"
        applied = await _add_tags(session, user, "create_wiki", "wiki", project, page.id, target_ref, tag_names)
        result += "\nTags: " + ", ".join(applied)
    return result


# ---------------------------------------------------------------------------
# Wiki — delete / restore
# ---------------------------------------------------------------------------


async def _delete_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    cascade_children: bool = False,
) -> str:
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "delete_wiki_pages")
    page, _content = await _wiki_svc.get_page(session, project.id, slug)
    deleted_ids = await _wiki_svc.delete_page(session, page.id, user, cascade_children=cascade_children)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_DELETED,
        "delete_wiki",
        {"project_key": project_key, "slug": slug, "deleted_ids": deleted_ids},
        project_id=project.id,
    )
    count = len(deleted_ids)
    suffix = "s" if count > 1 else ""
    return f"Deleted {count} wiki page{suffix} (slug: {slug}, cascade={cascade_children})."


async def _restore_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    cascade: bool = True,
) -> str:

    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "delete_wiki_pages")

    page = await _wiki_svc.find_deleted_page(session, project.id, slug)

    restored_ids = await _wiki_svc.restore_page(session, page.id, project_id=project.id, cascade=cascade)
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_RESTORED,
        "restore_wiki",
        {"project_key": project_key, "slug": slug, "restored_ids": restored_ids},
        project_id=project.id,
    )
    count = len(restored_ids)
    suffix = "s" if count > 1 else ""
    return f"Restored {count} wiki page{suffix} (slug: {slug}, cascade={cascade})."


# ---------------------------------------------------------------------------
# Wiki — metadata (parent, title, protected)
# ---------------------------------------------------------------------------


async def _update_wiki_metadata(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    parent_slug: str | None = None,
    title: str | None = None,
    protected: bool | None = None,
) -> str:
    """Update wiki page metadata (parent, title, protected) without editing content."""
    if parent_slug is None and title is None and protected is None:
        raise ValidationError("at least one of parent_slug, title, or protected must be provided.")

    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, _content = await _wiki_svc.get_page(session, project.id, slug)

    changes: list[str] = []
    audit_changes: dict[str, dict[str, object]] = {}

    # --- title (rename) ---
    if title is not None:
        old_title = page.title
        old_slug = page.slug
        page = await _wiki_svc.rename_page(session, page.id, title, page.lock_version)
        changes.append(f"title -> '{page.title}' (new slug: {page.slug})")
        audit_changes["title"] = {"old": old_title, "new": page.title}
        audit_changes["slug"] = {"old": old_slug, "new": page.slug}

    # --- parent ---
    if parent_slug is not None:
        old_parent_id = page.parent_id
        if parent_slug == "":
            await _wiki_svc.set_parent(session, page, None)
            changes.append("parent -> (root)")
        else:
            parent_page, _parent_content = await _wiki_svc.get_page(session, project.id, parent_slug)
            await _wiki_svc.set_parent(session, page, parent_page)
            changes.append(f"parent -> '{parent_slug}'")
        audit_changes["parent_id"] = {"old": old_parent_id, "new": page.parent_id}

    # --- protected ---
    if protected is not None:
        old_protected = page.protected
        page.protected = protected
        await session.flush()
        changes.append(f"protected -> {protected}")
        audit_changes["protected"] = {"old": old_protected, "new": protected}

    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_UPDATED,
        "update_wiki_metadata",
        {"project_key": project_key, "slug": slug, "changes": audit_changes},
        project_id=project.id,
    )
    return f"Updated wiki page '{page.title}': " + ", ".join(changes) + "."


# ---------------------------------------------------------------------------
# Wiki — section operations
# ---------------------------------------------------------------------------


async def _append_wiki(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    text: str,
    position: str = "end",
) -> str:
    """Append text to a wiki page at a given position.

    ``position`` is either ``"end"`` (default) or ``"after:## Heading Name"``
    to insert after a specific section.
    """
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""

    if position == "end":
        new_text = current.rstrip("\n") + "\n\n" + text if current.strip() else text
    elif position.startswith("after:"):
        heading_query = position[len("after:") :]
        sections = split_into_sections(current)
        match = find_section(sections, heading_query)
        if match is None:
            raise ValidationError(
                f"heading '{heading_query}' not found in wiki page '{page.title}'.\n"
                f"Content length: {len(current)} chars.",
                field="position",
            )
        sec_idx, section = match
        sec_level = section["level"]

        # Find the end of this section (including children):
        # next section at same-or-higher level
        insert_before_line: int | None = None
        for s in sections[sec_idx + 1 :]:
            if s["heading"] is not None and s["level"] <= sec_level:
                insert_before_line = s["start"]
                break

        lines = current.split("\n")
        if insert_before_line is not None:
            # Insert text before the next same-or-higher-level heading
            before = "\n".join(lines[:insert_before_line]).rstrip("\n")
            after = "\n".join(lines[insert_before_line:])
            new_text = before + "\n\n" + text + "\n\n" + after
        else:
            # No next same-level heading; append at end
            new_text = current.rstrip("\n") + "\n\n" + text
    else:
        raise ValidationError(f"invalid position '{position}'. Use 'end' or 'after:## Heading Name'.", field="position")

    page, new_content = await _wiki_svc.update_page(
        session,
        page.id,
        new_text,
        user,
        lock_version=page.lock_version,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_UPDATED,
        "append_wiki",
        {"project_key": project_key, "slug": slug, "position": position},
        project_id=project.id,
    )
    return (
        f"Appended to wiki page '{page.title}' (version {new_content.version}).\n"
        f"Position: {position}. New content length: {len(new_text)} chars."
    )


async def _read_wiki_section(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    heading: str,
    include_children: bool = True,
) -> str:
    """Read a single section from a wiki page by heading."""
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "view_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    text = content.text or ""

    sections = split_into_sections(text)
    match = find_section(sections, heading)
    if match is None:
        raise ValidationError(
            f"heading '{heading}' not found in wiki page '{page.title}'.\n"
            f"Available headings: {', '.join(s['heading'] for s in sections if s['heading'])}",
            field="heading",
        )

    sec_idx, section = match
    sec_level = section["level"]
    lines = text.split("\n")

    if include_children:
        # Include everything until the next same-or-higher-level heading
        end_line: int | None = None
        for s in sections[sec_idx + 1 :]:
            if s["heading"] is not None and s["level"] <= sec_level:
                end_line = s["start"]
                break
        if end_line is not None:  # noqa: SIM108
            result_lines = lines[section["start"] : end_line]
        else:
            result_lines = lines[section["start"] :]
    else:
        # Stop at the first sub-heading
        end_line = None
        for s in sections[sec_idx + 1 :]:
            if s["heading"] is not None:
                end_line = s["start"]
                break
        if end_line is not None:  # noqa: SIM108
            result_lines = lines[section["start"] : end_line]
        else:
            result_lines = lines[section["start"] :]

    result_text = "\n".join(result_lines).strip()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_READ,
        "read_wiki_section",
        {"project_key": project_key, "slug": slug, "heading": heading},
        project_id=project.id,
    )
    return f"Wiki: {page.title} (section)\n\n{result_text}"


async def _replace_wiki_section(
    session: AsyncSession,
    user: User,
    project_key: str,
    slug: str,
    heading: str,
    text: str,
) -> str:
    """Replace a section's body while preserving the heading line."""
    project = await _get_project(session, user, project_key)
    await _wiki_svc.require_module_enabled(session, project.id)
    await _require_permission(session, user, project.id, "manage_wiki")
    page, content = await _wiki_svc.get_page(session, project.id, slug)
    current = content.text or ""

    sections = split_into_sections(current)
    match = find_section(sections, heading)
    if match is None:
        raise ValidationError(
            f"heading '{heading}' not found in wiki page '{page.title}'.\n"
            f"Available headings: {', '.join(s['heading'] for s in sections if s['heading'])}",
            field="heading",
        )

    sec_idx, section = match
    sec_level = section["level"]
    lines = current.split("\n")

    # Find the end of this section (next same-or-higher-level heading)
    end_line: int | None = None
    for s in sections[sec_idx + 1 :]:
        if s["heading"] is not None and s["level"] <= sec_level:
            end_line = s["start"]
            break

    # Build new content: heading line + new body + rest
    heading_line = section["heading"]
    before = "\n".join(lines[: section["start"]])
    if end_line is not None:  # noqa: SIM108
        after = "\n".join(lines[end_line:])
    else:
        after = ""

    parts = []
    if before.strip():
        parts.append(before.rstrip("\n"))
    parts.append(heading_line + "\n\n" + text.strip())
    if after.strip():
        parts.append(after.lstrip("\n"))
    new_text = "\n\n".join(parts)

    page, new_content = await _wiki_svc.update_page(
        session,
        page.id,
        new_text,
        user,
        lock_version=page.lock_version,
    )
    await session.flush()
    await _log_tool(
        session,
        user,
        AuditEvent.WIKI_UPDATED,
        "replace_wiki_section",
        {"project_key": project_key, "slug": slug, "heading": heading},
        project_id=project.id,
    )
    return (
        f"Replaced section '{heading}' in wiki page '{page.title}' "
        f"(version {new_content.version}).\n"
        f"New content length: {len(new_text)} chars."
    )
