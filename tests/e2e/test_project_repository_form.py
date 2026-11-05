"""E2E: the repository form says what is missing on the field itself, and never sends an invalid form.

Save stays enabled. A click with errors marks the failed fields (red border,
``aria-invalid``, a message linked by ``aria-describedby``), focuses the first
one and sends no request. Errors the server reports with a ``field`` land on
that field the same way.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Locator, Page, Request, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

_REPO = "example-org/widgets"
_URL = f"https://git.example.org/{_REPO}"
_RED = "rgb(220, 38, 38)"


@pytest.fixture
def project_key(api_client: httpx.Client) -> str:
    return create_project(api_client)["key"]


def _open_form(page: Page, project_key: str) -> Locator:
    page.goto(f"/projects/{project_key}/settings/")
    page.get_by_role("link", name="Repositories").click()
    expect(page.get_by_test_id("repositories-card")).to_be_visible()
    page.get_by_role("button", name="Connect a repository").first.click()
    modal = _modal(page)
    expect(modal).to_be_visible()
    return modal


def _modal(page: Page) -> Locator:
    return page.locator(".sp-modal").filter(has=page.get_by_test_id("repository-save"))


def _record_posts(page: Page, project_key: str) -> list[Request]:
    sent: list[Request] = []
    path = f"/api/v1/projects/{project_key}/repositories/"

    def on_request(req: Request) -> None:
        if req.method in ("POST", "PATCH") and path in req.url:
            sent.append(req)

    page.on("request", on_request)
    return sent


def _expect_invalid(page: Page, field: Locator, error_id: str, message: str) -> None:
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_css("border-top-color", _RED)
    described = field.get_attribute("aria-describedby") or ""
    assert error_id in described.split(), described
    error = page.locator(f"#{error_id}")
    expect(error).to_be_visible()
    expect(error).to_have_text(message)


def _expect_valid(page: Page, field: Locator, error_id: str) -> None:
    expect(field).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator(f"#{error_id}")).to_be_hidden()


def test_merge_status_missing_is_shown_on_the_field(admin_page: Page, project_key: str) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    sent = _record_posts(admin_page, project_key)
    _open_form(admin_page, project_key)
    admin_page.get_by_test_id("repository-name").fill(_REPO)
    admin_page.get_by_test_id("repository-url").fill(_URL)
    admin_page.get_by_test_id("repository-merge-automation").check()

    merge_status = admin_page.get_by_test_id("repository-merge-status")
    # Ticking merge automation alone is not a mistake yet: nothing is red.
    _expect_valid(admin_page, merge_status, "repository-merge-status-error")

    save = admin_page.get_by_test_id("repository-save")
    expect(save).to_be_enabled()
    save.click()
    _expect_invalid(
        admin_page,
        merge_status,
        "repository-merge-status-error",
        "Choose a merge status, or turn merge automation off.",
    )
    expect(merge_status).to_be_focused()
    assert sent == []

    merge_status.select_option(index=1)
    _expect_valid(admin_page, merge_status, "repository-merge-status-error")
    save.click()
    expect(admin_page.get_by_test_id("repository-row")).to_have_count(1)
    expect(admin_page.get_by_test_id("repository-row")).to_contain_text(_REPO)
    assert len(sent) == 1
    tracker.assert_no_errors()


def test_empty_repository_and_url_are_shown_on_the_fields(admin_page: Page, project_key: str) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    sent = _record_posts(admin_page, project_key)
    _open_form(admin_page, project_key)
    name = admin_page.get_by_test_id("repository-name")
    url = admin_page.get_by_test_id("repository-url")

    # A pristine form shows no red.
    _expect_valid(admin_page, name, "repository-name-error")
    _expect_valid(admin_page, url, "repository-url-error")
    expect(_modal(admin_page).locator(".form-error:visible")).to_have_count(0)

    save = admin_page.get_by_test_id("repository-save")
    save.click()
    _expect_invalid(admin_page, name, "repository-name-error", "Enter the repository as owner/name.")
    _expect_invalid(admin_page, url, "repository-url-error", "Enter the repository's http(s) address.")
    # Repository URL comes first in the form, so it gets the focus.
    expect(url).to_be_focused()
    # The hint stays linked next to the message.
    assert "repository-name-hint" in (name.get_attribute("aria-describedby") or "").split()
    assert sent == []

    name.fill(_REPO)
    _expect_valid(admin_page, name, "repository-name-error")
    save.click()
    expect(url).to_be_focused()
    assert sent == []

    url.fill("not a url")
    expect(url).to_have_attribute("aria-invalid", "true")
    url.fill(_URL)
    _expect_valid(admin_page, url, "repository-url-error")
    save.click()
    expect(admin_page.get_by_test_id("repository-row")).to_have_count(1)
    assert len(sent) == 1
    tracker.assert_no_errors()


def test_a_field_left_empty_is_marked_when_left(admin_page: Page, project_key: str) -> None:
    _open_form(admin_page, project_key)
    name = admin_page.get_by_test_id("repository-name")
    url = admin_page.get_by_test_id("repository-url")
    name.focus()
    url.focus()
    _expect_invalid(admin_page, name, "repository-name-error", "Enter the repository as owner/name.")
    # The field being typed in is not judged until it is left.
    _expect_valid(admin_page, url, "repository-url-error")


def test_server_field_error_is_shown_on_its_field(admin_page: Page, project_key: str) -> None:
    path = f"**/api/v1/projects/{project_key}/repositories/"
    body = {
        "errors": [
            {
                "code": "validation_error",
                "message": "Value error, repo_url must be an http or https URL",
                "field": "repo_url",
                "details": None,
            }
        ]
    }
    admin_page.route(
        path,
        lambda route: (
            route.fulfill(status=422, content_type="application/json", body=json.dumps(body))
            if route.request.method == "POST"
            else route.continue_()
        ),
    )
    _open_form(admin_page, project_key)
    name = admin_page.get_by_test_id("repository-name")
    url = admin_page.get_by_test_id("repository-url")
    name.fill(_REPO)
    url.fill(_URL)
    admin_page.get_by_test_id("repository-save").click()

    _expect_invalid(admin_page, url, "repository-url-error", "repo_url must be an http or https URL")
    expect(url).to_be_focused()
    _expect_valid(admin_page, name, "repository-name-error")
    # No general banner: the error is on the field.
    expect(_modal(admin_page).locator(".flash-error")).to_have_count(0)

    # Editing the value clears the server's message.
    url.fill(_URL + "-2")
    _expect_valid(admin_page, url, "repository-url-error")


def test_duplicate_repository_is_shown_on_the_repository_field(
    admin_page: Page, project_key: str, api_client: httpx.Client
) -> None:
    resp = api_client.post(
        f"/api/v1/projects/{project_key}/repositories/",
        json={"provider": "forgejo", "repo_full_name": _REPO, "repo_url": _URL},
    )
    assert resp.status_code == 201, resp.text
    _open_form(admin_page, project_key)
    admin_page.get_by_label("Provider").select_option("forgejo")
    name = admin_page.get_by_test_id("repository-name")
    name.fill(_REPO)
    admin_page.get_by_test_id("repository-url").fill(_URL)
    admin_page.get_by_test_id("repository-save").click()
    _expect_invalid(admin_page, name, "repository-name-error", "This repository is already connected to the project.")
    expect(name).to_be_focused()


def test_disabled_primary_button_looks_disabled(admin_page: Page, project_key: str) -> None:
    admin_page.goto(f"/projects/{project_key}/settings/")
    style = admin_page.evaluate(
        """() => {
            const enabled = document.createElement('button');
            enabled.className = 'sp-btn sp-btn-primary';
            const disabled = enabled.cloneNode();
            disabled.disabled = true;
            document.body.append(enabled, disabled);
            const read = (el) => ({opacity: getComputedStyle(el).opacity, cursor: getComputedStyle(el).cursor});
            const out = {enabled: read(enabled), disabled: read(disabled)};
            enabled.remove();
            disabled.remove();
            return out;
        }"""
    )
    assert style["enabled"] == {"opacity": "1", "cursor": "pointer"}
    assert float(style["disabled"]["opacity"]) <= 0.6
    assert style["disabled"]["cursor"] == "not-allowed"
