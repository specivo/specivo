"""The permission catalogue: groups, plain-language labels, dependencies and the Non member ceiling.

This module is the single description of every project permission for
people. The keys themselves are the ``Permission`` enum in
``permission_service``; this module says what each one means, how they are
grouped in the role editor, which ones need others to make sense, and which
ones the builtin Non member role may ever hold. The role service, the REST
API (``GET /api/v1/admin/roles/permissions/``), the web role editor and the
user guide all read it from here.

It is pure: no database, no I/O. Labels and descriptions are lazy, so they are
translated when they are rendered, in the locale active at that moment.

Dependencies
------------
``PERMISSION_REQUIRES`` lists each permission's *direct* prerequisites ("edit
issues" needs "view issues"). ``normalise_permissions`` adds the transitive
closure of every requested key, so a saved role can never end up able to
edit what it cannot see. The map is acyclic, and every role the seed and the
migrations create is already closed under it.

Full access
-----------
``"*"`` (``FULL_ACCESS``) grants every permission, including ones added in
later versions. It is stored alone: a list holding ``"*"`` normalises to
``["*"]``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from specivo.core.exceptions import ValidationError
from specivo.core.i18n import LazyString
from specivo.core.i18n import gettext_lazy as _l
from specivo.services.permission_service import Permission

#: Stored in ``roles.permissions`` to grant every permission, present and future.
FULL_ACCESS = "*"


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    """One permission as people see it.

    ``enforced`` is False for a permission no code checks yet; the editor
    still offers it (imported roles carry it) but marks it "Not used yet".
    """

    key: Permission
    label: LazyString
    description: LazyString
    enforced: bool = True


@dataclass(frozen=True, slots=True)
class PermissionGroup:
    """A heading in the role editor.

    ``module`` names the project module the group's permissions depend on
    (``issue_tracking``, ``wiki``, ``time_tracking``), or None when they apply
    whatever modules a project has enabled.
    """

    key: str
    label: LazyString
    module: str | None
    permissions: tuple[PermissionInfo, ...]


P = Permission

PERMISSION_GROUPS: tuple[PermissionGroup, ...] = (
    PermissionGroup(
        key="issues",
        label=_l("Issues"),
        module="issue_tracking",
        permissions=(
            PermissionInfo(P.VIEW_ISSUES, _l("View issues"), _l("See the project's issues and their history.")),
            PermissionInfo(P.ADD_ISSUES, _l("Create issues"), _l("Open new issues in the project.")),
            PermissionInfo(
                P.EDIT_ISSUES,
                _l("Edit issues"),
                _l("Change an issue's fields, status, assignee and description."),
            ),
            PermissionInfo(P.DELETE_ISSUES, _l("Delete issues"), _l("Delete issues permanently.")),
            PermissionInfo(
                P.SET_ISSUES_PRIVATE,
                _l("Make issues private"),
                _l("Mark issues as private, so only their author and assignee (and administrators) can see them."),
            ),
            PermissionInfo(
                P.MANAGE_ISSUE_RELATIONS,
                _l("Manage issue relations"),
                _l("Add and remove links between issues, such as blocks or relates to."),
            ),
            PermissionInfo(
                P.MANAGE_SUBTASKS,
                _l("Manage subtasks"),
                _l("Reserved for subtask management; changing an issue's parent is covered by Edit issues."),
                enforced=False,
            ),
        ),
    ),
    PermissionGroup(
        key="comments",
        label=_l("Comments"),
        module="issue_tracking",
        permissions=(
            PermissionInfo(P.ADD_ISSUE_NOTES, _l("Add comments"), _l("Write comments on issues.")),
            PermissionInfo(
                P.EDIT_OWN_NOTES,
                _l("Edit own comments"),
                _l("Add and remove attachments on comments the user wrote."),
            ),
            PermissionInfo(
                P.EDIT_NOTES,
                _l("Edit any comment"),
                _l("Add and remove attachments on anyone's comments."),
            ),
            PermissionInfo(
                P.DELETE_OWN_NOTES,
                _l("Delete own comments"),
                _l("Reserved: comments cannot be deleted yet."),
                enforced=False,
            ),
            PermissionInfo(
                P.DELETE_NOTES,
                _l("Delete any comment"),
                _l("Reserved: comments cannot be deleted yet."),
                enforced=False,
            ),
            PermissionInfo(
                P.VIEW_PRIVATE_NOTES,
                _l("View private comments"),
                _l("Read comments marked as private."),
            ),
        ),
    ),
    PermissionGroup(
        key="wiki",
        label=_l("Wiki"),
        module="wiki",
        permissions=(
            PermissionInfo(P.VIEW_WIKI, _l("View wiki"), _l("Read the project's wiki pages.")),
            PermissionInfo(
                P.MANAGE_WIKI,
                _l("Create and edit wiki pages"),
                _l("Create, edit, move and tag wiki pages and their attachments."),
            ),
            PermissionInfo(P.DELETE_WIKI_PAGES, _l("Delete wiki pages"), _l("Delete wiki pages.")),
        ),
    ),
    PermissionGroup(
        key="planning",
        label=_l("Planning"),
        module="issue_tracking",
        permissions=(
            PermissionInfo(P.MANAGE_VERSIONS, _l("Manage versions"), _l("Create, edit, close and delete versions.")),
            PermissionInfo(
                P.MANAGE_SPRINTS,
                _l("Manage sprints"),
                _l("Create, start, complete, edit and delete sprints."),
            ),
            PermissionInfo(
                P.MANAGE_RECURRING_TASKS,
                _l("Manage recurring tasks"),
                _l("Create, edit, skip and delete recurring tasks, which open issues on a schedule."),
            ),
        ),
    ),
    PermissionGroup(
        key="time",
        label=_l("Time"),
        module="time_tracking",
        permissions=(
            PermissionInfo(P.VIEW_TIME_ENTRIES, _l("View time entries"), _l("See time logged in the project.")),
            PermissionInfo(P.LOG_TIME, _l("Log time"), _l("Log time on the project and its issues.")),
            PermissionInfo(
                P.MANAGE_TIME_ENTRIES,
                _l("Edit any time entry"),
                _l("Edit and delete time entries other people logged."),
            ),
        ),
    ),
    PermissionGroup(
        key="ai_usage",
        label=_l("AI usage"),
        module=None,
        permissions=(
            PermissionInfo(
                P.VIEW_AI_USAGE,
                _l("View AI usage"),
                _l("See AI usage logged in the project; people always see their own entries."),
            ),
            PermissionInfo(P.LOG_AI_USAGE, _l("Log AI usage"), _l("Log AI usage in the user's own name.")),
            PermissionInfo(
                P.LOG_AI_USAGE_FOR_OTHERS,
                _l("Log AI usage for others"),
                _l("Log AI usage in another project member's name."),
            ),
            PermissionInfo(
                P.MANAGE_AI_USAGE,
                _l("Edit any AI usage entry"),
                _l("Edit and exclude AI usage entries other people logged."),
            ),
        ),
    ),
    PermissionGroup(
        key="project",
        label=_l("Project"),
        module=None,
        permissions=(
            PermissionInfo(
                P.MANAGE_PROJECT,
                _l("Manage project settings"),
                _l("Change the project's settings, modules, tags, metadata schemas, webhooks and repositories."),
            ),
            PermissionInfo(
                P.MANAGE_MEMBERS,
                _l("Manage members"),
                _l("Add and remove project members and change their roles."),
            ),
        ),
    ),
)

#: Every permission in catalogue order.
CATALOGUE_ORDER: tuple[Permission, ...] = tuple(info.key for group in PERMISSION_GROUPS for info in group.permissions)

_ORDER_INDEX: Mapping[str, int] = MappingProxyType({key.value: i for i, key in enumerate(CATALOGUE_ORDER)})

#: Direct prerequisites. A permission not listed needs nothing else.
PERMISSION_REQUIRES: Mapping[Permission, frozenset[Permission]] = MappingProxyType(
    {
        P.ADD_ISSUES: frozenset({P.VIEW_ISSUES}),
        P.EDIT_ISSUES: frozenset({P.VIEW_ISSUES}),
        P.DELETE_ISSUES: frozenset({P.VIEW_ISSUES}),
        P.SET_ISSUES_PRIVATE: frozenset({P.VIEW_ISSUES}),
        P.MANAGE_ISSUE_RELATIONS: frozenset({P.VIEW_ISSUES}),
        P.MANAGE_SUBTASKS: frozenset({P.VIEW_ISSUES}),
        P.ADD_ISSUE_NOTES: frozenset({P.VIEW_ISSUES}),
        P.EDIT_NOTES: frozenset({P.VIEW_ISSUES}),
        P.DELETE_NOTES: frozenset({P.VIEW_ISSUES}),
        P.VIEW_PRIVATE_NOTES: frozenset({P.VIEW_ISSUES}),
        # "Own" comments presuppose writing them.
        P.EDIT_OWN_NOTES: frozenset({P.ADD_ISSUE_NOTES}),
        P.DELETE_OWN_NOTES: frozenset({P.ADD_ISSUE_NOTES}),
        P.MANAGE_VERSIONS: frozenset({P.VIEW_ISSUES}),
        P.MANAGE_SPRINTS: frozenset({P.VIEW_ISSUES}),
        # Recurring patterns open issues: without this it would be a way around add_issues.
        P.MANAGE_RECURRING_TASKS: frozenset({P.ADD_ISSUES}),
        P.MANAGE_WIKI: frozenset({P.VIEW_WIKI}),
        P.DELETE_WIKI_PAGES: frozenset({P.MANAGE_WIKI}),
        # Logging time one cannot read back is the trap migration 0036 fixed.
        P.LOG_TIME: frozenset({P.VIEW_TIME_ENTRIES}),
        P.MANAGE_TIME_ENTRIES: frozenset({P.VIEW_TIME_ENTRIES}),
        P.MANAGE_AI_USAGE: frozenset({P.VIEW_AI_USAGE}),
        P.LOG_AI_USAGE_FOR_OTHERS: frozenset({P.LOG_AI_USAGE}),
    }
)

#: The only permissions the builtin Non member role may hold. An allow-list, so
#: a permission added later stays out until someone adds it here on purpose.
#: Rule of thumb: no Full access, no project or member management, no delete,
#: nothing that edits other people's data and nothing that exposes private
#: content to every signed-in user.
NON_MEMBER_ALLOWED: frozenset[Permission] = frozenset(
    {
        P.VIEW_ISSUES,
        P.ADD_ISSUES,
        P.EDIT_ISSUES,
        P.MANAGE_ISSUE_RELATIONS,
        P.MANAGE_SUBTASKS,
        P.ADD_ISSUE_NOTES,
        P.EDIT_OWN_NOTES,
        P.VIEW_WIKI,
        P.MANAGE_WIKI,
        P.VIEW_TIME_ENTRIES,
        P.LOG_TIME,
        P.VIEW_AI_USAGE,
        P.LOG_AI_USAGE,
    }
)

_KNOWN: frozenset[str] = frozenset(p.value for p in Permission)


def _closure(permission: Permission) -> frozenset[Permission]:
    result: set[Permission] = set()
    stack = list(PERMISSION_REQUIRES.get(permission, ()))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(PERMISSION_REQUIRES.get(current, ()))
    return frozenset(result)


_CLOSURES: Mapping[Permission, frozenset[Permission]] = MappingProxyType({p: _closure(p) for p in Permission})


def requires_closure(permission: Permission) -> frozenset[Permission]:
    """Return every permission *permission* needs, directly or through another (not itself)."""
    return _CLOSURES[permission]


def normalise_permissions(requested: Iterable[str]) -> list[str]:
    """Return the permission list a role stores for *requested*.

    ``"*"`` anywhere gives ``["*"]``. Otherwise every key must be a known
    permission (else :class:`ValidationError` on ``permissions``); each one's
    prerequisites are added, duplicates dropped, and the keys returned in
    catalogue order.
    """
    keys = [str(k) for k in requested]
    if FULL_ACCESS in keys:
        return [FULL_ACCESS]
    unknown = sorted({k for k in keys if k not in _KNOWN})
    if unknown:
        raise ValidationError(
            f"Unknown permission: {', '.join(unknown)}",
            field="permissions",
            details={"unknown": unknown},
        )
    result: set[str] = set()
    for key in keys:
        permission = Permission(key)
        result.add(permission.value)
        result.update(p.value for p in _CLOSURES[permission])
    return sorted(result, key=_ORDER_INDEX.__getitem__)


def not_allowed_for_non_member(permissions: Collection[str]) -> list[str]:
    """Return the entries of *permissions* the Non member role may not hold, in catalogue order.

    ``"*"`` is always refused and listed first; unknown keys are listed last.
    """
    allowed = {p.value for p in NON_MEMBER_ALLOWED}
    refused = {str(p) for p in permissions} - allowed
    return sorted(refused, key=lambda k: (k != FULL_ACCESS, _ORDER_INDEX.get(k, len(_ORDER_INDEX)), k))


def catalogue_payload() -> dict[str, Any]:
    """Return the catalogue as plain JSON-ready data, translated in the current locale.

    Shape::

        {"groups": [{"key", "label", "module",
                     "permissions": [{"key", "label", "description", "enforced", "requires": [...]}]}],
         "non_member_allowed": [...], "total": 28}

    ``requires`` is the transitive closure, in catalogue order, so a client
    can tick every prerequisite in one step.
    """
    groups = [
        {
            "key": group.key,
            "label": str(group.label),
            "module": group.module,
            "permissions": [
                {
                    "key": info.key.value,
                    "label": str(info.label),
                    "description": str(info.description),
                    "enforced": info.enforced,
                    "requires": sorted((p.value for p in _CLOSURES[info.key]), key=_ORDER_INDEX.__getitem__),
                }
                for info in group.permissions
            ],
        }
        for group in PERMISSION_GROUPS
    ]
    return {
        "groups": groups,
        "non_member_allowed": sorted((p.value for p in NON_MEMBER_ALLOWED), key=_ORDER_INDEX.__getitem__),
        "total": len(CATALOGUE_ORDER),
    }
