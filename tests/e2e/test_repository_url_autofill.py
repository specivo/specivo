"""E2E: the repository form fills Provider and Repository from a pasted URL, and the provider can change.

The browser's parser runs the same case table as the server's
(``tests/fixtures/repo_urls.json``), so the two stay in step.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import BrowserContext, Locator, Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

_CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "repo_urls.json").read_text())["cases"]


@pytest.fixture
def project_key(api_client: httpx.Client) -> str:
    return create_project(api_client)["key"]


def _open_tab(page: Page, project_key: str) -> None:
    page.goto(f"/projects/{project_key}/settings/")
    page.get_by_role("link", name="Repositories").click()
    expect(page.get_by_test_id("repositories-card")).to_be_visible()


def _open_form(page: Page, project_key: str) -> Locator:
    _open_tab(page, project_key)
    page.get_by_role("button", name="Connect a repository").first.click()
    modal = page.locator(".sp-modal").filter(has=page.get_by_test_id("repository-save"))
    expect(modal).to_be_visible()
    return modal


def test_browser_parser_matches_the_shared_table(admin_page: Page, project_key: str) -> None:
    _open_tab(admin_page, project_key)
    results = admin_page.evaluate(
        """(cases) => cases.map((c) => {
            const r = window.spParseRepoAddress(c.input, c.provider);
            return r ? {provider: r.provider, repo_full_name: r.repoFullName, url: r.url} : null;
        })""",
        _CASES,
    )
    mismatches = [
        (case["input"], case["provider"], got, case["expected"])
        for case, got in zip(_CASES, results, strict=True)
        if got != case["expected"]
    ]
    assert mismatches == []


def test_url_comes_first(admin_page: Page, project_key: str) -> None:
    modal = _open_form(admin_page, project_key)
    ids = modal.locator(".form-input").evaluate_all("(els) => els.slice(0, 3).map((el) => el.id)")
    assert ids == ["repository-url", "repository-provider", "repository-name"]


def test_pasting_a_gitlab_sub_page_fills_provider_and_full_path(
    admin_context: BrowserContext, admin_page: Page, project_key: str
) -> None:
    admin_context.grant_permissions(["clipboard-read", "clipboard-write"])
    tracker = ConsoleErrorTracker().attach(admin_page)
    _open_form(admin_page, project_key)
    provider = admin_page.get_by_test_id("repository-provider")
    expect(provider).to_have_value("github")

    url = admin_page.get_by_test_id("repository-url")
    admin_page.evaluate(
        "(text) => navigator.clipboard.writeText(text)",
        "https://gitlab.com/example-group/platform/widgets/-/tree/main",
    )
    url.focus()
    admin_page.keyboard.press("ControlOrMeta+V")

    expect(provider).to_have_value("gitlab")
    expect(admin_page.get_by_test_id("repository-name")).to_have_value("example-group/platform/widgets")
    # A paste rewrites the address as the repository's root.
    expect(url).to_have_value("https://gitlab.com/example-group/platform/widgets")
    expect(admin_page.get_by_test_id("repository-fill-note")).to_contain_text(
        "Detected GitLab repository example-group/platform/widgets"
    )

    admin_page.get_by_test_id("repository-save").click()
    row = admin_page.get_by_test_id("repository-row")
    expect(row).to_contain_text("GitLab")
    expect(row).to_contain_text("example-group/platform/widgets")
    tracker.assert_no_errors()


def test_typed_github_ssh_address_becomes_the_web_url(admin_page: Page, project_key: str) -> None:
    _open_form(admin_page, project_key)
    admin_page.get_by_test_id("repository-provider").select_option("forgejo")
    # A provider picked by hand is kept even for a known host.
    url = admin_page.get_by_test_id("repository-url")
    url.fill("git@github.com:example-org/widgets.git")
    expect(admin_page.get_by_test_id("repository-provider")).to_have_value("forgejo")
    expect(admin_page.get_by_test_id("repository-name")).to_have_value("example-org/widgets")
    url.press("Tab")
    expect(url).to_have_value("https://github.com/example-org/widgets")

    # The server refuses the mismatch, on the Provider field.
    admin_page.get_by_test_id("repository-save").click()
    provider = admin_page.get_by_test_id("repository-provider")
    expect(provider).to_have_attribute("aria-invalid", "true")
    expect(admin_page.get_by_test_id("repository-provider-error")).to_have_text(
        "github.com repositories use the GitHub provider."
    )
    expect(provider).to_be_focused()
    provider.select_option("github")
    expect(provider).not_to_have_attribute("aria-invalid", "true")
    admin_page.get_by_test_id("repository-save").click()
    expect(admin_page.get_by_test_id("repository-row")).to_contain_text("GitHub")


def test_pasting_a_github_url_switches_the_provider(admin_page: Page, project_key: str) -> None:
    _open_form(admin_page, project_key)
    admin_page.get_by_test_id("repository-url").fill("https://gitlab.com/example-group/widgets")
    expect(admin_page.get_by_test_id("repository-provider")).to_have_value("gitlab")
    admin_page.get_by_test_id("repository-url").fill("https://github.com/specivo/specivo/pulls")
    # Both still hold what was filled in, so a new address fills them again.
    expect(admin_page.get_by_test_id("repository-provider")).to_have_value("github")
    expect(admin_page.get_by_test_id("repository-name")).to_have_value("specivo/specivo")
    expect(admin_page.get_by_test_id("repository-fill-note")).to_contain_text(
        "Detected GitHub repository specivo/specivo"
    )


def test_repository_typed_by_hand_is_not_overwritten(admin_page: Page, project_key: str) -> None:
    _open_form(admin_page, project_key)
    name = admin_page.get_by_test_id("repository-name")
    name.fill("example-org/mirror")
    admin_page.get_by_test_id("repository-url").fill("https://gitlab.com/example-group/widgets")
    expect(admin_page.get_by_test_id("repository-provider")).to_have_value("gitlab")
    expect(name).to_have_value("example-org/mirror")


def test_self_hosted_url_keeps_the_chosen_provider(admin_page: Page, project_key: str) -> None:
    _open_form(admin_page, project_key)
    admin_page.get_by_test_id("repository-provider").select_option("forgejo")
    url = admin_page.get_by_test_id("repository-url")
    url.fill("https://git.example.org/example-org/widgets/src/branch/main")
    expect(admin_page.get_by_test_id("repository-provider")).to_have_value("forgejo")
    expect(admin_page.get_by_test_id("repository-name")).to_have_value("example-org/widgets")
    expect(admin_page.get_by_test_id("repository-fill-note")).to_contain_text(
        "Filled in repository example-org/widgets from the URL"
    )
    # A self-hosted address is not rewritten: the forge may live under a sub-path.
    url.press("Tab")
    expect(url).to_have_value("https://git.example.org/example-org/widgets/src/branch/main")


def test_undo_restores_the_previous_values(admin_page: Page, project_key: str) -> None:
    _open_form(admin_page, project_key)
    provider = admin_page.get_by_test_id("repository-provider")
    name = admin_page.get_by_test_id("repository-name")
    url = admin_page.get_by_test_id("repository-url")
    # Typed key by key: every keystroke fills again; Undo goes back to before the first.
    url.press_sequentially("https://gitlab.com/example-group/widgets")
    expect(provider).to_have_value("gitlab")
    expect(name).to_have_value("example-group/widgets")

    admin_page.get_by_test_id("repository-fill-undo").click()
    expect(provider).to_have_value("github")
    expect(name).to_have_value("")
    expect(admin_page.get_by_test_id("repository-fill-note")).to_have_count(0)
    # Undone means left alone: a further edit of the address fills nothing.
    url.fill("https://gitlab.com/example-group/gadgets")
    expect(provider).to_have_value("github")
    expect(name).to_have_value("")


def test_changing_the_provider_of_an_integration(admin_page: Page, project_key: str, api_client: httpx.Client) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    created = api_client.post(
        f"/api/v1/projects/{project_key}/repositories/",
        json={
            "provider": "github",
            "repo_full_name": "example-org/widgets",
            "repo_url": "https://github.com/example-org/widgets",
        },
    )
    assert created.status_code == 201, created.text
    old_hook = created.json()["hook_url"]
    _open_tab(admin_page, project_key)
    admin_page.get_by_test_id("repository-row").get_by_role("button", name="Edit").click()

    provider = admin_page.get_by_test_id("repository-provider")
    expect(provider).to_be_enabled()
    expect(admin_page.get_by_test_id("repository-provider-warning")).to_have_count(0)
    admin_page.get_by_test_id("repository-url").fill("https://gitlab.com/example-org/widgets")
    expect(provider).to_have_value("gitlab")
    expect(admin_page.get_by_test_id("repository-provider-warning")).to_contain_text("set it up again on GitLab")
    admin_page.get_by_test_id("repository-save").click()

    reveal = admin_page.get_by_test_id("repository-secret")
    expect(reveal).to_be_visible()
    expect(admin_page.get_by_test_id("repository-provider-changed")).to_contain_text(
        "The provider changed from GitHub to GitLab"
    )
    # GitLab's setup steps, and the new URL.
    expect(reveal).to_contain_text("Secret token")
    new_hook = admin_page.get_by_test_id("repository-hook-url").inner_text()
    assert "/hooks/gitlab/" in new_hook
    assert new_hook == old_hook.replace("/hooks/github/", "/hooks/gitlab/")
    expect(admin_page.get_by_test_id("repository-row")).to_contain_text("GitLab")

    base = str(api_client.base_url).rstrip("/")
    gone = httpx.post(base + urlparse(old_hook).path, content=b"{}", headers={"X-GitHub-Event": "ping"}, timeout=10)
    assert gone.status_code == 404
    tracker.assert_no_errors()
