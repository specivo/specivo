"""Carry the source's non-member and anonymous access over, without opening anything.

Redmine decides what people without a membership may see with two builtin
roles that apply instance-wide, behind a "sign-in required" setting. Specivo
decides it per project: a seeded read-only Non member role for signed-in
non-members, and a project's ``anonymous_permissions`` for visitors without an
account, which has no effect until an administrator turns the instance-wide
anonymous access switch on.

The adapter has already translated the source into that shape
(:class:`~specivo.importers.core.ir.IRPublicAccess`). What this loader adds is
the part that keeps an import from publishing a tracker by itself:

* **The instance switch is never touched.** Projects are opted in, and the
  report lists them, so an administrator sees exactly what turning the switch
  on would expose. While the switch is on, opting a project in would expose
  it at once, so nothing is applied and the projects are listed for the
  administrator to opt in by hand. That check is made by the service under
  the lock the switch itself takes, so the switch being turned on during an
  import cannot slip an opt-in past its confirmation list.
* **An opt-in is applied once per project.** A re-run finds it recorded in the
  id map and leaves the project as it is, so an administrator who narrowed or
  cleared it after the first run is not overruled.
* **Only an empty list is filled,** and only on a public project.
* **Specivo's Non member role is not changed.** It is instance-wide and
  read-only; the source's Non member permissions are compared with it and
  every difference is reported.

Every opt-in is written to the security audit log with reason ``import``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from specivo.importers.core.ir import EntityType, IRPublicAccess
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.user_loader import ensure_import_account
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.services.anonymous_access_service import (
    ImportedAnonymousPermissionsResult,
    apply_imported_anonymous_permissions,
    is_anonymous_access_enabled,
)

logger = logging.getLogger(__name__)

# Report sections.
NOTE_ANONYMOUS_SOURCE = "anonymous_access_in_source"
NOTE_ANONYMOUS_PROJECTS = "projects_readable_anonymously_once_the_switch_is_on"
NOTE_ANONYMOUS_SWITCH = "anonymous_access_switch"
NOTE_NON_MEMBER_DIFFERENCES = "non_member_role_differences"
NOTE_PERMISSIONS_NOT_CARRIED = "permissions_not_carried_over"


async def load_public_access(ctx: PhaseContext) -> None:
    """Opt the imported public projects in to anonymous reading where the source allowed it, and report."""
    reader = getattr(ctx.adapter, "extract_public_access", None)
    if not callable(reader):
        return
    # None: the source can say nothing about it (a bundle without
    # public_access.json), which is not the same as saying nobody had access.
    access: IRPublicAccess | None = await reader()
    if access is None:
        return

    await _report_non_member_role(ctx, access)
    for line in access.not_carried_over:
        ctx.summary.add_note(NOTE_PERMISSIONS_NOT_CARRIED, line)

    ctx.summary.add_note(NOTE_ANONYMOUS_SOURCE, access.anonymous_source_setting)

    refused_by_switch = False
    for project_ref in ctx.project_refs:
        permissions = access.anonymous_permissions.get(project_ref)
        if not permissions:
            continue
        project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
        project = await ctx.session.get(Project, project_id) if project_id is not None else None
        if project is None:
            continue
        result = await _opt_in(ctx, project, project_ref, permissions)
        refused_by_switch |= result is ImportedAnonymousPermissionsResult.SWITCH_ON

    # Read for the report only. Whether a project may be opted in is decided
    # by apply_imported_anonymous_permissions, under the lock the switch takes.
    if refused_by_switch or await is_anonymous_access_enabled(ctx.session):
        ctx.summary.add_note(
            NOTE_ANONYMOUS_SWITCH,
            "Anonymous access is on, so the import opted in no project while it was;"
            " opt the projects marked above in by hand once they have been reviewed",
        )
    else:
        ctx.summary.add_note(
            NOTE_ANONYMOUS_SWITCH,
            "Anonymous access is off and the import left it off: nothing is readable without"
            " an account until an administrator turns it on after reviewing the projects above",
        )


async def _opt_in(
    ctx: PhaseContext, project: Project, project_ref: str, permissions: list[str]
) -> ImportedAnonymousPermissionsResult | None:
    """Apply one project's anonymous permissions, or explain why not.

    Returns the service's result, or None when an earlier run already did it.
    """
    granted = ", ".join(permissions)

    if await ctx.id_map.get(ctx.session, EntityType.ANONYMOUS_ACCESS, project_ref) is not None:
        current = ", ".join(project.anonymous_permissions or []) or "none"
        ctx.summary.record_skipped(EntityType.ANONYMOUS_ACCESS)
        ctx.summary.add_note(
            NOTE_ANONYMOUS_PROJECTS, f"{project.key}: {granted} (applied by an earlier run; now: {current})"
        )
        return None

    actor = await ensure_import_account(ctx)
    result = await apply_imported_anonymous_permissions(ctx.session, project, permissions, actor)

    if result is ImportedAnonymousPermissionsResult.APPLIED:
        await ctx.id_map.put(ctx.session, EntityType.ANONYMOUS_ACCESS, project_ref, "projects", project.id)
        ctx.summary.record_created(EntityType.ANONYMOUS_ACCESS)
        ctx.summary.add_note(NOTE_ANONYMOUS_PROJECTS, f"{project.key}: {granted}")
        ctx.tick()
    elif result is ImportedAnonymousPermissionsResult.SWITCH_ON:
        ctx.warn(
            "Anonymous access is on in this instance, so the project was not opted in;"
            " opting it in now would make it readable without an account immediately",
            project=project.key,
            permissions=granted,
        )
        ctx.summary.add_note(
            NOTE_ANONYMOUS_PROJECTS, f"{project.key}: {granted} (not applied: switch on, opt in by hand)"
        )
    elif result is ImportedAnonymousPermissionsResult.ALREADY_SET:
        ctx.summary.add_note(
            NOTE_ANONYMOUS_PROJECTS,
            f"{project.key}: {granted} (not applied: already set to {', '.join(project.anonymous_permissions)})",
        )
    elif result is ImportedAnonymousPermissionsResult.NOT_PUBLIC:
        ctx.warn("Project is not public, so it was not opted in to anonymous reading", project=project.key)
    return result


async def _report_non_member_role(ctx: PhaseContext, access: IRPublicAccess) -> None:
    """Compare the source's Non member role with Specivo's and report every difference.

    Specivo's role is left exactly as it is: it applies to every public
    project in the instance, not just the imported ones, so widening it would
    open projects the source never knew about, and narrowing it would take
    access away from them.
    """
    role = (
        await ctx.session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER).limit(1))
    ).scalar_one_or_none()
    specivo = set(role.permissions or []) if role is not None else set()
    source = set(access.non_member_permissions)
    name = role.name if role is not None else "Non member"

    for permission in sorted(source - specivo):
        ctx.summary.add_note(
            NOTE_NON_MEMBER_DIFFERENCES,
            f"{permission}: granted by the source's {access.non_member_source_name} role,"
            f" not by Specivo's {name} role (not carried over)",
        )
    for permission in sorted(specivo - source):
        ctx.summary.add_note(
            NOTE_NON_MEMBER_DIFFERENCES,
            f"{permission}: granted by Specivo's {name} role, not by the source's"
            f" {access.non_member_source_name} role (signed-in non-members of public projects can do more here)",
        )
