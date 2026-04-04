"""E2E tests for the brand_name setting.

Verifies the brand name appears in the sidebar, login page, and page
titles — and updates everywhere when changed via the admin settings API.
"""

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]


def test_default_brand_name_in_sidebar(auth_page: Page) -> None:
    """Sidebar shows the default brand name 'Specivo'."""
    auth_page.goto("/")
    expect(auth_page.locator(".sidebar-brand span")).to_have_text("Specivo")


def test_default_brand_name_in_title(auth_page: Page) -> None:
    """Page title contains the default brand name."""
    auth_page.goto("/")
    expect(auth_page).to_have_title("Dashboard - Specivo")


def test_default_brand_name_on_login_page(page: Page, e2e_server: str) -> None:
    """Login page heading shows the default brand name."""
    page.goto(f"{e2e_server}/login/")
    expect(page.locator(".login-brand h1")).to_have_text("Specivo")


def test_brand_name_updates_in_sidebar(admin_page: Page, api_client: httpx.Client) -> None:
    """After changing brand_name via API, sidebar reflects the new name."""
    api_client.patch(
        "/api/v1/admin/settings/",
        json={"brand_name": "Acme Corp"},
    )
    try:
        admin_page.goto("/")
        expect(admin_page.locator(".sidebar-brand span")).to_have_text("Acme Corp")
    finally:
        api_client.patch("/api/v1/admin/settings/", json={"brand_name": "Specivo"})


def test_brand_name_updates_in_title(admin_page: Page, api_client: httpx.Client) -> None:
    """After changing brand_name via API, page title reflects the new name."""
    api_client.patch(
        "/api/v1/admin/settings/",
        json={"brand_name": "Acme Corp"},
    )
    try:
        admin_page.goto("/")
        expect(admin_page).to_have_title("Dashboard - Acme Corp")
    finally:
        api_client.patch("/api/v1/admin/settings/", json={"brand_name": "Specivo"})


def test_brand_name_updates_on_login_page(api_client: httpx.Client, page: Page, e2e_server: str) -> None:
    """After changing brand_name via API, login page shows the new name."""
    api_client.patch(
        "/api/v1/admin/settings/",
        json={"brand_name": "Acme Corp"},
    )
    try:
        page.goto(f"{e2e_server}/login/")
        expect(page.locator(".login-brand h1")).to_have_text("Acme Corp")
        expect(page).to_have_title("Sign in - Acme Corp")
    finally:
        api_client.patch("/api/v1/admin/settings/", json={"brand_name": "Specivo"})


def test_empty_brand_name_falls_back_to_specivo(admin_page: Page, api_client: httpx.Client) -> None:
    """Setting brand_name to null/empty falls back to 'Specivo'."""
    api_client.patch(
        "/api/v1/admin/settings/",
        json={"brand_name": None},
    )
    try:
        admin_page.goto("/")
        expect(admin_page.locator(".sidebar-brand span")).to_have_text("Specivo")
    finally:
        api_client.patch("/api/v1/admin/settings/", json={"brand_name": "Specivo"})
