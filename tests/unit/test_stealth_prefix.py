"""The URL prefix (``STEALTH_PREFIX``) helpers and every place the UI applies them.

``specivo.core.app_url`` is the one place the prefix is read. These tests pin
the helpers, the ``next`` handling built on them, the template global and
filter, the rendered Markdown, and the browser-side ``spUrl()``, each with
and without a prefix.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.routing import _IncludedRouter
from pydantic import ValidationError

from specivo.core.app_url import (
    RESERVED_FIRST_SEGMENTS,
    absolute_url,
    base_path,
    is_under_base,
    normalize_prefix,
    prefixed,
    public_url,
    strip_base,
)
from specivo.core.config import Settings, get_settings
from specivo.services.markdown_service import classify_link_target, render_wiki_markdown
from specivo.web.deps import get_templates, login_url_for, redirect_to, safe_next_path

pytestmark = pytest.mark.unit

PREFIX = "/hidden-ui"
GLOBALS_JS = Path(__file__).resolve().parents[2] / "frontend" / "js" / "lib" / "globals.js"


@pytest.fixture
def prefix(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(get_settings(), "stealth_prefix", PREFIX)
    return PREFIX


@pytest.fixture
def no_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "stealth_prefix", "")


# ---------------------------------------------------------------------------
# normalize_prefix and the setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("  ", ""),
        ("/hidden-ui", "/hidden-ui"),
        ("/hidden-ui/", "/hidden-ui"),
        ("hidden-ui", "/hidden-ui"),
        ("/a.b_c~d/e-f/", "/a.b_c~d/e-f"),
    ],
)
def test_normalize_prefix(raw: str, expected: str) -> None:
    assert normalize_prefix(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/hid den",
        "/a'b",
        '/a"b',
        "/a<b",
        "/a%2Fb",
        "/a?b",
        "/a#b",
        "/./x",
        "/x/..",
        "/a//b",
        "/api",
        "/static/x",
        "/Projects",
    ],
)
def test_normalize_prefix_refuses(raw: str) -> None:
    with pytest.raises(ValueError, match="STEALTH_PREFIX"):
        normalize_prefix(raw)


def test_setting_is_normalized_and_validated() -> None:
    base = {"database_url": "postgresql+asyncpg://u:p@localhost/db", "redis_url": "redis://x:6379/0"}
    settings = Settings(_env_file=None, secret_key="x" * 40, stealth_prefix="/hidden-ui/", **base)  # type: ignore[arg-type]
    assert settings.stealth_prefix == "/hidden-ui"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key="x" * 40, stealth_prefix="/a'b", **base)  # type: ignore[arg-type]


def test_reserved_segments_cover_every_route() -> None:
    """A prefix can never start like an application path, so ``prefixed`` stays unambiguous."""
    from specivo.main import create_app

    def routes(items):
        for route in items:
            if isinstance(route, _IncludedRouter):
                yield from routes(route.effective_candidates())
            else:
                yield route

    first = {getattr(r, "path", "").strip("/").split("/")[0] for r in routes(create_app().routes)} - {""}
    assert first <= RESERVED_FIRST_SEGMENTS, sorted(first - RESERVED_FIRST_SEGMENTS)


# ---------------------------------------------------------------------------
# base_path / prefixed / strip_base / is_under_base / absolute URLs
# ---------------------------------------------------------------------------


def test_helpers_without_a_prefix(no_prefix: None) -> None:
    assert base_path() == ""
    assert prefixed("/projects/") == "/projects/"
    assert strip_base("/projects/") == "/projects/"
    assert is_under_base("/anything/")
    assert not is_under_base("relative")


def test_helpers_with_a_prefix(prefix: str) -> None:
    assert base_path() == prefix
    assert prefixed("/projects/ACME/") == f"{prefix}/projects/ACME/"
    assert prefixed("/") == f"{prefix}/"
    # Idempotent: a path that already carries the prefix is left alone.
    assert prefixed(f"{prefix}/projects/") == f"{prefix}/projects/"
    assert prefixed(prefix) == prefix
    assert prefixed(f"{prefix}?x=1") == f"{prefix}?x=1"
    # A path that only starts with the same characters is not under it.
    assert prefixed(f"{prefix}-other/") == f"{prefix}{prefix}-other/"
    assert strip_base(f"{prefix}/issue/ACME-1/") == "/issue/ACME-1/"
    assert strip_base(prefix) == "/"
    assert strip_base("/issue/ACME-1/") == "/issue/ACME-1/"
    assert is_under_base(f"{prefix}/x") and is_under_base(prefix)
    assert not is_under_base("/projects/") and not is_under_base(f"{prefix}-other/")


@pytest.mark.parametrize("bad", ["projects/", "https://x.example/", "//evil.example/"])
def test_prefixed_takes_root_relative_paths_only(bad: str) -> None:
    with pytest.raises(ValueError, match="root-relative"):
        prefixed(bad)


def test_absolute_urls_carry_the_prefix(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setattr(get_settings(), "app_url", "https://tracker.example.com/")
    assert absolute_url("/scim/v2/x") == f"https://tracker.example.com{prefix}/scim/v2/x"
    assert public_url("/issue/ACME-1/") == f"https://tracker.example.com{prefix}/issue/ACME-1/"


def test_absolute_url_takes_explicit_settings() -> None:
    settings = get_settings().model_copy(update={"app_url": "https://t.example", "stealth_prefix": "/s/"})
    assert absolute_url("/auth/x/", settings) == "https://t.example/s/auth/x/"
    assert prefixed("/a/", settings) == "/s/a/"


# ---------------------------------------------------------------------------
# next= handling and redirects
# ---------------------------------------------------------------------------


def test_next_must_stay_inside_the_prefix(prefix: str) -> None:
    assert safe_next_path(f"{prefix}/projects/") == f"{prefix}/projects/"
    assert safe_next_path(f"{prefix}/search/?q=a%2Fb#top") == f"{prefix}/search/?q=a%2Fb#top"
    # Outside the prefix: not this application, refused like another origin.
    assert safe_next_path("/projects/") == ""
    assert safe_next_path(f"{prefix}-other/") == ""
    # The open-redirect protection still holds.
    for bad in ("//evil.example/", "/\\evil.example", "https://evil.example/", f"{prefix}/\r\nX: y"):
        assert safe_next_path(bad) == ""


def test_next_without_a_prefix_is_unchanged(no_prefix: None) -> None:
    assert safe_next_path("/projects/") == "/projects/"
    assert safe_next_path("//evil.example/") == ""


def test_login_url_keeps_the_prefix(prefix: str) -> None:
    assert login_url_for(f"{prefix}/projects/ACME/") == f"{prefix}/login/?next=%2Fhidden-ui%2Fprojects%2FACME%2F"
    assert login_url_for("/projects/ACME/") == f"{prefix}/login/"
    assert login_url_for("https://evil.example/") == f"{prefix}/login/"


def test_login_url_without_a_prefix(no_prefix: None) -> None:
    assert login_url_for("/projects/") == "/login/?next=%2Fprojects%2F"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/my/profile/", f"{PREFIX}/my/profile/"),
        (f"{PREFIX}/projects/?x=1", f"{PREFIX}/projects/?x=1"),
        ("https://idp.example/authorize?x=1", "https://idp.example/authorize?x=1"),
        ("?local=1", "?local=1"),
    ],
)
def test_redirect_to_prefixes_application_paths(prefix: str, target: str, expected: str) -> None:
    response = redirect_to(target, 303)
    assert response.status_code == 303
    assert response.headers["location"] == expected


def test_redirect_to_without_a_prefix(no_prefix: None) -> None:
    assert redirect_to("/my/profile/").headers["location"] == "/my/profile/"


# ---------------------------------------------------------------------------
# Templates: the base global and the prefixed filter
# ---------------------------------------------------------------------------

_TEMPLATE = (
    '<a href="{{ base }}/projects/">p</a>'
    "[{{ base ~ '/search/' }}]"
    "[{{ '/data/avatars/a.webp' | prefixed }}]"
    "[{{ 'https://x.example/a' | prefixed }}]"
    "[{{ none_value | prefixed }}]"
    "[{% if base %}set{% else %}unset{% endif %}]"
)


def test_template_global_without_a_prefix(no_prefix: None) -> None:
    html = get_templates().env.from_string(_TEMPLATE).render(none_value=None)
    assert html == '<a href="/projects/">p</a>[/search/][/data/avatars/a.webp][https://x.example/a][None][unset]'


def test_template_global_with_a_prefix(prefix: str) -> None:
    html = get_templates().env.from_string(_TEMPLATE).render(none_value=None)
    assert html == (
        f'<a href="{prefix}/projects/">p</a>[{prefix}/search/][{prefix}/data/avatars/a.webp]'
        "[https://x.example/a][None][set]"
    )


def test_template_global_reads_the_setting_on_every_render(monkeypatch: pytest.MonkeyPatch) -> None:
    template = get_templates().env.from_string("{{ base }}")
    monkeypatch.setattr(get_settings(), "stealth_prefix", "")
    assert template.render() == ""
    monkeypatch.setattr(get_settings(), "stealth_prefix", "/later")
    assert template.render() == "/later"


# ---------------------------------------------------------------------------
# Rendered Markdown
# ---------------------------------------------------------------------------

_MARKDOWN = (
    "See ACME-1, [[Home Page]], ![chart](chart.png), [typed](/issue/ACME-2/), "
    '[copied](/hidden-ui/issue/ACME-3/), [out](https://x.example/a) and <a href="/projects/ACME/">raw</a>.'
)
_ATTACHMENTS = {"chart.png": "/api/v1/attachments/7/download/"}


def test_markdown_links_without_a_prefix(no_prefix: None) -> None:
    html = str(render_wiki_markdown(_MARKDOWN, "ACME", _ATTACHMENTS))
    assert 'href="/issue/ACME-1/"' in html
    assert 'href="/projects/ACME/wiki/home-page/"' in html
    assert 'src="/api/v1/attachments/7/download/"' in html
    assert 'href="/issue/ACME-2/"' in html
    assert 'href="https://x.example/a"' in html
    assert 'href="/projects/ACME/"' in html


def test_markdown_links_get_the_prefix(prefix: str) -> None:
    html = str(render_wiki_markdown(_MARKDOWN, "ACME", _ATTACHMENTS))
    assert f'href="{prefix}/issue/ACME-1/"' in html
    assert f'href="{prefix}/projects/ACME/wiki/home-page/"' in html
    assert f'src="{prefix}/api/v1/attachments/7/download/"' in html
    assert f'href="{prefix}/issue/ACME-2/"' in html
    # Copied from the address bar: already prefixed, not prefixed twice.
    assert f'href="{prefix}/issue/ACME-3/"' in html
    assert f"{prefix}{prefix}" not in html
    assert 'href="https://x.example/a"' in html
    assert f'href="{prefix}/projects/ACME/"' in html


def test_link_classification_reads_prefixed_paths(prefix: str) -> None:
    found = classify_link_target(f"{prefix}/issue/ACME-3/")
    assert found is not None and (found.kind, found.target) == ("issue", "ACME-3")
    wiki = classify_link_target(f"https://t.example{prefix}/projects/ACME/wiki/home/", "https://t.example")
    assert wiki is not None and (wiki.kind, wiki.target, wiki.project_key) == ("wiki", "home", "ACME")
    plain = classify_link_target("/issue/ACME-4/")
    assert plain is not None and plain.target == "ACME-4"


# ---------------------------------------------------------------------------
# spUrl() in frontend/js/lib/globals.js, run under Node
# ---------------------------------------------------------------------------

_SPURL_CASES = [
    "/projects/ACME/",
    "/",
    "/hidden-ui",
    "/hidden-ui/projects/",
    "/hidden-ui?x=1",
    "/hidden-ui-other/",
    "//evil.example/",
    "https://x.example/a",
    "?page=2",
    "relative/path",
]


def _run_sp_url(base: str) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    script = f"""
        global.window = global;
        global.document = {{
            cookie: '',
            querySelector: (sel) => sel === 'meta[name="sp-base"]'
                ? {{ getAttribute: () => {json.dumps(base)} }} : null,
        }};
        require({json.dumps(str(GLOBALS_JS))});
        const out = {{}};
        for (const c of {json.dumps(_SPURL_CASES)}) out[c] = window.spUrl(c);
        out.__null__ = String(window.spUrl(null));
        console.log(JSON.stringify(out));
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_sp_url_without_a_prefix() -> None:
    out = _run_sp_url("")
    assert all(out[c] == c for c in _SPURL_CASES)
    assert out["__null__"] == "null"


def test_sp_url_with_a_prefix() -> None:
    out = _run_sp_url(PREFIX)
    assert out["/projects/ACME/"] == f"{PREFIX}/projects/ACME/"
    assert out["/"] == f"{PREFIX}/"
    assert out["/hidden-ui"] == "/hidden-ui"
    assert out["/hidden-ui/projects/"] == "/hidden-ui/projects/"
    assert out["/hidden-ui?x=1"] == "/hidden-ui?x=1"
    assert out["/hidden-ui-other/"] == f"{PREFIX}/hidden-ui-other/"
    for unchanged in ("//evil.example/", "https://x.example/a", "?page=2", "relative/path"):
        assert out[unchanged] == unchanged
