"""``robots.txt`` and the sitemap, served at the site root.

Both are always at the root, never behind the stealth prefix (an instance with
one is never indexed, see ``specivo.services.search_engine_service``).

``robots.txt``, first match wins:

1. the admin setting ``robots_txt``;
2. the ``ROBOTS_TXT`` environment variable, when it is set;
3. while search engine indexing applies and a project is readable without an
   account: a generated file allowing just those projects' pages and naming
   the sitemap;
4. ``Disallow: /``.

The sitemap (``/sitemap.xml``, an index, and the sitemaps it points at under
``/sitemaps/``) answers 404 whenever indexing does not apply. Its requests run
as the anonymous principal inside ``anonymous_read_scope``, like every other
anonymous read: rate limited, read-only, uncacheable.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from specivo.core.config import get_settings
from specivo.core.database import DbSession
from specivo.models.user import User
from specivo.services import search_engine_service as seo
from specivo.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

_SITEMAP_MEDIA_TYPE = "application/xml"


async def _robots_txt_body(db: DbSession) -> str:
    settings = get_settings()
    try:
        custom = (await SettingsService().get_all(db)).get("robots_txt")
        if custom:
            return custom
        if "robots_txt" in settings.model_fields_set:
            return settings.robots_txt
        anonymous = await seo.indexing_reader(db)
        if anonymous is not None:
            projects = await seo.readable_projects(db, anonymous)
            if projects:
                return seo.robots_txt(projects)
    except Exception:
        # Fail closed: when the settings cannot be read, nothing is offered to
        # crawlers beyond the default.
        logger.warning("Could not build robots.txt; serving the default", exc_info=True)
    return settings.robots_txt


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt(db: DbSession) -> PlainTextResponse:
    """The robots.txt in force for this request (see the module docstring)."""
    response = PlainTextResponse(await _robots_txt_body(db))
    # It follows settings and opt-ins that change at any time.
    response.headers["Cache-Control"] = "no-cache"
    return response


async def _anonymous_or_404(db: DbSession) -> User:
    try:
        anonymous = await seo.indexing_reader(db)
    except Exception:
        logger.warning("Could not resolve search engine indexing; refusing the sitemap", exc_info=True)
        anonymous = None
    if anonymous is None:
        raise HTTPException(status_code=404, detail="Not found")
    return anonymous


async def _serve(request: Request, response: Response, db: DbSession, build) -> Response:
    """Run *build* as the anonymous principal and answer its XML, or 404 when it has nothing."""
    from specivo.core.security import anonymous_read_scope

    anonymous = await _anonymous_or_404(db)
    async with anonymous_read_scope(request, response, db):
        body = await build(anonymous)
    if body is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Keep what the anonymous scope put on the injected response (rate limit
    # headers), but not its empty body's length.
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")}
    return Response(body, media_type=_SITEMAP_MEDIA_TYPE, headers=headers)


@router.get(seo.SITEMAP_PATH)
async def sitemap_index(request: Request, response: Response, db: DbSession) -> Response:
    """The sitemap index: the project sitemap and every issue and wiki chunk."""
    return await _serve(request, response, db, lambda anonymous: seo.sitemap_index(db, anonymous))


@router.get(f"{seo.SITEMAPS_PREFIX}projects.xml")
async def projects_sitemap(request: Request, response: Response, db: DbSession) -> Response:
    """The project list and each readable project's page and issue list."""
    return await _serve(request, response, db, lambda anonymous: seo.projects_sitemap(db, anonymous))


@router.get(seo.SITEMAPS_PREFIX + "{project_key}/issues-{chunk}.xml")
async def issues_sitemap(request: Request, response: Response, project_key: str, chunk: int, db: DbSession) -> Response:
    """One chunk of a project's issues anonymous visitors can read."""
    return await _serve(request, response, db, lambda anonymous: seo.issues_sitemap(db, anonymous, project_key, chunk))


@router.get(seo.SITEMAPS_PREFIX + "{project_key}/wiki-{chunk}.xml")
async def wiki_sitemap(request: Request, response: Response, project_key: str, chunk: int, db: DbSession) -> Response:
    """One chunk of a project's wiki pages, when anonymous visitors can read its wiki."""
    return await _serve(request, response, db, lambda anonymous: seo.wiki_sitemap(db, anonymous, project_key, chunk))
