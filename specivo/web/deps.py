"""Web layer dependencies: template loading and optional auth."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, Request, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, pass_context
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.middleware import ANONYMOUS_READER_STATE_KEY
from specivo.models.user import User

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "themes"
SHARED_DIR = Path(__file__).resolve().parent.parent / "templates" / "_shared"

logger = logging.getLogger(__name__)

# Plugin asset URLs populated by setup_plugin_assets() at startup.
_plugin_css_files: list[str] = []
_plugin_js_files: list[str] = []


def _resolve_git_commit() -> str:
    """Resolve the short git commit hash once at import time.

    Tries ``/app`` first (Docker), then the package source directory (local dev),
    and finally falls back to the ``GIT_COMMIT`` environment variable.
    """
    # The source tree lives two levels above this file (specivo/web/deps.py).
    _src_dir = str(Path(__file__).resolve().parent.parent.parent)
    for cwd in ("/app", _src_dir):
        try:
            result = subprocess.run(  # noqa: S603, S607
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=cwd,
            )
            commit = result.stdout.strip()
            if commit:
                return commit
        except Exception:
            continue
    return os.environ.get("GIT_COMMIT", "")


_git_commit: str = _resolve_git_commit()

# Versioned (content-hashed) bundle filenames populated by
# setup_versioned_assets() at startup from the esbuild manifests. Defaults are
# the un-hashed names so templates still render if a build manifest is missing.
_versioned_assets: dict[str, str] = {
    "specivo.min.css": "specivo.min.css",
    "alpine-init.min.js": "alpine-init.min.js",
    "app.min.js": "app.min.js",
}

# Brand name — populated from DB setting at startup, updated via admin.
_brand_name: str = "Specivo"


def set_brand_name(name: str) -> None:
    """Update the in-memory brand name (called from startup/admin)."""
    global _brand_name
    _brand_name = name or "Specivo"


def get_brand_name() -> str:
    return _brand_name


async def get_active_sprint_id(db, project_id: int) -> int | None:
    """Return the active sprint ID for a project, or None."""
    from sqlalchemy import select

    from specivo.models.sprint import Sprint

    result = await db.execute(
        select(Sprint.id).where(Sprint.project_id == project_id, Sprint.status == "active")
    )
    row = result.first()
    return row[0] if row else None


def _to_user_tz(dt, tz_name: str = "UTC"):
    """Convert a UTC datetime to the given IANA timezone.

    Returns a timezone-aware datetime in the user's local timezone.
    If *tz_name* is invalid, falls back to UTC silently.
    """
    if dt is None:
        return None
    from datetime import UTC
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    try:
        tz = ZoneInfo(tz_name) if tz_name else UTC
    except (ZoneInfoNotFoundError, KeyError):
        tz = UTC

    return dt.astimezone(tz)


def _localtime(dt, tz_name: str = "UTC") -> str:
    """Jinja2 filter: convert UTC datetime to user timezone, format as 'YYYY-MM-DD HH:MM'.

    Usage in templates: ``{{ dt | localtime(user.timezone) }}``
    """
    local_dt = _to_user_tz(dt, tz_name)
    if local_dt is None:
        return ""
    return local_dt.strftime("%Y-%m-%d %H:%M")


def _localdt(dt, tz_name: str = "UTC", fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Jinja2 filter: convert UTC datetime to user timezone with custom format.

    Usage in templates: ``{{ dt | localdt(user.timezone, '%b %d, %Y at %H:%M') }}``
    """
    local_dt = _to_user_tz(dt, tz_name)
    if local_dt is None:
        return ""
    return local_dt.strftime(fmt)


def _timeago(dt, tz_name: str = "UTC", mode: str = "smart") -> str:
    """Convert a datetime to a human-readable relative time string.

    The *tz_name* parameter is used when the mode falls back to an absolute
    date — relative times ("5 min ago") are timezone-agnostic, but "today"
    vs "yesterday" boundaries and formatted dates use the user's timezone.

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

    # Convert to user timezone for date boundary comparisons
    local_dt = _to_user_tz(dt, tz_name)
    local_now = _to_user_tz(now, tz_name)
    today = local_now.date()
    dt_date = local_dt.date()

    if mode == "date":
        if dt_date.year == today.year:
            return local_dt.strftime("%b %d")
        return local_dt.strftime("%b %d, %Y")

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
        return local_dt.strftime("%b %d")

    return local_dt.strftime("%b %d, %Y")


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

    # Git commit hash (debug only, resolved once at import time)
    templates.env.globals["git_commit"] = _git_commit if settings.debug else ""

    # Markdown filters — both bind to the shared renderer in
    # specivo.services.markdown_service so saved-content rendering and the
    # editor preview endpoint cannot drift.
    from specivo.services.markdown_service import (
        render_plain_markdown,
        render_wiki_markdown,
    )

    templates.env.filters["markdown"] = render_plain_markdown

    # wiki_markdown is a context filter so it can read the per-request set of
    # validated issue references (``known_issue_refs``) placed in the template
    # context by page handlers. When absent, every KEY-123 token is linked
    # (original behaviour); when present, only existing/previously-existing refs
    # are linked. See IssueService.resolve_known_issue_refs.
    from jinja2 import pass_context

    @pass_context
    def _wiki_markdown_filter(ctx, text, project_key="", attachment_map=None):
        return render_wiki_markdown(
            text,
            project_key,
            attachment_map,
            known_issue_refs=ctx.get("known_issue_refs"),
        )

    templates.env.filters["wiki_markdown"] = _wiki_markdown_filter

    # Syntax highlighting filter for raw code strings (e.g. SQL debug panel)
    import markupsafe
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

    # Timezone-aware datetime filters
    templates.env.filters["localtime"] = _localtime
    templates.env.filters["localdt"] = _localdt

    # Metadata diff formatting for activity log
    def _format_metadata_diff(
        old_json: str | None, new_json: str | None
    ) -> list[dict[str, str]]:
        """Parse two metadata JSON blobs and return per-key diffs.

        Returns a list of dicts with keys: ``key``, ``old``, ``new``.
        Only keys that actually changed are included.
        """
        import json as _json

        def _parse(raw: str | None) -> dict:
            if not raw:
                return {}
            try:
                return _json.loads(raw)
            except (ValueError, TypeError):
                return {}

        def _truncate(val: object, limit: int = 80) -> str:
            s = _json.dumps(val, separators=(",", ":"), default=str) if not isinstance(val, str) else val
            if len(s) > limit:
                return s[:limit] + "\u2026"
            return s

        old_d = _parse(old_json)
        new_d = _parse(new_json)
        all_keys = sorted(set(old_d) | set(new_d))
        diffs: list[dict[str, str]] = []
        for k in all_keys:
            ov = old_d.get(k)
            nv = new_d.get(k)
            if ov != nv:
                diffs.append({
                    "key": k,
                    "old": _truncate(ov) if ov is not None else "",
                    "new": _truncate(nv) if nv is not None else "",
                })
        return diffs

    templates.env.filters["metadata_diff"] = _format_metadata_diff
    templates.env.globals["metadata_diff"] = _format_metadata_diff

    # Where the "Sign in" action on an anonymous page points. A context
    # function rather than a per-handler context variable, because the header
    # is included by every page and would otherwise need the value passed
    # through all of them.
    templates.env.globals["sign_in_url"] = _sign_in_url

    return templates


def setup_versioned_assets(versioned: dict[str, str]) -> None:
    """Update versioned asset filenames from create_app().

    Called once at startup with the merged esbuild bundle manifests.
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


# Where a user carrying ``must_change_password`` is sent, and the only web
# paths that survive the redirect. Anything else would bounce back here.
#
# The check runs in ``get_current_user_optional``, which every web page and
# partial resolves its user through.
#
# ``/logout/`` resolves no user of its own today, so it is already out of reach
# of this check. It is listed anyway: being able to leave an account whose
# password somebody else chose must not depend on that staying true.
#
# Static files, ``/static/...``, are mounted outside the router and never touch
# this dependency, so the page keeps its CSS and bundles. The form posts to
# ``/api/v1/auth/change-password/``, which the API side exempts.
CHANGE_PASSWORD_PATH = "/my/password/"
_PASSWORD_CHANGE_ALLOWED_SUFFIXES = (CHANGE_PASSWORD_PATH, "/logout/")


def redirect_if_password_change_required(request: Request, user: object) -> None:
    """Send a user who must change their password to the page that lets them.

    Raises the 302 ``HTTPException`` the web layer uses for redirects. Returns
    quietly when the flag is not set, or when the request is already for one of
    the pages a forced user is allowed to reach — without that exception the
    change-password page would redirect to itself forever.
    """
    if not getattr(user, "must_change_password", False):
        return

    from specivo.core.config import get_settings

    sp = get_settings().stealth_prefix.rstrip("/")
    path = request.url.path
    if any(path.startswith(sp + suffix) for suffix in _PASSWORD_CHANGE_ALLOWED_SUFFIXES):
        return

    from fastapi import HTTPException

    raise HTTPException(status_code=302, headers={"Location": f"{sp}{CHANGE_PASSWORD_PATH}"})


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User:
    """Dependency: return the authenticated user or redirect to login.

    Uses HTTPException with Location header to trigger a 302 redirect
    when the user is not authenticated.

    The redirect to the change-password page is not made here: it belongs to
    ``get_current_user_optional`` below, which every web page goes through —
    most of them without ever calling this dependency.
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

    Raises a 302 ``HTTPException`` — the redirect the web layer already uses —
    when the resolved user must change their password and the request is not
    for one of the pages that lets them. Returning the user instead would let
    every page that only checks for ``None`` render normally.

    Silent refresh: when the access token has expired but a valid
    ``refresh_token`` cookie is present, the function calls
    ``AuthService.refresh()`` to obtain new tokens.  The new tokens
    are stored on ``request.state.refreshed_tokens`` so the
    ``TokenRefreshMiddleware`` can set the cookies on the response.
    """
    from specivo.core.exceptions import AppError
    from specivo.core.security import authenticate_request

    try:
        # The forced password-change gate is off here on purpose. It answers
        # with 403, which this function would read as "not signed in" and turn
        # into a redirect to the login page — where an already-signed-in user
        # has nothing to do. The web layer needs the resolved user so it can
        # send it to the change-password page instead; see ``require_user``.
        user_obj = await authenticate_request(request, db, enforce_password_change=False)
    except AppError:
        # get_current_user now performs silent refresh internally for the
        # cookie-based paths ("auth_token_expired" + missing access_token).
        # Any other AppError (invalid/revoked token, locked/deactivated
        # account) falls through to rendering the page as anonymous.
        return None
    except Exception:
        return None

    # Per-user locale override: the LocaleMiddleware runs before auth, so the
    # user's saved language preference is not yet known when it activates a
    # locale. Now that the authenticated user is resolved, re-activate their
    # preferred language so it wins over the middleware-detected default. The
    # middleware's `finally: deactivate()` still resets the contextvar after
    # the request completes.
    user_language = getattr(user_obj, "language", None)
    if user_language:
        from specivo.core.i18n import activate
        from specivo.core.locales import get_available_locales

        if user_language in get_available_locales():
            activate(user_language)

    # Last step, and the reason it is here rather than in ``require_user``:
    # only a handful of pages use that dependency. Every other page — issues,
    # wiki, projects, sprints, the dashboard — resolves its user through this
    # function and redirects to /login/ itself, so this is the one place a
    # forced password change can be enforced across the whole web surface.
    redirect_if_password_change_required(request, user_obj)

    return user_obj


# ---------------------------------------------------------------------------
# Anonymous web reading
# ---------------------------------------------------------------------------

# The web pages that may be served to a visitor without an account, as the
# path templates FastAPI registers them under.
#
# This is the whole surface anonymous browsing has, and it is the web mirror
# of ``specivo.core.security.ANONYMOUS_READ_ROUTES``: a page joins it by being
# written here *and* by depending on ``get_web_reader``, and the contract test
# in ``tests/integration/test_anonymous_route_contract.py`` fails when the two
# disagree.
#
# What is deliberately absent is as much of the decision as what is present:
# the dashboard, sprints, the backlog, the roadmap, versions, time entries,
# recurring patterns, project settings, the wiki page list, history, diffs and
# the trash, every form and every htmx partial. The wiki index is here only
# because it is a redirect to the home page — without it the "Wiki" link on a
# project people can read would send them to the login screen.
ANONYMOUS_WEB_ROUTES: frozenset[str] = frozenset(
    {
        "/projects/",
        "/projects/{key}/",
        "/projects/{project_key}/issues/",
        "/issue/{issue_ref}/",
        "/projects/{project_key}/wiki/",
        "/projects/{project_key}/wiki/{slug}/",
        "/search/",
    }
)

# Where a refused visitor is sent. Bare, exactly as ``require_user`` and every
# page handler has always spelled it, so the refusal stays the redirect the web
# layer already used.
LOGIN_PATH = "/login/"

# A ``next`` longer than this is dropped rather than echoed into a redirect.
_MAX_NEXT_LENGTH = 2000


def safe_next_path(raw: str | None) -> str:
    """Return *raw* if it is somewhere this application may send a browser.

    Only a path within this site survives: it must start with a single ``/``
    and carry nothing that could turn it into another origin or a second
    header. Everything else becomes the empty string, which callers read as
    "no destination" and fall back to the dashboard.

    ``//evil.example`` and ``/\\evil.example`` are the two forms browsers
    resolve as protocol-relative URLs, so both are refused even though they
    look local.
    """
    if not raw or len(raw) > _MAX_NEXT_LENGTH:
        return ""
    if not raw.startswith("/") or raw.startswith(("//", "/\\")):
        return ""
    if any(char in raw for char in "\r\n\t") or any(ord(char) < 0x20 for char in raw):
        return ""
    return raw


def _current_path(request: Request) -> str:
    """The path and query the visitor asked for, as a single relative URL."""
    path = request.url.path
    return f"{path}?{request.url.query}" if request.url.query else path


def login_url_for(target: str) -> str:
    """The login URL that returns a visitor to *target* once they sign in."""
    destination = safe_next_path(target)
    if not destination:
        return LOGIN_PATH
    return f"{LOGIN_PATH}?next={quote(destination, safe='')}"


@pass_context
def _sign_in_url(ctx) -> str:
    """Template global: the login URL that comes back to the current page.

    A context function so templates can call it as ``sign_in_url()`` without
    every handler having to put the value in its context.
    """
    request = ctx.get("request")
    if request is None:
        return LOGIN_PATH
    return login_url_for(_current_path(request))


def refuse_anonymous_web(request: Request) -> Response:
    """The one refusal every allowlisted web page produces.

    A redirect to the login page, and nothing else. It is returned for a
    project that does not exist, one that is private, one that is not opted in,
    one that is archived, an issue the visitor may not see, a permission the
    project did not grant, credentials that failed, and an instance whose
    switch is off — so comparing responses tells a visitor nothing about which
    projects exist or why a page was withheld.

    The response is a pure function of the URL that was asked for: the only
    part that varies is ``next``, which echoes back what the visitor typed.
    That is what makes the refusals comparable at all, and it is why the cache
    headers are set here rather than left to
    ``AnonymousResponseHeadersMiddleware``. The middleware only marks responses
    that resolved to the anonymous principal, so with the switch off a refusal
    would arrive without them — and a shared cache header would then be enough
    to tell a switched-off instance from a project that refused.
    """
    from fastapi.responses import RedirectResponse

    response = RedirectResponse(login_url_for(_current_path(request)), status_code=302)
    setattr(request.state, ANONYMOUS_READER_STATE_KEY, False)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie, Authorization"
    return response


async def get_web_reader(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AsyncGenerator[object | None, None]:
    """Dependency: the signed-in user, the anonymous principal, or nothing.

    The web counterpart of ``specivo.core.security.get_reader``, and a thin one
    on purpose: the rules that decide what an anonymous visitor is subject to
    live in ``anonymous_read_scope``, which both dependencies share, so the
    pages cannot end up more permissive than the API.

    Three cases, and the middle one is the point:

    - **Credentials present** — resolved by ``get_current_user_optional``,
      exactly as every page already resolved them, with the forced
      password-change redirect and the per-user locale it performs. A
      credential that fails resolves to ``None`` here, never to the anonymous
      principal: a visitor whose session expired is asked to sign in again
      rather than quietly shown the public half of the site while believing
      they are signed in.
    - **No credentials at all** — the anonymous principal, but only while the
      instance switch is on and the row exists.
    - **Neither** — ``None``, which every handler turns into
      :func:`refuse_anonymous_web`.

    Handlers receive ``None`` rather than an exception so the refusal stays a
    returned ``RedirectResponse``, which is how the web layer has always
    refused; nothing about a page outside this allowlist changes.
    """
    from specivo.core.security import (
        anonymous_read_scope,
        has_credentials,
        resolve_anonymous_principal,
    )

    if has_credentials(request):
        yield await get_current_user_optional(request, db)
        return

    try:
        anonymous = await resolve_anonymous_principal(db)
    except Exception:
        # Fail closed. Resolving the principal reads the instance switch from
        # the database, and a request that cannot establish whether anonymous
        # access is switched on must not be served as though it were: the
        # visitor gets the same refusal an instance with the switch off
        # produces. Refusing on this path costs an unauthenticated visitor a
        # login screen; guessing could publish a project nobody opted in.
        logger.warning("Could not resolve anonymous access; refusing the request", exc_info=True)
        anonymous = None

    if anonymous is None:
        yield None
        return

    async with anonymous_read_scope(request, response, db):
        yield anonymous
