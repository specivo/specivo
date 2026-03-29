"""Users API — user autocomplete for @mention UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.reaction import UserAutocompleteOut

router = APIRouter(tags=["users"])


@router.get(
    "/users/autocomplete",
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
