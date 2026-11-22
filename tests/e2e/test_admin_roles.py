"""E2E: an administrator builds a role in Administration > Roles, and a member gets its rights.

The role editor is an Alpine component that writes through the admin roles
API. This walks the whole path once: copy Developer on the New role form,
tick **Delete wiki pages** on the role page (its prerequisite, **Create and
edit wiki pages**, is already on), save, give the role to a project member,
and check that the member could not delete a wiki page before and can after.

Wiki pages have no delete button in the web UI yet; deleting goes through the
REST API (and MCP). The member's side is therefore checked through the API
with the member's own token, and in the browser through the wiki trash,
which only people with **Delete wiki pages** may open.

E2E rows persist between tests, so the test removes the project (with its
memberships and wiki) and the role it created.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Generator

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

from specivo.testing.e2e_base import create_project, wait_for_alpine
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def member_api(e2e_server: str, _user_auth: tuple[str, list[dict]]) -> Generator[httpx.Client, None, None]:
    """The regular (non-admin) E2E user, for API calls made as the member."""
    token, _ = _user_auth
    client = httpx.Client(base_url=e2e_server, timeout=10)
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    client.close()


@pytest.fixture
def member_page(browser, e2e_server: str, _user_auth: tuple[str, list[dict]]) -> Generator[Page, None, None]:
    _, cookies = _user_auth
    context: BrowserContext = browser.new_context(base_url=e2e_server)
    context.add_cookies(cookies)
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def cleanup(api_client: httpx.Client) -> Generator[dict, None, None]:
    """Collect what the test creates and delete it afterwards: the project first, then the role."""
    created: dict = {"project_key": None, "role_id": None}
    yield created
    if created["project_key"]:
        api_client.delete(f"/api/v1/projects/{created['project_key']}/")
    if created["role_id"]:
        api_client.delete(f"/api/v1/admin/roles/{created['role_id']}/")


def _role_id(api: httpx.Client, name: str) -> int:
    resp = api.get("/api/v1/admin/roles/", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    matches = [r["id"] for r in resp.json()["items"] if r["name"] == name]
    assert matches, f"no role named {name!r}"
    return int(matches[0])


def _user_id(api: httpx.Client, login: str) -> int:
    resp = api.get("/api/v1/users/autocomplete/", params={"q": login})
    assert resp.status_code == 200, resp.text
    matches = [u["id"] for u in resp.json() if u["login"] == login]
    assert matches, f"no user {login!r}"
    return int(matches[0])


def _create_wiki_page(api: httpx.Client, project_key: str, title: str) -> str:
    resp = api.post(f"/api/v1/projects/{project_key}/wiki/", json={"title": title, "text": "Release checklist."})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["slug"])


def test_copied_role_with_delete_wiki_pages_lets_a_member_delete_a_page(
    admin_page: Page,
    member_page: Page,
    api_client: httpx.Client,
    member_api: httpx.Client,
    e2e_seed_data: dict,
    cleanup: dict,
) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    role_name = f"Wiki editor {uuid.uuid4().hex[:6]}"

    # A private project with the wiki module (on by default), two pages,
    # and the regular user as a Developer.
    project_key = create_project(api_client)["key"]
    cleanup["project_key"] = project_key
    first_slug = _create_wiki_page(api_client, project_key, "Release checklist")
    second_title = f"Old notes {uuid.uuid4().hex[:4]}"
    second_slug = _create_wiki_page(api_client, project_key, second_title)
    user_id = _user_id(api_client, e2e_seed_data["user_login"])
    resp = api_client.post(
        f"/api/v1/projects/{project_key}/members/",
        json={"user_id": user_id, "role_ids": [_role_id(api_client, "Developer")]},
    )
    assert resp.status_code == 201, resp.text

    # As a Developer the member may edit the wiki but not delete from it.
    assert member_api.delete(f"/api/v1/projects/{project_key}/wiki/{first_slug}/").status_code == 403
    trash = member_page.goto(f"/projects/{project_key}/wiki/trash/")
    assert trash is not None
    assert trash.status == 403

    # New role: a copy of Developer.
    admin_page.goto("/admin/roles/new/")
    wait_for_alpine(admin_page)
    admin_page.locator("#sp-role-name").fill(role_name)
    admin_page.locator("#sp-role-source").select_option(label="A copy of Developer")
    expect(admin_page.get_by_text("The copy gets the permissions")).to_be_visible()
    admin_page.get_by_role("button", name="Create role").click()

    admin_page.wait_for_url(re.compile(r"/admin/roles/\d+/$"))
    match = re.search(r"/admin/roles/(\d+)/$", admin_page.url)
    assert match is not None
    role_id = int(match.group(1))
    cleanup["role_id"] = role_id
    wait_for_alpine(admin_page)
    expect(admin_page.locator("h1")).to_have_text(role_name)
    # The copy took Developer's workflow transitions, so there is no warning about them.
    expect(admin_page.get_by_text("This role can't change issue status yet.")).to_have_count(0)

    manage_wiki = admin_page.locator('input[name="permission"][value="manage_wiki"]')
    delete_wiki = admin_page.locator('input[name="permission"][value="delete_wiki_pages"]')
    expect(manage_wiki).to_be_checked()
    expect(delete_wiki).not_to_be_checked()
    delete_wiki.check()
    admin_page.get_by_role("button", name="Save", exact=True).click()
    expect(admin_page.get_by_role("status")).to_have_text("Saved.")

    saved = api_client.get(f"/api/v1/admin/roles/{role_id}/").json()
    assert "delete_wiki_pages" in saved["permissions"]
    assert "manage_wiki" in saved["permissions"]

    # The new role is offered on the project's member form.
    admin_page.goto(f"/projects/{project_key}/settings/")
    admin_page.get_by_role("link", name="Members").click()
    expect(admin_page.locator(".sp-field-min-160 select option", has_text=role_name)).to_have_count(1)
    tracker.assert_no_errors()

    # Give the member the new role instead of Developer.
    resp = api_client.patch(f"/api/v1/projects/{project_key}/members/user/{user_id}/", json={"role_ids": [role_id]})
    assert resp.status_code == 200, resp.text

    # Now the member can delete a page, and open the trash it went to.
    assert member_api.delete(f"/api/v1/projects/{project_key}/wiki/{second_slug}/").status_code == 204
    trash = member_page.goto(f"/projects/{project_key}/wiki/trash/")
    assert trash is not None
    assert trash.status == 200
    expect(member_page.get_by_role("cell", name=second_title)).to_be_visible()
