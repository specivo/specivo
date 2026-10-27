"""Account events of single sign-on: one function per event.

Each function writes one ``specivo.sso`` log line with the subject hashed
(:func:`subject_hash`), never the raw ``sub``. They are the single place an
event is recorded, so the security audit events (``SSO_USER_PROVISIONED``,
``SSO_ACCOUNT_LINKED``, ``SSO_ACCOUNT_UNLINKED``) can be added here without
touching the callers. The same holds for the mapping events
(``SSO_ROLES_SYNCED``, ``SSO_GROUPS_OVERAGE``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Literal

from specivo.services.sso.log import logger

__all__ = [
    "LinkMethod",
    "UnlinkedBy",
    "account_linked",
    "account_unlinked",
    "admin_demotion_skipped",
    "groups_overage",
    "login_refused_policy",
    "roles_synced",
    "subject_hash",
    "user_provisioned",
]

#: How an identity came to be linked (``UserIdentity.linked_via`` without ``jit``).
LinkMethod = Literal["user_link", "auto_email", "admin"]
#: Who removed an identity.
UnlinkedBy = Literal["user", "admin"]


def subject_hash(subject: str) -> str:
    """The first 16 hex characters of SHA-256(sub): enough to correlate log lines, not to identify."""
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]


def user_provisioned(*, provider_slug: str, user_id: int, subject: str) -> None:
    """A user was created just in time from an external identity."""
    logger.info(
        "sso.user.provisioned provider=%s user_id=%s sub=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
    )


def account_linked(*, provider_slug: str, user_id: int, subject: str, method: LinkMethod) -> None:
    """An external identity was linked to an existing user."""
    logger.info(
        "sso.account.linked provider=%s user_id=%s sub=%s method=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        method,
    )


def account_unlinked(
    *,
    provider_slug: str,
    user_id: int,
    subject: str,
    by: UnlinkedBy,
    actor_id: int,
    sessions_revoked: int,
) -> None:
    """An external identity was removed from a user, by the user or by an admin."""
    logger.info(
        "sso.account.unlinked provider=%s user_id=%s sub=%s by=%s actor_id=%s sessions_revoked=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        by,
        actor_id,
        sessions_revoked,
    )


def _ids(values: Iterable[int]) -> str:
    return ",".join(str(v) for v in sorted(values)) or "-"


def login_refused_policy(
    *,
    provider_slug: str,
    user_id: int | None,
    subject: str,
    code: str,
    reason: str,
    sessions_revoked: int,
) -> None:
    """The provider's access policy refused a login (domain, required groups, group overage)."""
    logger.warning(
        "sso.login.refused_policy provider=%s user_id=%s sub=%s code=%s reason=%s sessions_revoked=%s",
        provider_slug,
        user_id if user_id is not None else "-",
        subject_hash(subject),
        code,
        reason,
        sessions_revoked,
    )


def groups_overage(*, provider_slug: str, user_id: int | None, subject: str, denied: bool) -> None:
    """Entra ID sent a group overage marker: the groups are unknown and nothing was synced."""
    logger.warning(
        "sso.groups.overage provider=%s user_id=%s sub=%s denied=%s hint=%s",
        provider_slug,
        user_id if user_id is not None else "-",
        subject_hash(subject),
        denied,
        "the user is in too many groups for the token; in Entra ID emit only "
        '"Groups assigned to the application" or use app roles (the roles claim)',
    )


def roles_synced(
    *,
    provider_slug: str,
    user_id: int,
    subject: str,
    admin_before: bool,
    admin_after: bool,
    groups_added: Iterable[int],
    groups_removed: Iterable[int],
) -> None:
    """The admin flag or the SSO-managed user-group memberships changed (user group ids)."""
    logger.info(
        "sso.roles.synced provider=%s user_id=%s sub=%s admin_before=%s admin_after=%s "
        "groups_added=%s groups_removed=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        admin_before,
        admin_after,
        _ids(groups_added),
        _ids(groups_removed),
    )


def admin_demotion_skipped(*, provider_slug: str, user_id: int, subject: str) -> None:
    """A demotion was skipped because it would leave no active admin who can sign in."""
    logger.warning(
        "sso.roles.admin_demotion_skipped provider=%s user_id=%s sub=%s reason=last_admin",
        provider_slug,
        user_id,
        subject_hash(subject),
    )
