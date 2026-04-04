"""Users API — user autocomplete for @mention UI, user preferences."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import ACTIVITY_PER_PAGE_OPTIONS
from specivo.core.database import get_db
from specivo.core.exceptions import ValidationError
from specivo.core.rate_limit import rate_limit
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.reaction import UserAutocompleteOut

router = APIRouter(tags=["users"])


@router.get(
    "/users/autocomplete/",
    response_model=list[UserAutocompleteOut],
)
async def user_autocomplete(
    q: str = Query(min_length=1, max_length=100, description="Search prefix for login or display_name"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserAutocompleteOut]:
    """Search users by login or display name prefix for @mention autocomplete."""
    pattern = f"%{q}%"
    result = await db.execute(
        select(User)
        .where(
            User.status == "active",
            or_(
                User.login.ilike(pattern),
                User.display_name.ilike(pattern),
            ),
        )
        .order_by(User.login)
        .limit(20)
    )
    users = list(result.scalars().all())
    return [
        UserAutocompleteOut(
            id=u.id,
            login=u.login,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
        )
        for u in users
    ]


@router.patch(
    "/users/me/preferences/activity-per-page/",
    tags=["preferences"],
)
async def update_activity_per_page(
    per_page: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limit("preference_update", max_requests=30, window_seconds=60)),
) -> dict:
    """Save the user's activity items-per-page preference."""
    if per_page not in ACTIVITY_PER_PAGE_OPTIONS:
        raise ValidationError(f"Invalid per_page. Allowed: {ACTIVITY_PER_PAGE_OPTIONS}")
    current_user.preferences = {**current_user.preferences, "activity_per_page": per_page}
    await db.flush()
    return {"activity_per_page": per_page}
