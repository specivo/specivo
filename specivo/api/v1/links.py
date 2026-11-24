"""Link graph API: what an issue, wiki page or hub links to, what links to it, and a project's graph."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import ENTITY_LINKS_DEFAULT_LIMIT, ENTITY_LINKS_MAX_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.links import LinkGraphOut
from specivo.services.entity_link_nodes import HubReader
from specivo.services.entity_link_service import (
    ALL_KINDS,
    ITEM_KINDS,
    KIND_SIMILAR,
    EntityLinkService,
    NodeKey,
    resolve_root,
)
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["links"])
_service = EntityLinkService()

_DIRECTION = Query(
    "both",
    pattern="^(in|out|both)$",
    description="in (backlinks, relations towards it, parent and children), out, or both",
)
_DEPTH = Query(1, ge=1, le=2, description="1, or 2 to expand issue and wiki-page neighbours once more")
_KINDS = Query(
    None,
    description=(
        "Comma-separated edge kinds to keep: "
        + ", ".join(sorted(ALL_KINDS))
        + ". When omitted, every kind except the structured-node ones (tagged, in_component, in_metadata, "
        "targets_version, in_sprint, assigned_to, mentions), which add tag, component, metadata, version, sprint, "
        "user and concept nodes with their degree, and except similar (suggested links), which comes only when "
        "named or with include_suggestions=true"
    ),
)
_INCLUDE_SUGGESTIONS = Query(
    False,
    description=(
        "Add suggested links (kind similar, suggested: true, the cosine score in label) to the kinds read: "
        "issues and wiki pages with similar content that are not linked yet. With kind mentions read, also "
        "keep the concept mentions below the project's confidence threshold (suggested: true)"
    ),
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
    db: AsyncSession,
    user: User,
    root: NodeKey,
    direction: str,
    depth: int,
    kinds: str | None,
    limit: int,
    include_suggestions: bool = False,
) -> LinkGraphOut:
    parsed = _parse_kinds(kinds)
    if include_suggestions:
        parsed = sorted(set(parsed or ITEM_KINDS) | {KIND_SIMILAR})
    graph = await _service.get_links(
        db,
        user,
        root,
        direction=direction,
        depth=depth,
        kinds=parsed,
        limit=limit,
        default_kinds=ITEM_KINDS,
        suggestions=include_suggestions,
    )
    return LinkGraphOut.model_validate(graph.as_dict())


@router.get("/issues/{issue_ref}/links/", response_model=LinkGraphOut)
async def get_issue_links(
    issue_ref: str,
    direction: str = _DIRECTION,
    depth: int = _DEPTH,
    kinds: str | None = _KINDS,
    limit: int = _LIMIT,
    include_suggestions: bool = _INCLUDE_SUGGESTIONS,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> LinkGraphOut:
    """Links of an issue: what it links to (text links, relations, parent, children, attachments,
    commits, pull requests) and what links to it (backlinks from issues, comments and wiki pages).

    Only what the caller may see is returned; a link to or from a hidden item is left out entirely.
    """
    root = await resolve_root(db, current_user, issue_ref)
    return await _graph(db, current_user, root, direction, depth, kinds, limit, include_suggestions)


@router.get("/projects/{project_key}/wiki/{slug}/links/", response_model=LinkGraphOut)
async def get_wiki_page_links(
    project_key: str,
    slug: str,
    direction: str = _DIRECTION,
    depth: int = _DEPTH,
    kinds: str | None = _KINDS,
    limit: int = _LIMIT,
    include_suggestions: bool = _INCLUDE_SUGGESTIONS,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> LinkGraphOut:
    """Links of a wiki page: what it links to and what links to it (backlinks), filtered for the caller."""
    root = await resolve_root(db, current_user, f"wiki:{project_key}/{slug}")
    return await _graph(db, current_user, root, direction, depth, kinds, limit, include_suggestions)


@router.get("/links/", response_model=LinkGraphOut)
async def get_links(
    ref: str = Query(
        ...,
        min_length=1,
        max_length=600,
        description=(
            "The root: ACME-12, wiki:ACME/slug, tag:ACME/<name>, component:ACME/<value>, "
            "metadata:ACME/<key>/<value>, version:ACME/<name>, sprint:ACME/<name> or user:<login>"
        ),
    ),
    direction: str = _DIRECTION,
    depth: int = _DEPTH,
    kinds: str | None = _KINDS,
    limit: int = _LIMIT,
    include_suggestions: bool = _INCLUDE_SUGGESTIONS,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> LinkGraphOut:
    """Links of any node the link graph can start from: an issue, a wiki page, or a tag, component, metadata
    value, version, sprint or user (whose members are listed). The graph view uses it to expand a hub node.

    A root the caller may not see is not found, as on the issue and wiki page routes.
    """
    try:
        root = await resolve_root(db, current_user, ref, hubs=True)
    except ValueError as exc:
        raise ValidationError(str(exc), field="ref") from None
    return await _graph(db, current_user, root, direction, depth, kinds, limit, include_suggestions)


@router.get("/projects/{project_key}/links/", response_model=LinkGraphOut)
async def get_project_links(
    project_key: str,
    kinds: str | None = _KINDS,
    limit: int = _LIMIT,
    include_suggestions: bool = _INCLUDE_SUGGESTIONS,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> LinkGraphOut:
    """The project's link graph: its most linked issues and wiki pages with their links, filtered for the caller.

    ``root`` is null and edges have no direction. A project the caller cannot reach, or whose issues and wiki
    they may not read, is not found.
    """
    try:
        project = await ProjectService().get_readable_by_key(db, project_key.upper(), current_user)
    except NotFoundError:
        raise NotFoundError("Project not found") from None
    hubs = HubReader(db, current_user)
    if not (await hubs.issues_readable(project.id) or await hubs.wiki_readable(project.id)):
        raise NotFoundError("Project not found")
    parsed = _parse_kinds(kinds)
    if include_suggestions:
        parsed = sorted(set(parsed or ITEM_KINDS) | {KIND_SIMILAR})
    graph = await _service.get_project_graph(
        db, current_user, project.id, kinds=parsed, limit=limit, default_kinds=ITEM_KINDS
    )
    return LinkGraphOut.model_validate(graph.as_dict())
