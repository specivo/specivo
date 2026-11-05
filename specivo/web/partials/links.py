"""htmx partials for the link graph: "Mentioned in" on an issue, "Linked from" on a wiki page.

Both are lazy-loaded the first time they are opened, so the page itself runs
no link queries. They read the same permission-filtered graph as the REST API
and the MCP tool (``EntityLinkService.get_links``): a text, comment or page the
reader may not see is left out, never shown with its title hidden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import ENTITY_LINKS_MAX_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.services.entity_link_service import TEXT_LINK_KINDS, EntityLinkService, LinkGraph, resolve_root
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(prefix="/partials", tags=["web-partials"], include_in_schema=False)
_service = EntityLinkService()


def group_backlinks(graph: LinkGraph) -> dict[str, list[dict[str, Any]]]:
    """Sort the incoming text links of *graph* into wiki pages, issues and comments, one row per source."""
    nodes = {n.ref: n for n in graph.nodes}
    groups: dict[str, list[dict[str, Any]]] = {"wiki_pages": [], "issues": [], "comments": []}
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.direction != "in" or edge.source not in nodes:
            continue
        node = nodes[edge.source]
        group = "comments" if edge.via else ("wiki_pages" if node.type == "wiki_page" else "issues")
        key = (group, edge.via or node.ref)
        if key in seen:
            continue
        seen.add(key)
        groups[group].append(
            {
                "ref": node.ref.removeprefix("wiki:"),
                "title": node.title,
                "url": edge.via_url or node.url,
                "project": node.project,
                "status": node.status,
                "label": edge.label,
                "via": edge.via,
            }
        )
    return groups


async def _render(request: Request, db: AsyncSession, ref: str, empty_text: str) -> Response:
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)
    try:
        root = await resolve_root(db, user, ref)
    except (NotFoundError, PermissionDeniedError, ValueError):
        return HTMLResponse("<p>Not found</p>", status_code=404)

    graph = await _service.get_links(
        db, user, root, direction="in", kinds=TEXT_LINK_KINDS, limit=ENTITY_LINKS_MAX_LIMIT
    )
    groups = group_backlinks(graph)
    return get_templates().TemplateResponse(
        request,
        "pages/_backlinks.html",
        context={
            "groups": groups,
            "total": sum(len(rows) for rows in groups.values()),
            "truncated": graph.truncated,
            "empty_text": empty_text,
        },
    )


@router.get("/issues/{issue_ref}/mentions/", response_class=HTMLResponse)
async def issue_mentions_partial(issue_ref: str, request: Request, db: AsyncSession = DB_SESSION) -> Response:
    """The "Mentioned in" section of an issue's Relations tab: texts that link to the issue."""
    from specivo.core.i18n import gettext as _

    return await _render(request, db, issue_ref, _("No wiki page, issue or comment mentions this issue yet."))


@router.get("/projects/{project_key}/wiki/{slug}/backlinks/", response_class=HTMLResponse)
async def wiki_backlinks_partial(
    project_key: str, slug: str, request: Request, db: AsyncSession = DB_SESSION
) -> Response:
    """The "Linked from" block of a wiki page: texts that link to the page."""
    from specivo.core.i18n import gettext as _

    return await _render(request, db, f"wiki:{project_key}/{slug}", _("No wiki page, issue or comment links here yet."))
