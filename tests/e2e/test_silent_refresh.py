"""E2E test for silent JWT refresh.

Starts a server with a very short access token lifetime (5 seconds)
to verify that silent refresh via the refresh_token cookie keeps
the user logged in after the access token expires.
"""

from __future__ import annotations

import os
import subprocess
import time

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

from specivo.testing.e2e_base import E2E_SERVER_HOST

pytestmark = [pytest.mark.e2e]

# Use a different port so we don't conflict with the normal E2E server
_REFRESH_TEST_PORT = 9945
_REFRESH_BASE_URL = f"http://{E2E_SERVER_HOST}:{_REFRESH_TEST_PORT}"

# Short access token lifetime for testing (1 minute — minimum int value)
_ACCESS_TOKEN_MINUTES = 1


@pytest.fixture(scope="module")
def refresh_server(e2e_seed_data):
    """Start a uvicorn server with a 5-second access token lifetime."""
    subprocess.run(f"lsof -ti:{_REFRESH_TEST_PORT} | xargs kill -9", shell=True, capture_output=True)
    time.sleep(0.5)

    env = {
        **os.environ,
        "ACCESS_TOKEN_EXPIRE_MINUTES": str(_ACCESS_TOKEN_MINUTES),
    }

    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "specivo.main:app",
            "--host",
            E2E_SERVER_HOST,
            "--port",
            str(_REFRESH_TEST_PORT),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{_REFRESH_BASE_URL}/health/", timeout=3)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, OSError):
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail(f"Refresh test server did not start on port {_REFRESH_TEST_PORT}")

    yield _REFRESH_BASE_URL

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_silent_refresh_keeps_user_logged_in(browser, refresh_server: str, e2e_seed_data: dict) -> None:
    """After access token expires, navigating to a page still works via silent refresh.

    1. Log in via the browser (sets both access_token and refresh_token cookies)
    2. Wait for the access_token to expire (5 seconds)
    3. Navigate to the dashboard
    4. Expect 200 (not redirect to /login/) — silent refresh kicked in
    """
    context: BrowserContext = browser.new_context(base_url=refresh_server)
    page: Page = context.new_page()

    try:
        # Log in via the login page
        page.goto("/login/")
        page.locator("input[type='checkbox']").check()  # Remember me for persistent cookies
        page.locator("#login-field").fill(e2e_seed_data["user_login"])
        page.locator("#password-field").fill(e2e_seed_data["password"])
        page.locator("button.sp-btn-login").click()
        page.wait_for_url("**/", timeout=5000)
        expect(page).to_have_title("Dashboard - Specivo")

        # Wait for the access token to expire (1 min + 5s buffer)
        time.sleep(_ACCESS_TOKEN_MINUTES * 60 + 5)

        # Navigate to a protected page — should NOT redirect to /login/
        response = page.goto("/")
        # Debug: check what we got
        final_url = page.url
        title = page.title()
        assert "/login" not in final_url, (
            f"Silent refresh failed — redirected to {final_url} (title: {title}). "
            f"Response status: {response.status if response else 'N/A'}"
        )
        expect(page).to_have_title("Dashboard - Specivo", timeout=10000)
    finally:
        context.close()


def test_expired_token_without_remember_me_redirects(browser, refresh_server: str, e2e_seed_data: dict) -> None:
    """Without Remember Me, after access token expires AND browser 'restart'
    (new context), user should be redirected to login.

    This simulates closing and reopening the browser — session cookies are gone.
    """
    # First: log in without Remember Me
    context1: BrowserContext = browser.new_context(base_url=refresh_server)
    page1: Page = context1.new_page()

    try:
        page1.goto("/login/")
        # Do NOT check Remember me
        page1.locator("#login-field").fill(e2e_seed_data["user_login"])
        page1.locator("#password-field").fill(e2e_seed_data["password"])
        page1.locator("button.sp-btn-login").click()
        page1.wait_for_url("**/", timeout=5000)
    finally:
        context1.close()

    # Simulate browser restart: new context has no cookies
    context2: BrowserContext = browser.new_context(base_url=refresh_server)
    page2: Page = context2.new_page()

    try:
        page2.goto("/")
        # Should redirect to login since session cookies are gone
        page2.wait_for_url("**/login/", timeout=5000)
        expect(page2.locator("#login-field")).to_be_visible()
    finally:
        context2.close()
