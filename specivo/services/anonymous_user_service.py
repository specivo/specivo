"""The anonymous user: the reserved ``users`` row for a visitor without an account.

The database creates exactly one row with ``is_anonymous = true`` and keeps it
inert: ``ck_users_anonymous_inert`` pins it deactivated, non-admin, not a
service account and without a password; ``uq_users_single_anonymous`` allows
only one; and the ``reject_anonymous_principal`` trigger keeps it out of
``members`` and ``user_group_members``.

This module is the application side of the same rules:

- ``real_users_clause()``: the predicate every user listing and count applies,
  so the row never appears next to real people.
- ``get_anonymous_user(session)``: resolve the row, caching only its id.
- ``refuse_anonymous_user(user)``: the error raised when an endpoint that
  changes or grants something to a user is pointed at the row.
- ``is_reserved_login(login)``: logins starting with ``$`` belong to the system.

Nothing here grants the row any access.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.exceptions import AppError
from specivo.models.user import User

logger = logging.getLogger(__name__)

# Logins starting with this prefix are reserved for system principals. The
# anonymous user's login is ``$anonymous``; user creation only accepts
# ``[a-z0-9_-]`` logins, so no account created through the application can
# start with it.
RESERVED_LOGIN_PREFIX = "$"

# Process-wide cache of the row's primary key. Only the id is cached: ORM
# instances belong to the session that loaded them and must not be handed to
# another one.
_anonymous_user_id: int | None = None


def real_users_clause() -> ColumnElement[bool]:
    """Return the predicate selecting user rows that stand for real accounts.

    Apply it to every query that lists, searches or counts users, so the
    anonymous user never shows up as though it were a colleague.
    """
    return User.is_anonymous.is_(False)


def is_reserved_login(login: str) -> bool:
    """Return True if *login* is reserved for a system principal."""
    return login.startswith(RESERVED_LOGIN_PREFIX)


async def get_anonymous_user(session: AsyncSession) -> User | None:
    """Return the anonymous user row, loaded through *session*.

    The row's id is cached for the life of the process; the row itself is
    loaded through *session* on every call. Returns None, with a warning, when
    the row is missing, which only happens if the database was not migrated or
    the row was deleted by hand.
    """
    global _anonymous_user_id

    if _anonymous_user_id is not None:
        user = await session.get(User, _anonymous_user_id)
        if user is not None and user.is_anonymous:
            return user
        _anonymous_user_id = None

    user = (await session.execute(select(User).where(User.is_anonymous.is_(True)))).scalar_one_or_none()
    if user is None:
        logger.warning("The anonymous user row is missing; run the database migrations to restore it")
        return None

    _anonymous_user_id = user.id
    return user


def clear_anonymous_user_cache() -> None:
    """Forget the cached anonymous user id."""
    global _anonymous_user_id
    _anonymous_user_id = None


class AnonymousUserProtectedError(AppError):
    """Raised when an operation targets the anonymous user (403)."""

    def __init__(self, message: str = "The anonymous user is reserved by the system and cannot be changed.") -> None:
        super().__init__(code="anonymous_user_protected", message=message, status_code=403)


def refuse_anonymous_user(user: User, message: str | None = None) -> None:
    """Raise ``AnonymousUserProtectedError`` if *user* is the anonymous user."""
    if user.is_anonymous:
        if message is None:
            raise AnonymousUserProtectedError()
        raise AnonymousUserProtectedError(message)
