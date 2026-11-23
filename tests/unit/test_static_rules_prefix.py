"""The stealth-prefix rules of ``scripts/check_static_rules.py``.

The checks run on every template and JS file in ``make lint`` and CI; these
tests pin what they catch and what they let through, on small samples.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_static_rules.py"
_spec = importlib.util.spec_from_file_location("check_static_rules", _SCRIPT)
assert _spec is not None and _spec.loader is not None
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)


def _template(text: str, name: str = "themes/default/pages/sample.html") -> list[str]:
    return rules.template_url_problems(name, text)


@pytest.mark.parametrize(
    "text",
    [
        '<a href="/projects/">x</a>',
        '<a href="/">home</a>',
        '<form action="/admin/roles/">',
        '<div hx-get="/partials/x/"></div>',
        "<img src='/static/a.png'>",
        "<a :href=\"'/issue/' + key\">",
        "<div x-data='f({ endpoint: \"/api/v1/x/\" })'>",
        "{{ tag_badge(t.name, '', '/search/?tag=' ~ t.name) }}",
        '{% set crumbs = [{"label": "Home", "url": "/"}] %}',
        '<a href="{{ some_url }}">',
        '<a :href="issue.url">',
        "<div style=\"background:url('/static/x.png')\">",
    ],
)
def test_template_rule_flags(text: str) -> None:
    assert _template(text), text


@pytest.mark.parametrize(
    "text",
    [
        '<a href="{{ base }}/projects/">x</a>',
        '<a href="{{ base }}/">home</a>',
        "<a :href=\"'{{ base }}/issue/' + key + '/edit/'\">",
        "<a :href=\"spUrl('/issue/' + key)\">",
        "<script>spFetch('/api/v1/x/')</script>",
        "{{ tag_badge(t.name, '', base ~ '/search/?tag=' ~ t.name) }}",
        '{% set crumbs = [{"label": "Home", "url": base ~ "/"}] %}',
        '<a href="{{ some_url | prefixed }}">',
        '<a href="{{ base }}{{ path }}">',
        '<a href="https://specivo.io/">',
        '<a href="#top">',
        '{# Usage: href="/projects/" #}',
        "{{ path.split('/') }}",
    ],
)
def test_template_rule_passes(text: str) -> None:
    assert _template(text) == [], text


def test_template_allow_list_is_per_template() -> None:
    allowed = '<a href="{{ canonical_url }}">'
    assert _template(allowed, "themes/default/base.html") == []
    assert _template(allowed, "themes/default/pages/other.html")


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("location.href = '/issue/' + key;", True),
        ('var u = "/api/v1/x/";', True),
        ("var u = `/projects/${k}/`;", True),
        ("location.href = spUrl('/issue/' + key);", False),
        ("spFetch(\n    '/api/v1/x/' + k +\n    '/y/'\n);", False),
        ("// see '/api/v1/x/'", False),
        ("/* '/api/v1/x/' */", False),
        ("var r = /^\\/static\\//;", False),
        ("var parts = path.split('/');", False),
    ],
)
def test_js_rule(source: str, flagged: bool) -> None:
    assert bool(rules.js_url_problems("frontend/js/sample.js", source)) is flagged


def test_js_exceptions_are_per_file() -> None:
    source = "return '/api/v1/attachments/' + att.id + '/download/';"
    assert rules.js_url_problems("frontend/js/components/attachments.js", source) == []
    assert rules.js_url_problems("frontend/js/components/other.js", source)


def test_the_tree_passes() -> None:
    assert rules.check_template_urls_carry_the_prefix() == []
    assert rules.check_js_urls_carry_the_prefix() == []
    assert rules.check_css_urls_are_relative() == []
