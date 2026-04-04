"""Stealth mode tests — robots.txt, X-Robots-Tag header, URL prefix."""

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_robots_txt_disallows_all(unauth_client: AsyncClient):
    """robots.txt returns Disallow: / for all user agents."""
    response = await unauth_client.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "User-agent: *" in body
    assert "Disallow: /" in body


@pytest.mark.integration
async def test_x_robots_tag_header_present(unauth_client: AsyncClient):
    """Every response includes X-Robots-Tag: noindex, nofollow, noarchive."""
    response = await unauth_client.get("/health/")
    assert response.headers.get("x-robots-tag") == "noindex, nofollow, noarchive"


@pytest.mark.integration
async def test_x_robots_tag_on_robots_txt(unauth_client: AsyncClient):
    """Even robots.txt itself carries the noindex header."""
    response = await unauth_client.get("/robots.txt")
    assert response.headers.get("x-robots-tag") == "noindex, nofollow, noarchive"
