"""Single sign-on events: one function per event, the single place each one is recorded.

Each function writes one ``specivo.sso`` log line, in every edition, with the
subject hashed (:func:`subject_hash`), never the raw ``sub``. The record also
carries structured fields (``extra``): ``event`` (the ``AuditEvent`` value
when there is one), ``request_id`` and ``audit`` (the audit details).

Events that are security audit events are also recorded through
:func:`specivo.services.security_audit_service.record_event` into the
current request's audit buffer. That write is persisted only when the
enterprise ``security_audit_log`` feature is registered, and a failure there
is logged and never reaches the caller.

Audit details hold the provider slug, the issuer (``iss``) when the caller
knows it, the hashed subject (``sub_hash``), ids and counts: never tokens,
codes, secrets, claim sets or group names. The audit actor (``user_id``) is
the user for their own actions (including what happens during their login),
the admin for admin actions, and nobody for what the IdP starts.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any, Literal

from specivo.services.security_audit_service import AuditEvent, current_request_id, record_event
from specivo.services.sso.log import logger

__all__ = [
    "LinkMethod",
    "UnlinkedBy",
    "account_linked",
    "account_unlinked",
    "admin_demotion_skipped",
    "backchannel_logout",
    "groups_overage",
    "login_refused_policy",
    "logout",
    "provider_changed",
    "roles_synced",
    "session_rejected",
    "subject_hash",
    "user_deprovisioned",
    "user_provisioned",
]

#: How an identity came to be linked (``UserIdentity.linked_via`` without ``jit``).
LinkMethod = Literal["user_link", "auto_email", "admin"]
#: Who removed an identity.
UnlinkedBy = Literal["user", "admin"]


def subject_hash(subject: str) -> str:
    """The first 16 hex characters of SHA-256(sub): enough to correlate log lines, not to identify."""
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]


def _details(
    *, provider_slug: str | None, issuer: str | None = None, subject: str | None = None, **values: Any
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if provider_slug is not None:
        details["provider"] = provider_slug
    if issuer is not None:
        details["iss"] = issuer
    if subject is not None:
        details["sub_hash"] = subject_hash(subject)
    details.update(values)
    return details


def _emit(
    level: int,
    message: str,
    *args: object,
    event: AuditEvent | str,
    actor_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write the log line and, for an ``AuditEvent``, the audit event. Never raises from the audit side."""
    logger.log(
        level,
        message,
        *args,
        extra={"event": str(event), "request_id": current_request_id(), "audit": details or {}},
    )
    if not isinstance(event, AuditEvent):
        return
    try:
        record_event(event, user_id=actor_id, details=details)
    except Exception as exc:  # noqa: BLE001 - the audit trail must never break the action it records
        logger.warning("sso.audit.failed event=%s error=%s", event.value, type(exc).__name__)


def _ids(values: Iterable[int]) -> str:
    return ",".join(str(v) for v in sorted(values)) or "-"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def user_provisioned(*, provider_slug: str, user_id: int, subject: str, issuer: str | None = None) -> None:
    """A user was created just in time from an external identity."""
    _emit(
        logging.INFO,
        "sso.user.provisioned provider=%s user_id=%s sub=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        event=AuditEvent.SSO_USER_PROVISIONED,
        actor_id=user_id,
        details=_details(provider_slug=provider_slug, issuer=issuer, subject=subject, user_id=user_id),
    )


def account_linked(
    *,
    provider_slug: str,
    user_id: int,
    subject: str,
    method: LinkMethod,
    issuer: str | None = None,
    actor_id: int | None = None,
) -> None:
    """An external identity was linked to an existing user.

    ``actor_id`` is the admin for an admin link; otherwise the user is the actor.
    """
    _emit(
        logging.INFO,
        "sso.account.linked provider=%s user_id=%s sub=%s method=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        method,
        event=AuditEvent.SSO_ACCOUNT_LINKED,
        actor_id=actor_id if actor_id is not None else user_id,
        details=_details(provider_slug=provider_slug, issuer=issuer, subject=subject, user_id=user_id, method=method),
    )


def account_unlinked(
    *,
    provider_slug: str,
    user_id: int,
    subject: str,
    by: UnlinkedBy,
    actor_id: int,
    sessions_revoked: int,
    issuer: str | None = None,
) -> None:
    """An external identity was removed from a user, by the user or by an admin."""
    _emit(
        logging.INFO,
        "sso.account.unlinked provider=%s user_id=%s sub=%s by=%s actor_id=%s sessions_revoked=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        by,
        actor_id,
        sessions_revoked,
        event=AuditEvent.SSO_ACCOUNT_UNLINKED,
        actor_id=actor_id,
        details=_details(
            provider_slug=provider_slug,
            issuer=issuer,
            subject=subject,
            user_id=user_id,
            by=by,
            sessions_revoked=sessions_revoked,
        ),
    )


def user_deprovisioned(
    *,
    user_id: int,
    actor_id: int | None,
    reason: str,
    provider_slug: str | None = None,
    issuer: str | None = None,
    subject: str | None = None,
    sessions_revoked: int = 0,
    api_keys_deactivated: int | None = None,
) -> None:
    """A user lost access because the IdP or an admin removed them (deactivated, sessions ended).

    ``actor_id`` is the admin who did it, or ``None`` when the IdP (or a
    scheduled sync) did; ``reason`` is a short code.
    """
    _emit(
        logging.INFO,
        "sso.user.deprovisioned provider=%s user_id=%s sub=%s actor_id=%s reason=%s sessions_revoked=%s",
        provider_slug if provider_slug is not None else "-",
        user_id,
        subject_hash(subject) if subject is not None else "-",
        actor_id if actor_id is not None else "-",
        reason,
        sessions_revoked,
        event=AuditEvent.USER_DEPROVISIONED,
        actor_id=actor_id,
        details=_details(
            provider_slug=provider_slug,
            issuer=issuer,
            subject=subject,
            user_id=user_id,
            reason=reason,
            sessions_revoked=sessions_revoked,
            **({"api_keys_deactivated": api_keys_deactivated} if api_keys_deactivated is not None else {}),
        ),
    )


# ---------------------------------------------------------------------------
# Access policy and role mapping
# ---------------------------------------------------------------------------


def login_refused_policy(
    *,
    provider_slug: str,
    user_id: int | None,
    subject: str,
    code: str,
    reason: str,
    sessions_revoked: int,
) -> None:
    """The provider's access policy refused a login (domain, required groups, group overage).

    Log line only: the refusal reaches the audit trail as ``SSO_LOGIN_FAILURE``
    with the same code, written by the callback route.
    """
    _emit(
        logging.WARNING,
        "sso.login.refused_policy provider=%s user_id=%s sub=%s code=%s reason=%s sessions_revoked=%s",
        provider_slug,
        user_id if user_id is not None else "-",
        subject_hash(subject),
        code,
        reason,
        sessions_revoked,
        event="sso_login_refused_policy",
    )


def groups_overage(*, provider_slug: str, user_id: int | None, subject: str, denied: bool) -> None:
    """Entra ID sent a group overage marker: the groups are unknown and nothing was synced."""
    _emit(
        logging.WARNING,
        "sso.groups.overage provider=%s user_id=%s sub=%s denied=%s hint=%s",
        provider_slug,
        user_id if user_id is not None else "-",
        subject_hash(subject),
        denied,
        "the user is in too many groups for the token; in Entra ID emit only "
        '"Groups assigned to the application" or use app roles (the roles claim)',
        event=AuditEvent.SSO_GROUPS_OVERAGE,
        actor_id=user_id,
        details=_details(provider_slug=provider_slug, subject=subject, user_id=user_id, denied=denied),
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
    added = sorted(groups_added)
    removed = sorted(groups_removed)
    _emit(
        logging.INFO,
        "sso.roles.synced provider=%s user_id=%s sub=%s admin_before=%s admin_after=%s "
        "groups_added=%s groups_removed=%s",
        provider_slug,
        user_id,
        subject_hash(subject),
        admin_before,
        admin_after,
        _ids(added),
        _ids(removed),
        event=AuditEvent.SSO_ROLES_SYNCED,
        actor_id=user_id,
        details=_details(
            provider_slug=provider_slug,
            subject=subject,
            user_id=user_id,
            admin_before=admin_before,
            admin_after=admin_after,
            groups_added=added,
            groups_removed=removed,
        ),
    )


def admin_demotion_skipped(*, provider_slug: str, user_id: int, subject: str) -> None:
    """A demotion was skipped because it would leave no active admin who can sign in."""
    _emit(
        logging.WARNING,
        "sso.roles.admin_demotion_skipped provider=%s user_id=%s sub=%s reason=last_admin",
        provider_slug,
        user_id,
        subject_hash(subject),
        event="sso_admin_demotion_skipped",
    )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def logout(*, provider_slug: str, issuer: str, user_id: int | None, subject: str, idp_redirect: bool) -> None:
    """A user signed out of an SSO session; ``idp_redirect`` when the browser goes on to the IdP."""
    _emit(
        logging.INFO,
        "sso.logout provider=%s sub=%s idp_redirect=%s",
        provider_slug,
        subject_hash(subject),
        "true" if idp_redirect else "false",
        event=AuditEvent.SSO_LOGOUT,
        actor_id=user_id,
        details=_details(
            provider_slug=provider_slug, issuer=issuer, subject=subject, user_id=user_id, idp_redirect=idp_redirect
        ),
    )


def backchannel_logout(
    *, provider_slug: str, issuer: str, subject: str | None, by: Literal["sid", "sub"], revoked: int
) -> None:
    """The IdP ended sessions over the back channel. No actor: the IdP started it."""
    _emit(
        logging.INFO,
        "sso.backchannel_logout provider=%s sub=%s by=%s revoked=%d",
        provider_slug,
        subject_hash(subject) if subject is not None else "-",
        by,
        revoked,
        event=AuditEvent.SSO_BACKCHANNEL_LOGOUT,
        actor_id=None,
        details=_details(provider_slug=provider_slug, issuer=issuer, subject=subject, by=by, sessions_revoked=revoked),
    )


def session_rejected(
    *, provider_slug: str, issuer: str, user_id: int, subject: str | None, reason: str, idp_error: str | None = None
) -> None:
    """A session ended when it was re-checked at the IdP. No actor: the IdP decided.

    ``reason`` is ``idp_refresh_rejected`` (the IdP refused the refresh token)
    or ``idp_check_failed`` (the IdP could not be asked and the provider says
    ``deny``); ``idp_error`` is the IdP's sanitised OAuth error code.
    """
    _emit(
        logging.INFO,
        "sso.session.rejected provider=%s user_id=%s sub=%s reason=%s idp_error=%s",
        provider_slug,
        user_id,
        subject_hash(subject) if subject is not None else "-",
        reason,
        idp_error or "-",
        event=AuditEvent.SSO_SESSION_REJECTED,
        actor_id=None,
        details=_details(
            provider_slug=provider_slug,
            issuer=issuer,
            subject=subject,
            user_id=user_id,
            reason=reason,
            **({"idp_error": idp_error} if idp_error else {}),
        ),
    )


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

_PROVIDER_EVENTS = frozenset(
    {
        AuditEvent.SSO_PROVIDER_CREATED,
        AuditEvent.SSO_PROVIDER_UPDATED,
        AuditEvent.SSO_PROVIDER_DELETED,
        AuditEvent.SSO_PROVIDER_SECRET_CHANGED,
    }
)


def provider_changed(
    event: AuditEvent,
    *,
    provider_id: int,
    provider_slug: str,
    issuer: str | None,
    actor_id: int | None,
    fields: Iterable[str] = (),
    counts: dict[str, int] | None = None,
) -> None:
    """A provider was created, updated, deleted or got a new client secret.

    ``fields`` are field *names*; values, the secret above all, never reach
    this function. ``counts`` are what a delete cleaned up.
    """
    if event not in _PROVIDER_EVENTS:
        raise ValueError(f"not a provider event: {event!r}")
    names = list(fields)
    extra = dict(counts or {})
    details = _details(provider_slug=provider_slug, issuer=issuer, provider_id=provider_id, fields=names)
    details.update(extra)
    _emit(
        logging.INFO,
        "sso.provider.%s provider_id=%s slug=%s actor=%s fields=%s%s",
        event.value.removeprefix("sso_provider_"),
        provider_id,
        provider_slug,
        actor_id,
        ",".join(names),
        "".join(f" {key}={value}" for key, value in extra.items()),
        event=event,
        actor_id=actor_id,
        details=details,
    )
