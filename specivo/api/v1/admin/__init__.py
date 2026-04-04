"""Admin API sub-package — shared dependencies."""

from __future__ import annotations

from fastapi import Depends

from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User


def require_admin_api(current_user: User = Depends(get_current_user)) -> User:  # noqa: B008
    """Dependency: raise 403 if the current user is not an admin."""
    if not current_user.is_admin:
        raise PermissionDeniedError("Admin access required")
    return current_user
