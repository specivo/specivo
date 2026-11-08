"""An API key is limited to projects through the create forms, and the key lists show the limit."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project, unique_key, wait_for_alpine

pytestmark = [pytest.mark.e2e]


def _tick(page: Page, project_key: str) -> None:
    """Tick *project_key* in the picker, narrowing the list with the filter box when it is shown."""
    picker = page.locator(".sp-key-limit")
    expect(picker.locator(".sp-key-limit-option").first).to_be_visible()
    search = picker.locator(".sp-key-limit-filter")
    if search.is_visible():
        search.fill(project_key)
    picker.locator(".sp-key-limit-option", has_text=project_key).locator("input[type=checkbox]").check()


def test_own_key_limited_through_the_form(admin_page: Page, api_client: httpx.Client) -> None:
    first = create_project(api_client, prefix="AKL")
    second = create_project(api_client, prefix="AKL")
    name = f"limited {unique_key()}"

    admin_page.goto("/my/api-keys/")
    wait_for_alpine(admin_page)
    admin_page.get_by_role("button", name="New API Key").click()
    expect(admin_page.locator(".sp-key-limit-summary")).to_contain_text("Nothing ticked")
    admin_page.locator("#key-name").fill(name)
    _tick(admin_page, first["key"])
    _tick(admin_page, second["key"])
    expect(admin_page.locator(".sp-key-limit-summary")).to_contain_text(first["key"])
    admin_page.locator(".card form button[type=submit]").click()

    expect(admin_page.locator(".flash-success")).to_be_visible()
    row = admin_page.locator("tr", has_text=name)
    expected = ", ".join(sorted([first["key"], second["key"]]))
    expect(row.locator(".sp-key-limit-cell")).to_have_text(f"Limited to: {expected}")

    keys = {k["name"]: k for k in api_client.get("/api/v1/my/api-keys/").json()}
    assert keys[name]["scopes"] == {"projects": sorted([first["id"], second["id"]])}
    api_client.delete(f"/api/v1/my/api-keys/{keys[name]['id']}/")


def test_unticked_key_reaches_all_projects(admin_page: Page, api_client: httpx.Client) -> None:
    name = f"open {unique_key()}"

    admin_page.goto("/my/api-keys/")
    wait_for_alpine(admin_page)
    admin_page.get_by_role("button", name="New API Key").click()
    admin_page.locator("#key-name").fill(name)
    admin_page.locator(".card form button[type=submit]").click()

    expect(admin_page.locator(".flash-success")).to_be_visible()
    expect(admin_page.locator("tr", has_text=name).locator(".sp-key-limit-cell")).to_have_text("All projects")
    keys = {k["name"]: k for k in api_client.get("/api/v1/my/api-keys/").json()}
    assert keys[name]["project_keys"] is None
    api_client.delete(f"/api/v1/my/api-keys/{keys[name]['id']}/")


def test_service_account_key_limited_through_the_admin_form(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="AKS")
    login = f"akl-agent-{unique_key().lower()}"
    resp = api_client.post(
        "/api/v1/admin/users/",
        json={"login": login, "email": f"{login}@example.com", "display_name": login, "is_service_account": True},
    )
    assert resp.status_code == 201, resp.text
    agent = resp.json()

    admin_page.goto(f"/admin/users/{agent['id']}/")
    wait_for_alpine(admin_page)
    admin_page.locator("#admin-key-name").fill("agent key")
    _tick(admin_page, project["key"])
    admin_page.locator("form:has(#admin-key-name) button[type=submit]").click()

    expect(admin_page.locator(".sp-key-reveal")).to_be_visible()
    row = admin_page.locator("tr", has_text="agent key")
    expect(row.locator(".sp-key-limit-cell")).to_have_text(f"Limited to: {project['key']}")
    listed = api_client.get(f"/api/v1/admin/users/{agent['id']}/api-keys/").json()
    assert [k["project_keys"] for k in listed] == [[project["key"]]]
