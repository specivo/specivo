"""Web layer dependencies: template loading and optional auth."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "themes"
SHARED_DIR = Path(__file__).resolve().parent.parent / "templates" / "_shared"

logger = logging.getLogger(__name__)

# Plugin asset URLs populated by setup_plugin_assets() at startup.
_plugin_css_files: list[str] = []
_plugin_js_files: list[str] = []

# Versioned static filenames populated by setup_versioned_assets() at startup.
_versioned_assets: dict[str, str] = {
    "specivo.css": "specivo.css",
    "specivo.js": "specivo.js",
}


def get_templates(theme: str = "default") -> Jinja2Templates:
    """Build a Jinja2Templates instance with theme-aware ChoiceLoader.

    Resolution order:
    1. Custom theme directory (if theme != "default" and exists)
    2. Default theme directory (always present)
    3. Shared templates (_shared/) for error pages etc.

    Registers ``has_feature`` as a Jinja2 global so all templates can
    use ``{% if has_feature("feature_name") %}`` without explicit
    context passing.
    """
    loaders: list[FileSystemLoader] = []

    if theme != "default":
        theme_dir = TEMPLATES_DIR / theme
        if theme_dir.exists():
            loaders.append(FileSystemLoader(str(theme_dir)))

    loaders.append(FileSystemLoader(str(TEMPLATES_DIR / "default")))

    if SHARED_DIR.exists():
        loaders.append(FileSystemLoader(str(SHARED_DIR)))

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR / "default"))
    templates.env.loader = ChoiceLoader(loaders)

    # i18n: install gettext callables for {% trans %} support
    from specivo.core.i18n import gettext, ngettext

    templates.env.add_extension("jinja2.ext.i18n")
    templates.env.install_gettext_callables(gettext, ngettext)

    # Feature-gating: expose has_feature() to every template
    from specivo.core.features import has_feature

    templates.env.globals["has_feature"] = has_feature

    # Plugin assets — module-level lists populated by setup_plugin_assets()
    templates.env.globals["plugin_css_files"] = _plugin_css_files
    templates.env.globals["plugin_js_files"] = _plugin_js_files

    # Versioned static filenames for cache busting
    templates.env.globals["versioned"] = _versioned_assets

    return templates


def setup_versioned_assets(versioned: dict[str, str]) -> None:
    """Update versioned asset filenames from create_app().

    Called once at startup after symlinks are created.
    """
    _versioned_assets.update(versioned)


def setup_plugin_assets(plugins: list) -> None:
    """Collect CSS/JS asset URLs from loaded plugins.

    Called from ``create_app()`` after plugin discovery so that
    ``base.html`` can auto-include plugin stylesheets and scripts.
    Mutates module-level lists that ``get_templates()`` references.
    """
    _plugin_css_files.clear()
    _plugin_js_files.clear()
    for plugin in plugins:
        assets = plugin.get_static_assets()
        _plugin_css_files.extend(assets.get("css", []))
        _plugin_js_files.extend(assets.get("js", []))


async def get_current_user_optional(
    request: Request,
    db: AsyncSession,
) -> object | None:
    """Try to resolve the current user from JWT cookie.

    Returns the User model if authenticated, None otherwise.
    Used by web pages that work for both logged-in and anonymous visitors.
    """
    from specivo.core.security import get_current_user

    try:
        return await get_current_user(request, db)
    except Exception:
        return None
