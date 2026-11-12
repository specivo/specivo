"""Security headers on every response, and header handling that cannot be pushed into a 500."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from starlette.requests import Request

from specivo.core.exceptions import _render_error_html

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


async def test_a_csrf_refusal_carries_the_security_headers(client: AsyncClient) -> None:
    """The CSRF middleware answers from inside the request-ID middleware now."""
    resp = await client.patch(
        "/api/v1/users/me/preferences/",
        json={"timezone": "UTC"},
        cookies={"access_token": "not-a-real-session"},
    )
    assert resp.status_code == 403
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" in resp.headers
    assert resp.headers["x-request-id"]


@pytest.mark.parametrize("header", [b"cookie", b"accept-language", b"x-csrf-token"])
async def test_non_utf8_header_bytes_are_not_a_500(unauth_client: AsyncClient, header: bytes) -> None:
    headers = [(header, b"specivo_lang=\xff\xfe; csrf_token=\xff")]
    resp = await unauth_client.get("/health/", headers=headers)
    assert resp.status_code == 200
    resp = await unauth_client.post("/api/v1/auth/logout/", headers=headers)
    assert resp.status_code != 500


async def test_a_plain_request_id_is_echoed(unauth_client: AsyncClient) -> None:
    resp = await unauth_client.get("/health/", headers={"X-Request-ID": "trace-01.ab_C"})
    assert resp.headers["x-request-id"] == "trace-01.ab_C"


@pytest.mark.parametrize("sent", ["a" * 65, "<script>", "two words", "line\\nbreak"])
async def test_an_odd_request_id_is_replaced(unauth_client: AsyncClient, sent: str) -> None:
    resp = await unauth_client.get("/health/", headers={"X-Request-ID": sent})
    echoed = resp.headers["x-request-id"]
    assert echoed != sent
    assert len(echoed) == 36  # a fresh UUID4


def test_the_last_resort_error_page_escapes_its_text() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})
    with patch("specivo.web.deps.get_templates", side_effect=RuntimeError("templates broken")):
        resp = _render_error_html(request, 400, title="<b>T</b>", message="<img src=x onerror=alert(1)>")
    body = resp.body.decode()
    assert "<img" not in body and "<b>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
