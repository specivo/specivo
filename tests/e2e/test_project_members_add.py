"""E2E: add a member who has no account yet, from project settings > Members.

Only existing users and groups can be added, and Add stays disabled until one
is picked from the suggestions. A name that matched nobody used to leave the
button disabled but looking active, with no message, so clicking it seemed to
do nothing. The picker now says nobody matched, an administrator can create
the account in the New User modal, and the new account is selected for Add.
"""

import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def project_key(api_client: httpx.Client) -> str:
    return create_project(api_client)["key"]


def _open_members(page: Page, project_key: str) -> None:
    page.goto(f"/projects/{project_key}/settings/")
    page.get_by_role("link", name="Members").click()
    expect(page.get_by_placeholder("Search people and groups...")).to_be_visible()


def test_unmatched_name_is_explained_and_add_looks_disabled(admin_page: Page, project_key: str) -> None:
    _open_members(admin_page, project_key)
    name = f"Nobody {uuid.uuid4().hex[:8]}"

    admin_page.get_by_placeholder("Search people and groups...").fill(name)

    expect(admin_page.get_by_test_id("member-no-match")).to_contain_text(f"No user or group matches “{name}”.")
    expect(admin_page.get_by_test_id("member-create-user")).to_contain_text(f"Create user “{name}”")

    add = admin_page.get_by_role("button", name="Add", exact=True)
    expect(add).to_be_disabled()
    expect(add).to_have_css("cursor", "not-allowed")

    # Moving on without picking anything leaves a hint behind.
    admin_page.locator("h3", has_text="Add Member").click()
    expect(admin_page.get_by_test_id("member-pick-hint")).to_be_visible()


def test_admin_creates_the_user_in_place_and_adds_them(admin_page: Page, project_key: str) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    _open_members(admin_page, project_key)
    suffix = uuid.uuid4().hex[:8]
    name = f"Teammate {suffix}"
    login = f"teammate{suffix}"

    admin_page.get_by_placeholder("Search people and groups...").fill(name)
    admin_page.get_by_test_id("member-create-user").click()

    # The shared New User modal, with the typed name carried over.
    display_name = admin_page.locator('input[x-model="newUser.display_name"]')
    expect(display_name).to_be_visible()
    expect(display_name).to_have_value(name)
    admin_page.locator('input[x-model="newUser.login"]').fill(login)
    admin_page.locator('input[x-model="newUser.email"]').fill(f"{login}@example.com")
    admin_page.locator('input[x-model="newUser.password"]').fill("correct-horse-battery")
    admin_page.get_by_role("button", name="Create", exact=True).click()

    expect(display_name).to_be_hidden()
    expect(admin_page.get_by_text(f"{name} created. Choose a role and click Add.")).to_be_visible()
    expect(admin_page.get_by_placeholder("Search people and groups...")).to_have_value(name)

    admin_page.locator(".sp-field-min-160 select").select_option(index=1)
    add = admin_page.get_by_role("button", name="Add", exact=True)
    expect(add).to_be_enabled()
    add.click()

    expect(admin_page.get_by_text(f"{name} — added.")).to_be_visible()
    expect(admin_page.locator("table").get_by_text(f"({login})")).to_be_visible()
    tracker.assert_no_errors()


def test_admin_users_page_creates_through_the_shared_modal(admin_page: Page) -> None:
    """Admin > Users renders the same modal; creating there still lists the account."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    suffix = uuid.uuid4().hex[:8]
    login = f"listed{suffix}"
    admin_page.goto("/admin/users/")

    login_field = admin_page.locator('input[x-model="newUser.login"]')
    admin_page.get_by_role("button", name="+ New User").click()
    expect(login_field).to_be_visible()

    # The backdrop and Escape both close it.
    admin_page.keyboard.press("Escape")
    expect(login_field).to_be_hidden()
    admin_page.get_by_role("button", name="+ New User").click()
    admin_page.locator(".sp-scrim.open").click(position={"x": 5, "y": 5})
    expect(login_field).to_be_hidden()

    admin_page.get_by_role("button", name="+ New User").click()
    admin_page.locator('input[x-model="newUser.display_name"]').fill(f"Listed {suffix}")
    login_field.fill(login)
    admin_page.locator('input[x-model="newUser.email"]').fill(f"{login}@example.com")
    admin_page.locator('input[x-model="newUser.password"]').fill("correct-horse-battery")
    admin_page.get_by_role("button", name="Create", exact=True).click()

    expect(admin_page.locator("table").get_by_text(login, exact=True)).to_be_visible()
    tracker.assert_no_errors()
