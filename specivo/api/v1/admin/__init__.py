"""Admin API sub-package — shared dependencies."""

from __future__ import annotations

from fastapi import Depends, Request

from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.services.api_key_scope import is_global_admin


def require_admin_api(current_user: User = Depends(get_current_user)) -> User:  # noqa: B008
    """Dependency: raise 403 if the current user is not an admin.

    An admin authenticated with a project-limited API key is refused: the
    admin API reaches the whole instance (``specivo.services.api_key_scope``).
    """
    if not is_global_admin(current_user):
        raise PermissionDeniedError("Admin access required")
    return current_user


def refuse_api_key(request: Request) -> None:
    """Raise 403 ``api_key_not_allowed`` when this request authenticated with an API key.

    ``get_current_user`` sets ``request.state.api_key_id`` for API-key auth only.
    """
    if getattr(request.state, "api_key_id", None) is not None:
        raise AppError(
            code="api_key_not_allowed",
            message="This change needs a signed-in session; API keys cannot make it",
            status_code=403,
        )


def require_admin_session_api(
    request: Request,
    current_user: User = Depends(require_admin_api),  # noqa: B008
) -> User:
    """Dependency: an admin signed in with a session (JWT), never with an API key.

    For every admin action that hands out a way to sign in or to act as
    someone: the SSO configuration (providers, group mappings, sign-in
    settings), setting a password (reset, or a new user created with one),
    unlocking or reactivating an account, and minting or re-enabling API keys
    or credentials for other users. A leaked admin key must not be able to
    turn itself into a session (reset another admin's password, then sign
    in) or register a rogue identity provider that maps its users to admins.
    Taking access away (lock, deactivate, revoke) stays key-accessible, so
    incident automation keeps working.
    """
    refuse_api_key(request)
    return current_user
