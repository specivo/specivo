"""API v1 router — aggregates all sub-routers.

Core routes include: auth, issues, journals, reactions, saved filters, bulk
operations, notifications (inbox + preferences), wiki, search, and admin.

Pro-only routes (webhooks) are mounted by the ProPlugin via ``get_routers()``
when specivo-pro is installed.

Enterprise-only routes (admin groups, credentials, kill switch, audit logs,
model costs, agent costs, admin metadata_schemas) are NOT included here.
They are mounted by the EnterprisePlugin via ``get_routers()``.
"""

from fastapi import APIRouter

from specivo.api.v1.admin.email import router as admin_email_router
from specivo.api.v1.admin.embedding_models import router as admin_embedding_models_router
from specivo.api.v1.admin.metadata_presets import router as admin_metadata_presets_router
from specivo.api.v1.admin.metadata_schemas import router as admin_metadata_schemas_router
from specivo.api.v1.admin.projects import router as admin_projects_router
from specivo.api.v1.admin.settings import router as admin_settings_router
from specivo.api.v1.admin.users import router as admin_users_router
from specivo.api.v1.admin.workflows import router as admin_workflows_router
from specivo.api.v1.agent_sessions import router as agent_sessions_router
from specivo.api.v1.api_keys import router as api_keys_router
from specivo.api.v1.attachments import router as attachments_router
from specivo.api.v1.auth import router as auth_router
from specivo.api.v1.issues import router as issues_router
from specivo.api.v1.markdown import router as markdown_router
from specivo.api.v1.metadata import router as metadata_router
from specivo.api.v1.notifications import router as notifications_router
from specivo.api.v1.projects import router as projects_router
from specivo.api.v1.reactions import router as reactions_router
from specivo.api.v1.recurring_patterns import router as recurring_patterns_router
from specivo.api.v1.relations import router as relations_router
from specivo.api.v1.saved_filters import router as saved_filters_router
from specivo.api.v1.search import router as search_router
from specivo.api.v1.search_admin import router as search_admin_router
from specivo.api.v1.sprints import router as sprints_router
from specivo.api.v1.tags import router as tags_router
from specivo.api.v1.time_entries import router as time_entries_router
from specivo.api.v1.users import router as users_router
from specivo.api.v1.versions import router as versions_router
from specivo.api.v1.wiki import router as wiki_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(api_keys_router, tags=["api-keys"])
api_router.include_router(projects_router)
api_router.include_router(issues_router)
api_router.include_router(relations_router)
api_router.include_router(versions_router)
api_router.include_router(recurring_patterns_router)
api_router.include_router(sprints_router)
api_router.include_router(tags_router)
api_router.include_router(attachments_router)
api_router.include_router(time_entries_router)
api_router.include_router(admin_settings_router)
api_router.include_router(wiki_router)
api_router.include_router(search_router)
api_router.include_router(search_admin_router)
api_router.include_router(admin_workflows_router)
api_router.include_router(notifications_router)
api_router.include_router(reactions_router)
api_router.include_router(saved_filters_router)
api_router.include_router(admin_embedding_models_router)
api_router.include_router(users_router)
api_router.include_router(agent_sessions_router)
api_router.include_router(admin_users_router)
api_router.include_router(admin_projects_router)
api_router.include_router(admin_email_router)
api_router.include_router(admin_metadata_presets_router)
api_router.include_router(admin_metadata_schemas_router)
api_router.include_router(metadata_router)
api_router.include_router(markdown_router)
