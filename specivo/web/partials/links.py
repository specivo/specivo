"""htmx partials for the link graph: "Mentioned in" on an issue, "Linked from" on a wiki page,
and "Possibly related" (suggested links, with Accept and Dismiss) on both.

Both are lazy-loaded the first time they are opened, so the page itself runs
no link queries. They read the same permission-filtered graph as the REST API
and the MCP tool (``EntityLinkService.get_links``): a text, comment or page the
reader may not see is left out, never shown with its title hidden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import ENTITY_LINKS_MAX_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.services.entity_link_service import STORED_LINK_KINDS, EntityLinkService, LinkGraph, resolve_root
from specivo.web.deps import get_current_user_optional, get_templates, login_redirect

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
        return login_redirect(request)
    user = cast("User", user_obj)
    try:
        root = await resolve_root(db, user, ref)
    except (NotFoundError, PermissionDeniedError, ValueError):
        return HTMLResponse("<p>Not found</p>", status_code=404)

    graph = await _service.get_links(
        db, user, root, direction="in", kinds=STORED_LINK_KINDS, limit=ENTITY_LINKS_MAX_LIMIT
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


# ---------------------------------------------------------------------------
# Possibly related: suggested links, with Accept and Dismiss
# ---------------------------------------------------------------------------


async def _decide_permissions(db: AsyncSession, user: User, root: tuple[str, Any]) -> dict[str, bool]:
    """Whether *user* may accept or dismiss, from *root*, a suggestion to an issue and to a page."""
    from specivo.services.link_suggestion_service import _item_info
    from specivo.services.permission_service import Permission, check_permission

    project_id, _ref = await _item_info(db, (root[0], int(root[1])))
    if root[0] == "issue":
        return {
            "issue": await check_permission(user, project_id, Permission.MANAGE_ISSUE_RELATIONS, db),
            "wiki_page": await check_permission(user, project_id, Permission.EDIT_ISSUES, db),
        }
    allowed = await check_permission(user, project_id, Permission.MANAGE_WIKI, db)
    return {"issue": allowed, "wiki_page": allowed}


async def _render_suggestions(
    request: Request, db: AsyncSession, user: User, ref: str, *, error: str | None = None, done: str | None = None
) -> Response:
    """The "Possibly related" list: issues and wiki pages apart, at most ``SIMILAR_LINKS_WEB_LIMIT_PER_TYPE``
    of each, best score first, each with a plain-text preview of its content."""
    from specivo.core.constants import SIMILAR_LINKS_PREVIEW_CHARS, SIMILAR_LINKS_WEB_LIMIT_PER_TYPE
    from specivo.services.entity_link_service import KIND_SIMILAR
    from specivo.services.link_suggestion_service import item_previews

    try:
        root = await resolve_root(db, user, ref)
    except (NotFoundError, PermissionDeniedError, ValueError):
        return HTMLResponse("<p>Not found</p>", status_code=404)
    graph = await _service.get_links(
        db, user, root, direction="both", kinds=[KIND_SIMILAR], limit=ENTITY_LINKS_MAX_LIMIT
    )
    nodes = {n.ref: n for n in graph.nodes}
    allowed = await _decide_permissions(db, user, root)
    found: dict[str, list[tuple[float, Any]]] = {"issue": [], "wiki_page": []}
    for edge in graph.edges:
        node = nodes.get(edge.target)
        if not edge.suggested or node is None or node.type not in found:
            continue
        found[node.type].append((float(edge.label or 0), node))
    groups: dict[str, list[dict[str, Any]]] = {}
    hidden: dict[str, int] = {}
    for node_type, scored in found.items():
        scored.sort(key=lambda pair: -pair[0])
        groups[node_type] = [
            {
                "ref": node.ref,
                "key": graph.keys.get(node.ref),
                "shown_ref": node.ref.removeprefix("wiki:"),
                "title": node.title,
                "url": node.url,
                "status": node.status,
                "score": f"{score:.3f}",
                # Two issues are linked by a relation; anything else by a typed link.
                "both_issues": root[0] == "issue" and node.type == "issue",
                "can_decide": allowed.get(node.type, False),
            }
            for score, node in scored[:SIMILAR_LINKS_WEB_LIMIT_PER_TYPE]
        ]
        hidden[node_type] = max(0, len(scored) - SIMILAR_LINKS_WEB_LIMIT_PER_TYPE)
    shown = [row for rows in groups.values() for row in rows]
    keys = [(str(row["key"][0]), int(row["key"][1])) for row in shown if row["key"]]
    previews = await item_previews(db, keys, SIMILAR_LINKS_PREVIEW_CHARS)
    for row in shown:
        key = row.pop("key")
        row["preview"] = previews.get((str(key[0]), int(key[1])), "") if key else ""
    return get_templates().TemplateResponse(
        request,
        "pages/_link_suggestions.html",
        context={
            "root_ref": graph.root.ref if graph.root else ref,
            "groups": groups,
            "hidden": hidden,
            "total": len(shown),
            "error": error,
            "done": done,
        },
    )


@router.get("/issues/{issue_ref}/suggestions/", response_class=HTMLResponse)
async def issue_suggestions_partial(issue_ref: str, request: Request, db: AsyncSession = DB_SESSION) -> Response:
    """The "Possibly related" section of an issue's Relations tab: suggested links, with Accept and Dismiss."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return login_redirect(request)
    return await _render_suggestions(request, db, cast("User", user_obj), issue_ref)


@router.get("/projects/{project_key}/wiki/{slug}/suggestions/", response_class=HTMLResponse)
async def wiki_suggestions_partial(
    project_key: str, slug: str, request: Request, db: AsyncSession = DB_SESSION
) -> Response:
    """The "Possibly related" block of a wiki page: suggested links, with Accept and Dismiss."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return login_redirect(request)
    return await _render_suggestions(request, db, cast("User", user_obj), f"wiki:{project_key}/{slug}")


@router.post("/link-suggestions/", response_class=HTMLResponse)
async def decide_suggestion_partial(request: Request, db: AsyncSession = DB_SESSION) -> Response:
    """Accept or dismiss one suggestion from the "Possibly related" list, then show the list again.

    The form names the item the list belongs to (``ref``), the other end
    (``other_ref``), ``op`` (the button pressed) and, to accept, ``link_type``.
    A refusal is shown above the list rather than as an error page.
    """
    from specivo.core.exceptions import AppError
    from specivo.core.i18n import gettext as _
    from specivo.services.link_suggestion_service import OP_ACCEPT, OPS, decide

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return login_redirect(request)
    user = cast("User", user_obj)
    form = await request.form()
    ref = str(form.get("ref") or "")[:300]
    other_ref = str(form.get("other_ref") or "")[:300]
    op = str(form.get("op") or "")
    link_type = str(form.get("link_type") or "") or None
    if op not in OPS or not ref or not other_ref:
        return HTMLResponse("<p>Bad request</p>", status_code=400)
    try:
        source = await resolve_root(db, user, ref)
    except (NotFoundError, PermissionDeniedError, ValueError):
        return HTMLResponse("<p>Not found</p>", status_code=404)
    # A savepoint, so a refusal half-way leaves nothing behind when the page answers normally.
    savepoint = await db.begin_nested()
    try:
        target = await resolve_root(db, user, other_ref)
        decision = await decide(db, user, op, source, target, link_type=link_type if op == OP_ACCEPT else None)
        await savepoint.commit()
    except (AppError, ValueError) as exc:
        if savepoint.is_active:
            await savepoint.rollback()
        return await _refused(request, db, user, ref, exc)
    done = _("Linked.") if decision.op == OP_ACCEPT else _("Dismissed. It will not be suggested again.")
    return await _render_suggestions(request, db, user, ref, done=done)


async def _refused(request: Request, db: AsyncSession, user: User, ref: str, exc: Exception) -> Response:
    from specivo.core.exceptions import AppError
    from specivo.core.i18n import gettext as _

    if isinstance(exc, NotFoundError | ValueError):
        message = _("That suggestion is no longer available.")
    elif isinstance(exc, PermissionDeniedError):
        message = _("You do not have permission to link these items.")
    else:
        message = exc.message if isinstance(exc, AppError) else str(exc)
    return await _render_suggestions(request, db, user, ref, error=message)
