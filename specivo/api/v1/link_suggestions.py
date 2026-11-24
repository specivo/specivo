"""Suggested links: accept one (it becomes a relation or a typed link) or dismiss it for good.

The suggestions themselves are read from the link graph endpoints with
``include_suggestions=true`` (edges of kind ``similar``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from specivo.core.database import DbSession
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.links import LinkSuggestionDecisionIn, LinkSuggestionDecisionOut
from specivo.services.entity_link_service import resolve_root
from specivo.services.link_suggestion_service import OP_ACCEPT, OP_DISMISS, decide

router = APIRouter(prefix="/link-suggestions", tags=["links"])


async def _decide(db: DbSession, user: User, op: str, data: LinkSuggestionDecisionIn) -> LinkSuggestionDecisionOut:
    source = await resolve_root(db, user, data.ref)
    target = await resolve_root(db, user, data.other_ref)
    decision = await decide(db, user, op, source, target, link_type=data.link_type if op == OP_ACCEPT else None)
    return LinkSuggestionDecisionOut(
        op=op,
        status=decision.status,
        ref=decision.source_ref,
        other_ref=decision.target_ref,
        link_kind=decision.link_kind,
        link_type=decision.link_type,
        relation_id=decision.relation_id,
        message=decision.describe(),
    )


@router.post("/accept/", response_model=LinkSuggestionDecisionOut)
async def accept_link_suggestion(
    data: LinkSuggestionDecisionIn,
    db: DbSession,
    current_user: User = Depends(get_current_user),
) -> LinkSuggestionDecisionOut:
    """Accept the suggested link between ``ref`` and ``other_ref``.

    Two issues get a relation (``manage_issue_relations`` in ``ref``'s
    project); an issue and a page, or two pages, get a typed link from ``ref``
    (``edit_issues`` or ``manage_wiki`` in its project). Both items must be
    visible to the caller, and an open suggestion must exist between them.
    """
    return await _decide(db, current_user, OP_ACCEPT, data)


@router.post("/dismiss/", response_model=LinkSuggestionDecisionOut)
async def dismiss_link_suggestion(
    data: LinkSuggestionDecisionIn,
    db: DbSession,
    current_user: User = Depends(get_current_user),
) -> LinkSuggestionDecisionOut:
    """Dismiss the suggested link between ``ref`` and ``other_ref``: it is never suggested again for this model.

    Needs the permission accepting it would need.
    """
    return await _decide(db, current_user, OP_DISMISS, data)
