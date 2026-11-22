"""E2E tests for wiki pages."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import api_post_with_retry, create_project, unique_key
from tests.e2e.pages.wiki_page import WikiShowPage

pytestmark = [pytest.mark.e2e]


def _create_project(api: httpx.Client) -> dict:
    project = create_project(api)
    # Enable wiki module
    api.patch(
        f"/api/v1/projects/{project['key']}/modules/",
        json={"modules": {"wiki": True}},
    )
    return project


def _create_wiki_page(api: httpx.Client, project_key: str, title: str, text: str = "Test content") -> dict:
    resp = api_post_with_retry(
        api,
        f"/api/v1/projects/{project_key}/wiki/",
        json={"title": title, "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_wiki_index_redirects_to_home(admin_page: Page, api_client: httpx.Client) -> None:
    """Wiki index /wiki/ redirects to /wiki/home/ and auto-creates home page."""
    project = _create_project(api_client)
    admin_page.goto(f"/projects/{project['key']}/wiki/")
    admin_page.wait_for_url(f"**/projects/{project['key']}/wiki/home/", timeout=5000)
    expect(admin_page.locator("h1", has_text="Home")).to_be_visible()


def test_wiki_home_page_renders(admin_page: Page, api_client: httpx.Client) -> None:
    """Auto-created home page renders with title 'Home'."""
    project = _create_project(api_client)
    admin_page.goto(f"/projects/{project['key']}/wiki/home/")
    admin_page.wait_for_load_state("networkidle", timeout=10000)
    expect(admin_page.locator("h1", has_text="Home")).to_be_visible(timeout=10000)


def test_wiki_all_pages_shows_created_page(admin_page: Page, api_client: httpx.Client) -> None:
    """A wiki page created via API appears in the All pages view."""
    project = _create_project(api_client)
    title = f"E2E Wiki {unique_key()}"
    _create_wiki_page(api_client, project["key"], title)

    admin_page.goto(f"/projects/{project['key']}/wiki/pages/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(f"text={title}")).to_be_visible()


def test_wiki_page_view(admin_page: Page, api_client: httpx.Client) -> None:
    """Wiki page detail shows the page title and content."""
    project = _create_project(api_client)
    title = f"E2E Page {unique_key()}"
    page_data = _create_wiki_page(api_client, project["key"], title, text="Hello **world**")

    show = WikiShowPage(admin_page)
    show.navigate(project["key"], page_data["slug"])
    show.expect_title(title)


def test_wiki_history_page(admin_page: Page, api_client: httpx.Client) -> None:
    """Wiki history page loads for a created page."""
    project = _create_project(api_client)
    title = f"E2E History {unique_key()}"
    page_data = _create_wiki_page(api_client, project["key"], title)

    admin_page.goto(f"/projects/{project['key']}/wiki/{page_data['slug']}/history/")
    expect(admin_page.locator("h1.history-page-title")).to_be_visible()


def test_wiki_delete_and_restore_from_trash(admin_page: Page, api_client: httpx.Client) -> None:
    """Delete a page through its confirm dialog, then restore it from the trash."""
    project = _create_project(api_client)
    title = f"E2E Delete {unique_key()}"
    page_data = _create_wiki_page(api_client, project["key"], title)

    admin_page.goto(f"/projects/{project['key']}/wiki/{page_data['slug']}/")
    dialog = admin_page.get_by_test_id("wiki-delete-dialog")
    expect(dialog).to_be_hidden()
    admin_page.get_by_test_id("wiki-delete-button").click()
    expect(dialog).to_be_visible()
    admin_page.get_by_test_id("wiki-delete-confirm").click()

    admin_page.wait_for_url(f"**/projects/{project['key']}/wiki/pages/?notice=deleted")
    expect(admin_page.get_by_test_id("wiki-notice")).to_be_visible()
    expect(admin_page.locator(f"text={title}")).to_have_count(0)

    admin_page.get_by_test_id("wiki-trash-link").click()
    admin_page.wait_for_url(f"**/projects/{project['key']}/wiki/trash/")
    row = admin_page.locator("tr", has_text=title)
    row.get_by_role("button", name="Restore").click()

    admin_page.wait_for_url(f"**/projects/{project['key']}/wiki/trash/?notice=restored")
    expect(admin_page.get_by_test_id("wiki-trash-notice")).to_be_visible()
    admin_page.goto(f"/projects/{project['key']}/wiki/{page_data['slug']}/")
    expect(admin_page.locator("h1", has_text=title)).to_be_visible()
