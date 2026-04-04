"""E2E test fixtures for specivo-core.

Re-exports shared Playwright fixtures from specivo.testing.e2e_base.
"""

import json

import httpx
import pytest

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
