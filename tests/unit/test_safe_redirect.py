"""Same-origin redirect checks: ``safe_next_path`` and ``frontend/js/lib/safe-url.js``.

Both sides answer the same table, so a ``next`` the server would refuse is
refused by the browser code too. The JS half runs the module under Node and is
skipped when Node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from specivo.web.deps import safe_next_path

pytestmark = pytest.mark.unit

SAFE_URL_JS = Path(__file__).resolve().parents[2] / "frontend" / "js" / "lib" / "safe-url.js"
ORIGIN = "https://tracker.example.org"

ACCEPTED = [
    "/",
    "/projects/ACME/",
    "/projects/ACME/issues/?tab=open&x=1",
    "/search/?q=a%2Fb#top",
    "/wiki/page:with:colons",
]
REFUSED = [
    "",
    "projects/ACME/",
    "https://evil.example/",
    "javascript:alert(1)",
    "//evil.example/",
    "/\\evil.example",
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r\nSet-Cookie: x=1",
    "/a\x00b",
    "/a\x7fb",
    "/" + "a" * 2000,
]


@pytest.mark.parametrize("value", ACCEPTED)
def test_safe_next_path_keeps_a_local_path(value):
    assert safe_next_path(value) == value


@pytest.mark.parametrize("value", REFUSED)
def test_safe_next_path_refuses_anything_else(value):
    assert safe_next_path(value) == ""


def test_safe_next_path_refuses_none():
    assert safe_next_path(None) == ""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _run_js(calls: list[list[object]]) -> list[object]:
    """Call exports of safe-url.js under Node; each call is ``[name, *args]``."""
    script = (
        "const m = await import(process.argv[1]);"
        "const calls = JSON.parse(process.argv[2]);"
        "process.stdout.write(JSON.stringify(calls.map(([name, ...args]) => m[name](...args))));"
    )
    result = subprocess.run(  # noqa: S603 - fixed script, arguments passed as data
        [_node(), "--input-type=module", "-e", script, SAFE_URL_JS.as_uri(), json.dumps(calls)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def test_js_safe_next_path_matches_the_server():
    values = ACCEPTED + REFUSED
    results = _run_js([["safeNextPath", value] for value in values])
    assert results == [safe_next_path(value) for value in values]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10", 10), ("50", 50), (25, 25), ("0", None), ("-5", None), ("1e3", None), ("10&x=1", None), ("", None)],
)
def test_js_page_size(value, expected):
    assert _run_js([["pageSize", value]]) == [expected]


@pytest.mark.parametrize(
    ("base", "params", "expected"),
    [
        ("/projects/ACME/issues", {"offset": 0, "limit": 25}, "/projects/ACME/issues?offset=0&limit=25"),
        (
            "/search/?q=bug&mode=hybrid&offset=50&limit=10",
            {"offset": 0, "limit": 25},
            "/search/?q=bug&mode=hybrid&offset=0&limit=25",
        ),
        (
            f"{ORIGIN}/projects/ACME/board/?board_per_col=10&sprint=3",
            {"board_per_col": 50},
            "/projects/ACME/board/?board_per_col=50&sprint=3",
        ),
        ("https://evil.example/x", {"limit": 10}, None),
        ("//evil.example/x", {"limit": 10}, None),
        ("/\\evil.example/x", {"limit": 10}, None),
        ("javascript:alert(1)//", {"limit": 10}, None),
        ("/\t/evil.example/x", {"limit": 10}, None),
    ],
)
def test_js_same_origin_url(base, params, expected):
    assert _run_js([["sameOriginUrl", base, params, ORIGIN]]) == [expected]
