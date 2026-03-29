"""Relations API — list, create, and delete issue relations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.relation import RelationCreate, RelationOut
from specivo.services.issue_service import IssueService
from specivo.services.relation_service import RelationService

router = APIRouter(tags=["relations"])
_issue_service = IssueService()
_relation_service = RelationService()


@router.get(
    "/issues/{issue_ref}/relations",
    response_model=list[RelationOut],
)
async def list_relations(
    issue_ref: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RelationOut]:
    """List all relations for an issue.

    Relation types are shown relative to the queried issue:
    if the issue is the target of a ``blocks`` relation, it is shown as
    ``blocked``.
    """
    issue = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)
    rows = await _relation_service.list_for_issue(db, issue)
    return [RelationOut(**row) for row in rows]


@router.post(
    "/issues/{issue_ref}/relations",
    response_model=RelationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_relation(
    issue_ref: str,
    data: RelationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RelationOut:
    """Create a relation from the given issue to ``issue_to_key``."""
    issue_from = await _issue_service.get_by_display_key(db, issue_ref, user=current_user)

    try:
        issue_to = await _issue_service.get_by_display_key(db, data.issue_to_key, user=current_user)
    except NotFoundError:
        raise NotFoundError(f"Issue {data.issue_to_key!r} not found")

    relation = await _relation_service.create(
        session=db,
        issue_from=issue_from,
        issue_to=issue_to,
        relation_type=data.relation_type,
        delay=data.delay,
    )

    # Build the response from the persisted relation
    rows = await _relation_service.list_for_issue(db, issue_from)
    for row in rows:
        if row["id"] == relation.id:
            return RelationOut(**row)

    # Fallback: construct directly from the stored relation
    from_key = issue_from.display_key
    to_key = issue_to.display_key
    return RelationOut(
        id=relation.id,
        issue_from_key=from_key,
        issue_to_key=to_key,
        relation_type=relation.relation_type,
        delay=relation.delay,
    )


@router.delete(
    "/relations/{relation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_relation(
    relation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a relation by its ID."""
    await _relation_service.delete(db, relation_id, current_user)
