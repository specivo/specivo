"""Reactions API — add, remove, and list emoji reactions on journals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.reaction import AddReactionRequest, ReactionGroupOut, ReactionOut, ReactionUserOut
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import check_permission
from specivo.services.reaction_service import ReactionService

router = APIRouter(tags=["reactions"])
_service = ReactionService()
_issue_service = IssueService()


@router.post(
    "/issues/{issue_ref}/journals/{journal_id}/reactions/",
    response_model=ReactionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_reaction(
    issue_ref: str,
    journal_id: int,
    data: AddReactionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReactionOut:
    """Add an emoji reaction to a journal entry."""
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "view_issues", db):
        raise PermissionDeniedError("You do not have permission to react in this project")

    reaction = await _service.add_reaction(
        session=db,
        journal_id=journal_id,
        user=current_user,
        emoji=data.emoji,
    )

    return ReactionOut(
        id=reaction.id,
        journal_id=reaction.journal_id,
        user_id=reaction.user_id,
        emoji=reaction.emoji,
        created_at=reaction.created_at,
    )


@router.delete(
    "/issues/{issue_ref}/journals/{journal_id}/reactions/{emoji}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_reaction(
    issue_ref: str,
    journal_id: int,
    emoji: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove an emoji reaction from a journal entry."""
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "view_issues", db):
        raise PermissionDeniedError("You do not have permission to manage reactions in this project")

    await _service.remove_reaction(
        session=db,
        journal_id=journal_id,
        user=current_user,
        emoji=emoji,
    )


@router.get(
    "/issues/{issue_ref}/journals/{journal_id}/reactions/",
    response_model=list[ReactionGroupOut],
)
async def list_reactions(
    issue_ref: str,
    journal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ReactionGroupOut]:
    """List reactions on a journal entry, grouped by emoji."""
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)
    if not await check_permission(current_user, issue.project_id, "view_issues", db):
        raise PermissionDeniedError("You do not have permission to view reactions in this project")

    groups = await _service.list_reactions(session=db, journal_id=journal_id)
    return [
        ReactionGroupOut(
            emoji=g.emoji,
            count=g.count,
            users=[ReactionUserOut(id=u["id"], login=u["login"], display_name=u["display_name"]) for u in g.users],
        )
        for g in groups
    ]
