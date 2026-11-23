"""The last-resort error page escapes the text it is given."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.requests import Request

from specivo.core.exceptions import _render_error_html

pytestmark = pytest.mark.unit


def test_the_last_resort_error_page_escapes_its_text() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})
    with patch("specivo.web.deps.get_templates", side_effect=RuntimeError("templates broken")):
        resp = _render_error_html(request, 400, title="<b>T</b>", message="<img src=x onerror=alert(1)>")
    body = resp.body.decode()
    assert "<img" not in body and "<b>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
