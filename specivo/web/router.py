"""Web router — serves HTML pages (excluded from OpenAPI schema)."""

from fastapi import APIRouter

from specivo.web.pages.admin import router as admin_pages
from specivo.web.pages.auth import router as auth_pages
from specivo.web.pages.dashboard import router as dashboard_pages
from specivo.web.pages.issues import router as issue_pages
from specivo.web.pages.issues import short_router as issue_short_pages
from specivo.web.pages.projects import router as project_pages
from specivo.web.pages.recurring import router as recurring_pages
from specivo.web.pages.search import router as search_pages
from specivo.web.pages.sprints import router as sprint_pages
from specivo.web.pages.time import router as time_pages
from specivo.web.pages.wiki import router as wiki_pages
from specivo.web.partials.dashboard import router as dashboard_partials
from specivo.web.partials.issues import router as issue_partials

web_router = APIRouter(include_in_schema=False)

web_router.include_router(dashboard_pages)
web_router.include_router(auth_pages)
web_router.include_router(project_pages)
web_router.include_router(wiki_pages)
web_router.include_router(issue_pages)
web_router.include_router(issue_short_pages)
web_router.include_router(issue_partials)
web_router.include_router(dashboard_partials)
web_router.include_router(sprint_pages)
web_router.include_router(recurring_pages)
web_router.include_router(time_pages)
web_router.include_router(search_pages)
web_router.include_router(admin_pages)
