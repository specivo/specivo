"""Link graph API: what an issue or wiki page links to, and what links to it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import ENTITY_LINKS_DEFAULT_LIMIT, ENTITY_LINKS_MAX_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import ValidationError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.links import LinkGraphOut
from specivo.services.entity_link_service import ALL_KINDS, EntityLinkService, NodeKey, resolve_root

router = APIRouter(tags=["links"])
_service = EntityLinkService()

_DIRECTION = Query("both", pattern="^(in|out|both)$", description="in (backlinks), out, or both")
_DEPTH = Query(1, ge=1, le=2, description="1, or 2 to expand issue and wiki-page neighbours once more")
_KINDS = Query(
    None,
    description="Comma-separated edge kinds to keep: " + ", ".join(sorted(ALL_KINDS)),
)
_LIMIT = Query(ENTITY_LINKS_DEFAULT_LIMIT, ge=1, le=ENTITY_LINKS_MAX_LIMIT, description="Neighbour nodes at most")


def _parse_kinds(kinds: str | None) -> list[str] | None:
    if not kinds:
        return None
    parsed = [k.strip() for k in kinds.split(",") if k.strip()]
    unknown = sorted(set(parsed) - ALL_KINDS)
    if unknown:
        raise ValidationError(f"Unknown link kind(s): {', '.join(unknown)}", field="kinds")
    return parsed


async def _graph(
    db: AsyncSession, user: User, root: NodeKey, direction: str, depth: int, kinds: str | None, limit: int
) -> LinkGraphOut:
    graph = await _service.get_links(
        db, user, root, direction=direction, depth=depth, kinds=_parse_kinds(kinds), limit=limit
    )
    return LinkGraphOut.model_validate(graph.as_dict())


@router.get("/issues/{issue_ref}/links/", response_model=LinkGraphOut)
async def get_issue_links(
    issue_ref: str,
    direction: str = _DIRECTION,
    depth: int = _DEPTH,
    kinds: str | None = _KINDS,
    limit: int = _LIMIT,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> LinkGraphOut:
    """Links of an issue: what it links to (text links, relations, parent, children, attachments,
    commits, pull requests) and what links to it (backlinks from issues, comments and wiki pages).

    Only what the caller may see is returned; a link to or from a hidden item is left out entirely.
    """
    root = await resolve_root(db, current_user, issue_ref)
    return await _graph(db, current_user, root, direction, depth, kinds, limit)


@router.get("/projects/{project_key}/wiki/{slug}/links/", response_model=LinkGraphOut)
async def get_wiki_page_links(
    project_key: str,
    slug: str,
    direction: str = _DIRECTION,
    depth: int = _DEPTH,
    kinds: str | None = _KINDS,
    limit: int = _LIMIT,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> LinkGraphOut:
    """Links of a wiki page: what it links to and what links to it (backlinks), filtered for the caller."""
    root = await resolve_root(db, current_user, f"wiki:{project_key}/{slug}")
    return await _graph(db, current_user, root, direction, depth, kinds, limit)
