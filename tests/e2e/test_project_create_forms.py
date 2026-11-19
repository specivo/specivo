"""The two project forms: the New Project modal on the projects list and the admin projects page."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project, unique_key, wait_for_alpine

pytestmark = [pytest.mark.e2e]


def _modules(api_client: httpx.Client, key: str) -> dict[str, bool]:
    resp = api_client.get(f"/api/v1/projects/{key}/modules/")
    assert resp.status_code == 200, resp.text
    return resp.json()["modules"]


def _open_modal(page: Page) -> None:
    page.goto("/projects/")
    wait_for_alpine(page)
    page.get_by_role("button", name="New Project").click()


def test_new_project_modal_creates_the_project(admin_page: Page, api_client: httpx.Client) -> None:
    key = unique_key("PCM")
    _open_modal(admin_page)
    modal = admin_page.locator(".sp-modal", has=admin_page.locator("form"))
    modal.locator("input[x-model=name]").fill(f"Modal {key}")
    modal.locator("input[x-model=identifier]").fill(key.lower())
    modal.locator("input[x-model=key]").fill(key)
    modal.locator("textarea[x-model=description]").fill("Made through the modal")
    modal.locator("input[x-model=moduleTime]").uncheck()
    modal.get_by_role("button", name="Create Project").click()

    admin_page.wait_for_url(f"**/projects/{key}/")
    project = api_client.get(f"/api/v1/projects/{key}/").json()
    assert (project["name"], project["description"]) == (f"Modal {key}", "Made through the modal")
    modules = _modules(api_client, key)
    assert modules["issue_tracking"] and modules["wiki"] and not modules["time_tracking"]


def test_new_project_modal_shows_the_server_error(admin_page: Page, api_client: httpx.Client) -> None:
    taken = create_project(api_client, prefix="PCM")
    _open_modal(admin_page)
    modal = admin_page.locator(".sp-modal", has=admin_page.locator("form"))
    modal.locator("input[x-model=name]").fill("Duplicate key")
    modal.locator("input[x-model=identifier]").fill(unique_key("PCM").lower())
    modal.locator("input[x-model=key]").fill(taken["key"])
    modal.get_by_role("button", name="Create Project").click()

    expect(modal.locator(".flash-error")).to_be_visible()
    expect(modal.locator(".flash-error")).not_to_have_text("")
    assert admin_page.url.endswith("/projects/")


def _admin_modal(page: Page):
    return page.locator(".sp-modal", has=page.locator("form"))


def test_admin_projects_create_then_edit(admin_page: Page, api_client: httpx.Client) -> None:
    key = unique_key("APF")
    admin_page.goto("/admin/projects/")
    wait_for_alpine(admin_page)
    admin_page.get_by_role("button", name="New Project").click()
    modal = _admin_modal(admin_page)
    modal.locator("input[x-model='form.name']").fill(f"Admin {key}")
    modal.locator("input[x-model='form.identifier']").fill(key.lower())
    modal.locator("input[x-model='form.key']").fill(key)
    modal.locator("input[x-model='form.moduleWiki']").uncheck()
    modal.locator("button[type=submit]").click()

    row = admin_page.locator("tr", has_text=f"Admin {key}")
    expect(row).to_be_visible()
    modules = _modules(api_client, key)
    assert modules["issue_tracking"] and not modules["wiki"] and modules["time_tracking"]

    wait_for_alpine(admin_page)
    row.get_by_role("button", name="Edit").click()
    modal = _admin_modal(admin_page)
    expect(modal.locator("input[x-model='form.key']")).to_be_disabled()
    modal.locator("input[x-model='form.name']").fill(f"Renamed {key}")
    modal.locator("textarea[x-model='form.description']").fill("Edited by the admin form")
    modal.locator("button[type=submit]").click()

    expect(admin_page.locator("tr", has_text=f"Renamed {key}")).to_be_visible()
    project = api_client.get(f"/api/v1/projects/{key}/").json()
    assert (project["name"], project["description"]) == (f"Renamed {key}", "Edited by the admin form")


def test_admin_projects_form_shows_the_server_error(admin_page: Page, api_client: httpx.Client) -> None:
    taken = create_project(api_client, prefix="APF")
    admin_page.goto("/admin/projects/")
    wait_for_alpine(admin_page)
    admin_page.get_by_role("button", name="New Project").click()
    modal = _admin_modal(admin_page)
    modal.locator("input[x-model='form.name']").fill("Duplicate key")
    modal.locator("input[x-model='form.identifier']").fill(unique_key("APF").lower())
    modal.locator("input[x-model='form.key']").fill(taken["key"])
    modal.locator("button[type=submit]").click()

    expect(modal.locator(".flash-error")).to_be_visible()
    expect(modal.locator(".flash-error")).not_to_have_text("")
