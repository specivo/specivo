"""Web router — serves HTML pages (excluded from OpenAPI schema)."""

from fastapi import APIRouter

from specivo.web.pages.admin import router as admin_pages
from specivo.web.pages.admin_ai_model_prices import router as admin_ai_model_price_pages
from specivo.web.pages.admin_exchange_rates import router as admin_exchange_rate_pages
from specivo.web.pages.admin_sso import router as admin_sso_pages
from specivo.web.pages.ai_spend_reports import router as ai_spend_report_pages
from specivo.web.pages.ai_subscriptions import router as ai_subscription_pages
from specivo.web.pages.ai_usage import router as ai_usage_pages
from specivo.web.pages.auth import router as auth_pages
from specivo.web.pages.connected_accounts import router as connected_account_pages
from specivo.web.pages.dashboard import router as dashboard_pages
from specivo.web.pages.email_change import router as email_change_pages
from specivo.web.pages.issues import router as issue_pages
from specivo.web.pages.issues import short_router as issue_short_pages
from specivo.web.pages.projects import router as project_pages
from specivo.web.pages.recurring import router as recurring_pages
from specivo.web.pages.search import router as search_pages
from specivo.web.pages.sprints import router as sprint_pages
from specivo.web.pages.sso import router as sso_pages
from specivo.web.pages.time import router as time_pages
from specivo.web.pages.wiki import router as wiki_pages
from specivo.web.partials.dashboard import router as dashboard_partials
from specivo.web.partials.issues import router as issue_partials
from specivo.web.partials.links import router as link_partials

web_router = APIRouter(include_in_schema=False)

web_router.include_router(dashboard_pages)
web_router.include_router(auth_pages)
web_router.include_router(sso_pages)
web_router.include_router(connected_account_pages)
web_router.include_router(email_change_pages)
# Before the project and admin pages, whose paths could otherwise match first.
web_router.include_router(ai_spend_report_pages)
web_router.include_router(project_pages)
web_router.include_router(wiki_pages)
web_router.include_router(issue_pages)
web_router.include_router(issue_short_pages)
web_router.include_router(issue_partials)
web_router.include_router(link_partials)
web_router.include_router(dashboard_partials)
web_router.include_router(sprint_pages)
web_router.include_router(recurring_pages)
web_router.include_router(time_pages)
web_router.include_router(search_pages)
web_router.include_router(admin_pages)
web_router.include_router(admin_sso_pages)
web_router.include_router(admin_exchange_rate_pages)
web_router.include_router(admin_ai_model_price_pages)
web_router.include_router(ai_subscription_pages)
web_router.include_router(ai_usage_pages)
