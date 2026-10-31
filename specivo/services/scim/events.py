"""SCIM events: one function per event, the single place each one is recorded.

Each function writes one ``specivo.scim`` log line in every edition and, for
an audit event, appends it to the request's audit buffer
(:func:`specivo.services.security_audit_service.record_event`, persisted only
with the enterprise ``security_audit_log`` feature). A failing audit write is
logged and never reaches the caller.

The actor (``user_id``) of a change made through SCIM is nobody: the identity
provider made it. Details carry ``source: scim``, the provider slug, the SCIM
id, the Specivo id, an ``action`` and the names (never the values) of the
changed fields. Configuration changes name the admin as the actor and never
carry the token.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from specivo.services.security_audit_service import AuditEvent, current_request_id, record_event

__all__ = [
    "config_changed",
    "group_changed",
    "logger",
    "request_refused",
    "user_changed",
]

logger = logging.getLogger("specivo.scim")

_USER_EVENTS = frozenset(
    {AuditEvent.SCIM_USER_CREATED, AuditEvent.SCIM_USER_UPDATED, AuditEvent.SCIM_USER_DEPROVISIONED}
)
_GROUP_EVENTS = frozenset({AuditEvent.SCIM_GROUP_CREATED, AuditEvent.SCIM_GROUP_UPDATED, AuditEvent.SCIM_GROUP_DELETED})


def _emit(message: str, *args: object, event: AuditEvent, actor_id: int | None, details: dict[str, Any]) -> None:
    logger.info(message, *args, extra={"event": str(event), "request_id": current_request_id(), "audit": details})
    try:
        record_event(event, user_id=actor_id, details=details)
    except Exception as exc:  # noqa: BLE001 - the audit trail must never break the action it records
        logger.warning("scim.audit.failed event=%s error=%s", event.value, type(exc).__name__)


def user_changed(
    event: AuditEvent,
    *,
    provider_slug: str,
    scim_id: str,
    user_id: int,
    action: str,
    fields: Iterable[str] = (),
    **counts: int,
) -> None:
    """A user was created, linked, revived, updated, reactivated, deactivated or deleted through SCIM."""
    if event not in _USER_EVENTS:
        raise ValueError(f"not a SCIM user event: {event!r}")
    names = sorted(fields)
    details: dict[str, Any] = {
        "source": "scim",
        "provider": provider_slug,
        "scim_id": scim_id,
        "user_id": user_id,
        "action": action,
        "fields": names,
        **counts,
    }
    _emit(
        "scim.user.%s provider=%s user_id=%s scim_id=%s fields=%s",
        action,
        provider_slug,
        user_id,
        scim_id,
        ",".join(names) or "-",
        event=event,
        actor_id=None,
        details=details,
    )


def group_changed(
    event: AuditEvent,
    *,
    provider_slug: str,
    scim_id: str,
    group_id: int,
    action: str,
    fields: Iterable[str] = (),
    members_added: Iterable[int] = (),
    members_removed: Iterable[int] = (),
    **counts: int,
) -> None:
    """A user group was created, updated or deleted through SCIM; members are user ids."""
    if event not in _GROUP_EVENTS:
        raise ValueError(f"not a SCIM group event: {event!r}")
    added = sorted(members_added)
    removed = sorted(members_removed)
    names = sorted(fields)
    details: dict[str, Any] = {
        "source": "scim",
        "provider": provider_slug,
        "scim_id": scim_id,
        "group_id": group_id,
        "action": action,
        "fields": names,
        "members_added": added,
        "members_removed": removed,
        **counts,
    }
    _emit(
        "scim.group.%s provider=%s group_id=%s scim_id=%s fields=%s members_added=%s members_removed=%s",
        action,
        provider_slug,
        group_id,
        scim_id,
        ",".join(names) or "-",
        len(added),
        len(removed),
        event=event,
        actor_id=None,
        details=details,
    )


def request_refused(*, provider_slug: str, scim_id: str | None, user_id: int | None, reason: str) -> None:
    """Specivo refused a SCIM change for a reason an auditor should see (``last_active_admin``)."""
    _emit(
        "scim.request.refused provider=%s user_id=%s scim_id=%s reason=%s",
        provider_slug,
        user_id if user_id is not None else "-",
        scim_id or "-",
        reason,
        event=AuditEvent.SCIM_REQUEST_REFUSED,
        actor_id=None,
        details={"source": "scim", "provider": provider_slug, "scim_id": scim_id, "user_id": user_id, "reason": reason},
    )


def config_changed(*, provider_id: int, provider_slug: str, actor_id: int | None, action: str) -> None:
    """The admin generated, rotated or revoked the SCIM token, or switched SCIM on or off."""
    _emit(
        "scim.config.%s provider_id=%s slug=%s actor=%s",
        action,
        provider_id,
        provider_slug,
        actor_id,
        event=AuditEvent.SCIM_CONFIG_CHANGED,
        actor_id=actor_id,
        details={"provider": provider_slug, "provider_id": provider_id, "action": action},
    )
