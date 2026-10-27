"""Account events of single sign-on: one function per event.

Each function writes one ``specivo.sso`` log line with the subject hashed
(:func:`subject_hash`), never the raw ``sub``. They are the single place an
event is recorded, so the security audit events (``SSO_USER_PROVISIONED``,
``SSO_ACCOUNT_LINKED``, ``SSO_ACCOUNT_UNLINKED``) can be added here without
touching the callers.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from specivo.services.sso.log import logger

__all__ = [
    "LinkMethod",
    "UnlinkedBy",
    "account_linked",
    "account_unlinked",
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
