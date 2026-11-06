"""E2E: the searchable assignee, tag and release filters on the issue list.

Type a few letters, pick a suggestion (mouse or keyboard), see the list
filtered and the choice as a chip; clear the chip to drop the filter. Also on
the board view and at phone width.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

ALICE_SUBJECT = "Assigned to the e2e user"
FREE_SUBJECT = "Nobody picked this up"
TAGGED_SUBJECT = "Tagged for the release"


def _db_value(sql: str, *args: object) -> object:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

    async def _run() -> object:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    # pytest-playwright runs an event loop of its own; use a fresh one elsewhere.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture
def filter_project(api_client: httpx.Client) -> dict:
    """A project with one member besides the admin, a tag, a release and three issues."""
    project = create_project(api_client, prefix="FCB")
    key = project["key"]
    role_id = _db_value(
        "INSERT INTO roles (name, permissions) VALUES ($1, '[\"view_issues\", \"add_issues\"]'::jsonb) RETURNING id",
        f"Filter viewer {key}",
    )
    user_id = _db_value("SELECT id FROM users WHERE lower(login) = 'e2e_user'")
    resp = api_client.post(f"/api/v1/projects/{key}/members/", json={"user_id": user_id, "role_ids": [role_id]})
    assert resp.status_code in (200, 201), resp.text
    version = api_client.post(f"/api/v1/projects/{key}/versions/", json={"name": "Orchid 1.0"})
    assert version.status_code == 201, version.text
    create_issue(api_client, key, ALICE_SUBJECT, assigned_to_id=user_id)
    create_issue(api_client, key, FREE_SUBJECT)
    tagged = create_issue(api_client, key, TAGGED_SUBJECT, fixed_version_id=version.json()["id"])
    resp = api_client.post(f"/api/v1/issues/{tagged['key']}/tags/", json={"name": "mango"})
    assert resp.status_code == 201, resp.text
    return {"key": key, "user_id": user_id, "version_id": version.json()["id"]}


def _open_list(page: Page, key: str, query: str = "") -> None:
    page.goto(f"/projects/{key}/issues/{'?' + query if query else ''}")
    page.wait_for_load_state("networkidle")


def _combo(page: Page, name: str):
    return page.locator(f'[data-filter-combo="{name}"]')


def _expect_listed(page: Page, *subjects: str) -> None:
    for subject in (ALICE_SUBJECT, FREE_SUBJECT, TAGGED_SUBJECT):
        expect(page.get_by_text(subject, exact=True)).to_have_count(1 if subject in subjects else 0)


def test_type_pick_and_clear_an_assignee(admin_page: Page, filter_project: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    _open_list(admin_page, filter_project["key"])
    field = _combo(admin_page, "assigned_to_id").get_by_role("combobox")
    expect(field).to_have_attribute("aria-expanded", "false")
    field.click()
    field.press_sequentially("e2e u")
    listbox = _combo(admin_page, "assigned_to_id").get_by_role("listbox")
    expect(field).to_have_attribute("aria-expanded", "true")
    option = listbox.get_by_role("option", name="E2E User")
    expect(option).to_be_visible()
    # The reader's own shortcuts drop out when they do not match.
    expect(listbox.get_by_role("option", name="Unassigned")).to_have_count(0)
    option.click()

    admin_page.wait_for_load_state("networkidle")
    assert parse_qs(urlsplit(admin_page.url).query)["assigned_to_id"] == [str(filter_project["user_id"])]
    chip = admin_page.locator('[data-filter-chip="assigned_to_id"]')
    expect(chip).to_contain_text("E2E User")
    _expect_listed(admin_page, ALICE_SUBJECT)

    chip.get_by_role("link").click()
    admin_page.wait_for_load_state("networkidle")
    assert "assigned_to_id" not in admin_page.url
    expect(chip).to_have_count(0)
    _expect_listed(admin_page, ALICE_SUBJECT, FREE_SUBJECT, TAGGED_SUBJECT)
    tracker.assert_no_errors()


def test_keyboard_selects_unassigned(admin_page: Page, filter_project: dict) -> None:
    _open_list(admin_page, filter_project["key"])
    field = _combo(admin_page, "assigned_to_id").get_by_role("combobox")
    field.focus()
    listbox = _combo(admin_page, "assigned_to_id").get_by_role("listbox")
    options = listbox.get_by_role("option")
    expect(options.first).to_be_visible()
    # "Me" comes first and is active; one step down is "Unassigned".
    expect(field).to_have_attribute("aria-activedescendant", options.nth(0).get_attribute("id"))
    field.press("ArrowDown")
    second = options.nth(1)
    expect(second).to_have_text("Unassigned")
    expect(second).to_have_attribute("aria-selected", "true")
    expect(field).to_have_attribute("aria-activedescendant", second.get_attribute("id"))
    field.press("Escape")
    expect(field).to_have_attribute("aria-expanded", "false")
    expect(listbox).to_be_hidden()
    # Reopening from the keyboard starts at the top again.
    field.press("ArrowDown")
    expect(field).to_have_attribute("aria-expanded", "true")
    expect(options.nth(0)).to_have_attribute("aria-selected", "true")
    field.press("ArrowDown")
    field.press("Enter")

    admin_page.wait_for_load_state("networkidle")
    assert parse_qs(urlsplit(admin_page.url).query)["assigned_to_id"] == ["none"]
    expect(admin_page.locator('[data-filter-chip="assigned_to_id"]')).to_contain_text("Unassigned")
    _expect_listed(admin_page, FREE_SUBJECT, TAGGED_SUBJECT)


def test_tag_and_release_filters_on_the_board(admin_page: Page, filter_project: dict) -> None:
    _open_list(admin_page, filter_project["key"], "view=board")
    tag_field = _combo(admin_page, "tag_id").get_by_role("combobox")
    tag_field.click()
    tag_field.press_sequentially("man")
    _combo(admin_page, "tag_id").get_by_role("option", name="mango").click()
    admin_page.wait_for_load_state("networkidle")
    query = parse_qs(urlsplit(admin_page.url).query)
    assert query["view"] == ["board"]
    expect(admin_page.locator('[data-filter-chip="tag_id"]')).to_contain_text("mango")
    expect(admin_page.locator(".kanban-card")).to_have_count(1)
    expect(admin_page.locator(".kanban-card")).to_contain_text(TAGGED_SUBJECT)

    # The tag stays while a release is chosen too.
    release = _combo(admin_page, "fixed_version_id").get_by_role("combobox")
    release.click()
    release.press_sequentially("orch")
    _combo(admin_page, "fixed_version_id").get_by_role("option", name="Orchid 1.0").click()
    admin_page.wait_for_load_state("networkidle")
    query = parse_qs(urlsplit(admin_page.url).query)
    assert query["fixed_version_id"] == [str(filter_project["version_id"])]
    assert "tag_id" in query
    assert query["view"] == ["board"]
    expect(admin_page.locator('[data-filter-chip="fixed_version_id"]')).to_contain_text("Orchid 1.0")
    expect(admin_page.locator(".kanban-card")).to_have_count(1)

    # Clearing the release chip keeps the tag and the board.
    admin_page.locator('[data-filter-chip="fixed_version_id"]').get_by_role("link").click()
    admin_page.wait_for_load_state("networkidle")
    query = parse_qs(urlsplit(admin_page.url).query)
    assert "fixed_version_id" not in query
    assert "tag_id" in query
    assert query["view"] == ["board"]


def test_no_release_filter(admin_page: Page, filter_project: dict) -> None:
    _open_list(admin_page, filter_project["key"])
    release = _combo(admin_page, "fixed_version_id").get_by_role("combobox")
    release.click()
    release.press_sequentially("no rel")
    _combo(admin_page, "fixed_version_id").get_by_role("option", name="No release").click()
    admin_page.wait_for_load_state("networkidle")
    assert parse_qs(urlsplit(admin_page.url).query)["fixed_version_id"] == ["none"]
    _expect_listed(admin_page, ALICE_SUBJECT, FREE_SUBJECT)


def test_filter_bar_fits_a_phone(admin_page: Page, filter_project: dict) -> None:
    admin_page.set_viewport_size({"width": 375, "height": 812})
    _open_list(
        admin_page,
        filter_project["key"],
        f"assigned_to_id={filter_project['user_id']}&fixed_version_id={filter_project['version_id']}",
    )
    expect(admin_page.locator('[data-filter-chip="assigned_to_id"]')).to_be_visible()
    assert admin_page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2"
    ), "the page scrolls sideways at 375 px"
    tag_field = _combo(admin_page, "tag_id").get_by_role("combobox")
    tag_field.click()
    menu = _combo(admin_page, "tag_id").get_by_role("listbox")
    expect(menu.get_by_role("option", name="mango")).to_be_visible()
    box = menu.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 375 + 1
    assert admin_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2")
