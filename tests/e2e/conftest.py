"""E2E test fixtures for specivo-core.

Re-exports shared Playwright fixtures from specivo.testing.e2e_base.
"""

import json
from collections.abc import Generator

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page

from specivo.testing.e2e_base import (  # noqa: F401
    _admin_auth,
    _flush_redis,
    _run_migrations,
    _seed_lookups,
    _user_auth,
    admin_context,
    admin_page,
    api_client,
    auth_context,
    auth_page,
    e2e_base_url,
    e2e_seed_data,
    e2e_server,
)

# ---------------------------------------------------------------------------
# Palette setting — ensure the E2E DB has a multi-color palette
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _seed_avatar_palette(e2e_server, _admin_auth):  # noqa: F811
    """Upsert the avatar_color_palette setting via the admin API once per session.

    The CLI seed may not have been run against this test DB; this fixture
    guarantees a palette with multiple colors is present so E2E tests that
    count swatches are stable.
    """
    token, _ = _admin_auth  # noqa: F811
    palette = ["#c49a3c", "#5B8C5A", "#7B68AE", "#E07B6C", "#4A90B8"]
    resp = httpx.patch(
        f"{e2e_server}/api/v1/admin/settings/",
        json={"avatar_color_palette": json.dumps(palette)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code not in (200, 204):
        pytest.fail(f"Failed to seed avatar_color_palette via admin API: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# Shared seed project for responsive & visual regression tests
# ---------------------------------------------------------------------------

RESPONSIVE_PROJECT_KEY = "RTEST"


@pytest.fixture(scope="session")
def responsive_project(e2e_server, _admin_auth) -> str:  # noqa: F811
    """Create a project with fixed content for responsive/visual tests.

    Uses a deterministic key (RTEST) so that screenshots and structural
    assertions are stable across runs. Returns the project key.
    """
    token, _ = _admin_auth
    headers = {"Authorization": f"Bearer {token}"}

    # Create project (ignore conflict if already exists)
    resp = httpx.post(
        f"{e2e_server}/api/v1/projects/",
        json={"name": "Responsive Test", "identifier": "rtest", "key": RESPONSIVE_PROJECT_KEY},
        headers=headers,
        timeout=10,
    )
    if resp.status_code not in (201, 409, 422):
        pytest.fail(f"Failed to create responsive project: {resp.status_code} {resp.text}")

    # Seed issues if empty
    resp = httpx.get(
        f"{e2e_server}/api/v1/projects/{RESPONSIVE_PROJECT_KEY}/issues/",
        headers=headers,
        timeout=10,
    )
    existing = resp.json().get("total", 0) if resp.status_code == 200 else 0

    if existing == 0:
        # Get first tracker ID
        from specivo.testing.e2e_base import get_first_tracker_id

        client = httpx.Client(base_url=e2e_server, headers=headers, timeout=10)
        tracker_id = get_first_tracker_id(client)

        for subject in [
            "Setup CI/CD pipeline",
            "Add user authentication",
            "Fix mobile sidebar overlap",
            "Implement search functionality",
            "Update project documentation",
            "Add dark mode support",
            "Improve loading performance",
            "Write unit tests for auth module",
        ]:
            httpx.post(
                f"{e2e_server}/api/v1/projects/{RESPONSIVE_PROJECT_KEY}/issues/",
                json={"subject": subject, "project_key": RESPONSIVE_PROJECT_KEY, "tracker_id": tracker_id},
                headers=headers,
                timeout=10,
            )
        client.close()

    # Seed wiki home with realistic content (PUT is idempotent)
    httpx.put(
        f"{e2e_server}/api/v1/projects/{RESPONSIVE_PROJECT_KEY}/wiki/home/",
        json={
            "text": (
                "## Overview\n\n"
                "The Responsive Test project validates layout behavior across multiple "
                "viewport sizes. This wiki page includes realistic content to test how "
                "long titles and rich articles render at different breakpoints.\n\n"
                "## Architecture\n\n"
                "The application uses a server-rendered stack with Jinja2 templates, "
                "Alpine.js for reactivity, and HTMX for partial page updates. CSS "
                "breakpoints control layout changes at 1100px, 960px, 768px, and 480px.\n\n"
                "### Frontend Components\n\n"
                "- **Sidebar**: Fixed on desktop, off-canvas on tablet/mobile\n"
                "- **Issue table**: Columns progressively hidden at narrower widths\n"
                "- **Wiki layout**: Two-column above 1100px, single column below\n"
                "- **Page headers**: Inline on desktop, stacked on mobile\n\n"
                "### Data Model\n\n"
                "Projects contain issues, wiki pages, sprints, and versions. Each entity "
                "supports custom fields and file attachments.\n\n"
                "## Configuration\n\n"
                "Settings are managed through the project settings page. Admins can "
                "configure trackers, statuses, priorities, and custom fields.\n\n"
                "| Setting | Default | Description |\n"
                "|---------|---------|-------------|\n"
                "| Theme | Dark | UI color scheme |\n"
                "| Language | English | Interface language |\n"
                "| Timezone | UTC | Display timezone |\n"
            )
        },
        headers=headers,
        timeout=10,
    )

    return RESPONSIVE_PROJECT_KEY


# ---------------------------------------------------------------------------
# Viewport definitions for responsive testing
# ---------------------------------------------------------------------------

VIEWPORTS = {
    "mobile": {"width": 375, "height": 812},
    "tablet": {"width": 768, "height": 1024},
    "narrow": {"width": 960, "height": 800},
    "desktop": {"width": 1280, "height": 800},
}


@pytest.fixture(params=list(VIEWPORTS.keys()), ids=list(VIEWPORTS.keys()))
def viewport_name(request) -> str:
    """Parametrized fixture that yields each viewport name."""
    return request.param


@pytest.fixture
def viewport_size(viewport_name: str) -> dict:
    """Returns {width, height} for the current viewport_name."""
    return VIEWPORTS[viewport_name]


@pytest.fixture
def responsive_page(
    browser, e2e_server, _admin_auth, viewport_name: str  # noqa: F811
) -> Generator[Page, None, None]:
    """Authenticated page (admin) at the parametrized viewport size."""
    _, cookies = _admin_auth
    size = VIEWPORTS[viewport_name]
    context: BrowserContext = browser.new_context(
        base_url=e2e_server,
        viewport=size,
    )
    context.add_cookies(cookies)
    page = context.new_page()
    yield page
    page.close()
    context.close()
