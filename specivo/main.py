"""Specivo — Your work. Your knowledge. Your infrastructure.

FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from specivo.api.v1.router import api_router
from specivo.core.config import get_settings
from specivo.core.database import get_engine
from specivo.core.exceptions import (
    AppError,
    app_error_handler,
    http_exception_handler,
    request_validation_error_handler,
)
from specivo.core.logging import setup_logging
from specivo.core.middleware import (
    AuditBatchMiddleware,
    LocaleMiddleware,
    RateLimitHeaderMiddleware,
    RequestIDMiddleware,
)
from specivo.core.plugin_manager import PluginManager
from specivo.core.redis import close_redis, get_redis
from specivo.hooks.router import hooks_router
from specivo.schemas.common import HealthResponse
from specivo.web.router import web_router


def _create_versioned_static_files() -> dict[str, str]:
    """Create versioned symlinks for CSS/JS files and return the filenames.

    e.g. specivo.css -> specivo.0.1.0.css
    Templates reference the versioned filename so CDN/proxy caches
    bust automatically on version bumps. No build step needed.
    """
    import os

    settings = get_settings()
    version = settings.version
    static_dir = Path(__file__).resolve().parent / "static"
    versioned = {}

    for subdir, base, ext in [
        ("css", "specivo", ".css"),
        ("js", "specivo", ".js"),
    ]:
        source = static_dir / subdir / f"{base}{ext}"
        target = static_dir / subdir / f"{base}.{version}{ext}"
        if source.exists():
            # Remove old versioned symlinks
            for old in source.parent.glob(f"{base}.*{ext}"):
                if old != source and old.is_symlink():
                    old.unlink()
            # Create new symlink
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(source.name, str(target))
            versioned[f"{base}{ext}"] = f"{base}.{version}{ext}"

    return versioned


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(debug=settings.debug)

    # Eagerly validate DB and Redis connectivity at startup so the container
    # fails fast rather than surfacing errors on the first request.
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    redis = await get_redis()
    await redis.ping()

    yield

    await close_redis()
    await engine.dispose()


_plugin_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Return the application-wide PluginManager singleton.

    Raises ``RuntimeError`` if ``create_app()`` has not been called yet.
    """
    if _plugin_manager is None:
        raise RuntimeError("PluginManager is not initialized — create_app() has not been called")
    return _plugin_manager


def create_app() -> FastAPI:
    global _plugin_manager  # noqa: PLW0603

    settings = get_settings()
    sp = settings.stealth_prefix.rstrip("/")

    application = FastAPI(
        title=settings.app_name,
        description=(
            "Your work. Your knowledge. Your infrastructure."
            " Self-hosted platform for project tracking, knowledge base,"
            " and AI-safe automation."
        ),
        version=settings.version,
        lifespan=lifespan,
        docs_url=f"{sp}/docs",
        redoc_url=f"{sp}/redoc",
        openapi_url=f"{sp}{settings.api_v1_prefix}/openapi.json",
    )

    # Request ID — added first so it is outermost (runs before CORS).
    application.add_middleware(RequestIDMiddleware)

    # Audit batch — collects audit events during the request and flushes
    # them in a single batch INSERT after the response is sent.
    application.add_middleware(AuditBatchMiddleware)

    # Rate limit headers — copies X-RateLimit-* headers from request.state
    # onto the response at the ASGI level so they survive endpoints that
    # return their own Response objects (e.g. JSONResponse).
    application.add_middleware(RateLimitHeaderMiddleware)

    # Locale — detect language from cookie/header and activate per-request.
    application.add_middleware(LocaleMiddleware)

    # Trusted hosts — reject requests with unexpected Host headers in production
    if settings.allowed_hosts != ["*"]:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    # CORS — must be added before routers so the middleware wraps all routes.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Plugin system — discover and load plugins from settings.
    pm = PluginManager()

    # Register core services so that plugins can override them.
    from specivo.services.api_key_service import ApiKeyService
    from specivo.services.journal_service import JournalService
    from specivo.services.notification_service import NotificationService
    from specivo.services.workflow_service import WorkflowService

    pm.service_registry.register("journal", JournalService)
    pm.service_registry.register("api_key", ApiKeyService)
    pm.service_registry.register("workflow", WorkflowService)
    pm.service_registry.register("notification", NotificationService)

    if settings.installed_plugins:
        pm.load_plugins(settings.installed_plugins)
    pm.register_services()
    pm.register_features()
    _plugin_manager = pm

    # Register plugin locale directories for i18n catalog merging.
    from specivo.core.i18n import _extra_locale_dirs

    for plugin in pm.plugins:
        for locale_dir, domain in plugin.get_locale_dirs():
            _extra_locale_dirs.append((locale_dir, domain))

    # Mount plugin static directories (e.g. pro/enterprise CSS/JS).
    for plugin in pm.plugins:
        for dir_path, url_path in plugin.get_static_dirs():
            if dir_path.exists():
                application.mount(
                    url_path,
                    StaticFiles(directory=str(dir_path)),
                    name=f"static_{plugin.name}",
                )

    # Versioned static filenames for cache busting in templates.
    from specivo.web.deps import setup_plugin_assets, setup_versioned_assets

    setup_versioned_assets(_create_versioned_static_files())

    # Collect plugin CSS/JS asset URLs for auto-inclusion in base.html.
    setup_plugin_assets(pm.plugins)

    # Run plugin on_startup hooks (e.g. enterprise audit middleware activation)
    for plugin in pm.plugins:
        plugin.on_startup(application)

    # Error handlers — register most specific first.
    application.add_exception_handler(RequestValidationError, request_validation_error_handler)
    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(HTTPException, http_exception_handler)

    # robots.txt — always at root, not behind stealth prefix.
    # Overridable: admin settings key "robots_txt" > env ROBOTS_TXT > default (disallow all).
    @application.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def robots_txt():
        from specivo.core.database import get_db
        from specivo.services.settings_service import SettingsService

        try:
            async for db in get_db():
                all_settings = await SettingsService().get_all(db)
                custom = all_settings.get("robots_txt")
                if custom:
                    return custom
        except Exception:
            pass
        return settings.robots_txt

    # API router — behind stealth prefix when configured
    application.include_router(api_router, prefix=sp)

    # Plugin routers — mounted after core API router so pro endpoints
    # are available under the same /api/v1 prefix.
    api_v1_prefix = f"{sp}{settings.api_v1_prefix}"
    for plugin in pm.plugins:
        for router_entry in plugin.get_routers(prefix=api_v1_prefix):
            plugin_router, kwargs = router_entry
            application.include_router(plugin_router, **kwargs)

    # Incoming webhooks — behind stealth prefix
    application.include_router(hooks_router, prefix=sp)

    # Static files — always at /static (not behind stealth prefix)
    application.mount("/static", StaticFiles(directory="specivo/static"), name="static")

    # Web pages — behind stealth prefix, AFTER API router (catch-all paths)
    application.include_router(web_router, prefix=sp)

    # Health check — behind stealth prefix.
    # Returns minimal info publicly; detailed diagnostics require admin auth.
    @application.get(f"{sp}/health", response_model=HealthResponse, tags=["system"])
    async def health_check():
        """Check database and Redis connectivity.

        The public (unauthenticated) response only contains ``{"status": "ok"}``.
        Detailed fields (version, tier, database/redis status) are included
        only for admin users to prevent information disclosure.
        """
        db_status = "ok"
        redis_status = "ok"

        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            logging.getLogger(__name__).error("Health check DB error: %s", exc)
            db_status = "error"

        try:
            r = await get_redis()
            await r.ping()
        except Exception as exc:
            logging.getLogger(__name__).error("Health check Redis error: %s", exc)
            redis_status = "error"

        overall = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"

        # Public response: only status field (no version/tier/component details)
        return HealthResponse(
            status=overall,
            database=db_status,
            redis=redis_status,
            version="",
            tier="",
        )

    return application


app = create_app()
