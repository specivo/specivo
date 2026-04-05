"""Web layer dependencies: template loading and optional auth."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.models.user import User

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

# Brand name — populated from DB setting at startup, updated via admin.
_brand_name: str = "Specivo"


def set_brand_name(name: str) -> None:
    """Update the in-memory brand name (called from startup/admin)."""
    global _brand_name
    _brand_name = name or "Specivo"


def get_brand_name() -> str:
    return _brand_name


def _timeago(dt, mode="smart") -> str:
    """Convert a datetime to a human-readable relative time string.

    Modes:
      "smart" (default): today → relative, yesterday → "Yesterday", older → date
      "relative": always relative ("5 min ago", "3 days ago", "2 months ago")
      "date": always show date ("Mar 28" or "Mar 28, 2025")
    """
    if dt is None:
        return ""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    delta = now - dt
    seconds = int(delta.total_seconds())
    today = now.date()
    dt_date = dt.date()

    if mode == "date":
        if dt_date.year == today.year:
            return dt.strftime("%b %d")
        return dt.strftime("%b %d, %Y")

    # Relative time calculation (shared by "smart" and "relative")
    def _relative() -> str:
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hr{'s' if hours != 1 else ''} ago"
        days = seconds // 86400
        if days < 7:
            return f"{days} day{'s' if days != 1 else ''} ago"
        if days < 30:
            weeks = days // 7
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        months = days // 30
        if months < 12:
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = months // 12
        return f"{years} year{'s' if years != 1 else ''} ago"

    if mode == "relative":
        return _relative()

    # "smart" mode: today → relative, yesterday → "Yesterday", older → date
    if dt_date == today:
        return _relative()

    if dt_date == today - timedelta(days=1):
        return "Yesterday"

    if dt_date.year == today.year:
        return dt.strftime("%b %d")

    return dt.strftime("%b %d, %Y")


def get_templates(theme: str = "default") -> Jinja2Templates:
    """Build a Jinja2Templates instance with theme-aware ChoiceLoader.

    Resolution order:
    1. Custom theme from data dir (``data/themes/{theme}/``, user-provided)
    2. Built-in theme (``specivo/templates/themes/{theme}/``, if not "default")
    3. Default theme (``specivo/templates/themes/default/``, always present)
    4. Custom error pages (``data/errors/``, user-provided: 403.html, 404.html, 500.html)
    5. Shared templates (``specivo/templates/_shared/``) for error pages

    Missing directories are silently skipped — the app never breaks if a
    custom folder doesn't exist.
    """
    from specivo.core.config import get_settings

    settings = get_settings()
    loaders: list[FileSystemLoader] = []

    if theme != "default":
        # Custom theme from data mount (user-provided overrides)
        custom_theme_dir = Path(settings.custom_themes_dir) / theme
        if custom_theme_dir.is_dir():
            loaders.append(FileSystemLoader(str(custom_theme_dir)))

        # Built-in theme (baked into image)
        builtin_dir = TEMPLATES_DIR / theme
        if builtin_dir.is_dir():
            loaders.append(FileSystemLoader(str(builtin_dir)))

    # Default theme (always present)
    loaders.append(FileSystemLoader(str(TEMPLATES_DIR / "default")))

    # Custom error pages from data mount (403.html, 404.html, 500.html)
    custom_errors_dir = Path(settings.custom_errors_dir)
    if custom_errors_dir.is_dir():
        loaders.append(FileSystemLoader(str(custom_errors_dir)))

    # Built-in shared templates (error pages, email templates)
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

    # Brand name — from DB setting, available in all templates
    templates.env.globals["brand_name"] = _brand_name

    # Plugin assets — module-level lists populated by setup_plugin_assets()
    templates.env.globals["plugin_css_files"] = _plugin_css_files
    templates.env.globals["plugin_js_files"] = _plugin_js_files

    # Versioned static filenames for cache busting
    templates.env.globals["versioned"] = _versioned_assets

    # Debug mode and version — for footer display
    templates.env.globals["debug"] = settings.debug
    templates.env.globals["app_version"] = settings.version

    # Git commit hash (debug only, resolved once at startup)
    if settings.debug:
        _commit = ""
        try:
            import subprocess

            _commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2, cwd="/app",
            ).stdout.strip()
        except Exception:
            pass
        if not _commit:
            # Fallback: read from GIT_COMMIT env var or VERSION file
            import os

            _commit = os.environ.get("GIT_COMMIT", "")
        templates.env.globals["git_commit"] = _commit

    # Markdown filter for wiki content
    import markdown as _md
    import markupsafe
    import nh3

    allowed_tags = {
        "p",
        "br",
        "hr",
        "a",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "del",
        "code",
        "pre",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "img",
        "div",
        "span",
        "mark",
        "sup",
        "sub",
        "dl",
        "dt",
        "dd",
    }
    allowed_attributes: dict[str, set[str]] = {
        "a": {"href", "title", "id"},
        "img": {"src", "alt", "title", "width", "height"},
        "th": {"align", "colspan", "rowspan"},
        "td": {"align", "colspan", "rowspan"},
        "code": {"class"},
        "pre": {"class"},
        "div": {"class"},
        "span": {"class"},
        "h1": {"id"},
        "h2": {"id"},
        "h3": {"id"},
        "h4": {"id"},
        "h5": {"id"},
        "h6": {"id"},
    }

    def _sanitize_html(html_str: str) -> str:
        return nh3.clean(
            html_str,
            tags=allowed_tags,
            attributes=allowed_attributes,
            link_rel="noopener noreferrer",
            url_schemes={"http", "https", "mailto"},
        )

    def _render_markdown(text: str) -> markupsafe.Markup:
        html = _md.markdown(
            text or "",
            extensions=["fenced_code", "tables", "toc", "codehilite"],
            extension_configs={
                "codehilite": {"css_class": "codehilite", "guess_lang": False},
            },
        )
        return markupsafe.Markup(_sanitize_html(html))

    templates.env.filters["markdown"] = _render_markdown

    _wiki_link_re = __import__("re").compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")

    def _render_wiki_markdown(text: str, project_key: str = "") -> markupsafe.Markup:
        """Render markdown with wiki [[Page Name]] link support."""
        if not text:
            return markupsafe.Markup("")

        from specivo.services.wiki_utils import slugify as wiki_slugify

        def _replace_wiki_link(m):
            target = m.group(1).strip()
            display = m.group(2)
            if display:
                display = display.strip()
                display = display.replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")
            else:
                display = target
                display = display.replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")
            slug = wiki_slugify(target)
            url = f"/projects/{project_key}/wiki/{slug}/"
            return f"[{display}]({url})"

        processed = _wiki_link_re.sub(_replace_wiki_link, text)
        rendered = _md.markdown(
            processed,
            extensions=["fenced_code", "tables", "toc", "codehilite"],
            extension_configs={
                "codehilite": {"css_class": "codehilite", "guess_lang": False},
            },
        )
        return markupsafe.Markup(_sanitize_html(rendered))

    templates.env.filters["wiki_markdown"] = _render_wiki_markdown

    # Syntax highlighting filter for raw code strings (e.g. SQL debug panel)
    from pygments import highlight as _pygments_highlight
    from pygments.formatters import HtmlFormatter as _HtmlFormatter
    from pygments.lexers import SqlLexer as _SqlLexer

    _sql_lexer = _SqlLexer()
    _code_formatter = _HtmlFormatter(nowrap=True)

    def _highlight_sql(text: str) -> markupsafe.Markup:
        return markupsafe.Markup(_pygments_highlight(text or "", _sql_lexer, _code_formatter))

    templates.env.filters["highlight_sql"] = _highlight_sql

    # Timeago filter for relative timestamps
    templates.env.filters["timeago"] = _timeago

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


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User:
    """Dependency: return the authenticated user or redirect to login.

    Uses HTTPException with Location header to trigger a 302 redirect
    when the user is not authenticated.
    """
    from fastapi import HTTPException

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        raise HTTPException(status_code=302, headers={"Location": "/login/"})
    return user_obj  # type: ignore[return-value]


async def get_current_user_optional(
    request: Request,
    db: AsyncSession,
) -> object | None:
    """Try to resolve the current user from JWT cookie.

    Returns the User model if authenticated, None otherwise.
    Used by web pages that work for both logged-in and anonymous visitors.

    Silent refresh: when the access token has expired but a valid
    ``refresh_token`` cookie is present, the function calls
    ``AuthService.refresh()`` to obtain new tokens.  The new tokens
    are stored on ``request.state.refreshed_tokens`` so the
    ``TokenRefreshMiddleware`` can set the cookies on the response.
    """
    from specivo.core.exceptions import AppError
    from specivo.core.security import get_current_user

    try:
        return await get_current_user(request, db)
    except AppError as exc:
        # Silent refresh applies when:
        # - "auth_token_expired": JWT is present but expired
        # - "unauthorized": no access_token cookie at all (browser deleted it after max_age)
        # In both cases, a valid refresh_token cookie can restore the session.
        if exc.code not in ("auth_token_expired", "unauthorized"):
            return None

        # Access token expired or missing — attempt silent refresh using
        # the refresh_token cookie (if present).
        refresh_token_raw = request.cookies.get("refresh_token")
        if not refresh_token_raw:
            return None

        try:
            import jwt as pyjwt

            from specivo.core.config import get_settings
            from specivo.services.auth_service import AuthService

            # Read "remember me" preference from the expired access token
            # so we can carry it forward to the refreshed cookies.
            settings = get_settings()
            remember = True  # backward compat default
            old_access = request.cookies.get("access_token")
            if old_access:
                try:
                    payload = pyjwt.decode(
                        old_access,
                        settings.secret_key,
                        algorithms=["HS256"],
                        options={"verify_exp": False},
                    )
                    remember = payload.get("rem", True)
                except Exception:
                    pass

            svc = AuthService()
            access_token, new_refresh_token = await svc.refresh(
                session=db,
                refresh_token_raw=refresh_token_raw,
                remember=remember,
            )

            # Store tokens so TokenRefreshMiddleware can set cookies.
            request.state.refreshed_tokens = {
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "remember": remember,
            }

            # Re-authenticate with the fresh access token to get the User.
            # Patch the cookie on the request so get_current_user reads it.
            request._cookies = {**request.cookies, "access_token": access_token}
            return await get_current_user(request, db)
        except Exception:
            logger.debug("Silent token refresh failed", exc_info=True)
            return None
    except Exception:
        return None
