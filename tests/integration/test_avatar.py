"""Integration tests for avatar color and photo upload features.

Covers:
- Auto-assignment of avatar_color on first login (auth_service)
- Preservation of existing avatar_color on subsequent logins
- GET /my/preferences/ rendering (palette swatches)
- POST /my/preferences/ accepting / rejecting colors
- POST /my/profile/avatar/ and POST /my/profile/avatar/delete/ endpoints
- Issue list renders user avatar color
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.setting import Setting
from specivo.testing.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACCESS_COOKIE = "access_token"


async def _upsert_palette(db_session: AsyncSession, value: str) -> None:
    """Upsert the avatar_color_palette setting (handles existing seed data)."""
    from sqlalchemy import select

    result = await db_session.execute(select(Setting).where(Setting.key == "avatar_color_palette"))
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = value
    else:
        db_session.add(Setting(key="avatar_color_palette", value=value))
    await db_session.flush()


# ---------------------------------------------------------------------------
# Login auto-assigns avatar_color
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_login_assigns_avatar_color_when_none(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """First login auto-assigns an avatar_color from the palette."""
    # User explicitly has no avatar_color in preferences
    user = UserFactory.build(preferences={})
    db_session.add(user)

    await _upsert_palette(db_session, '["#5B8C5A","#7B68AE"]')

    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.preferences.get("avatar_color") in {"#5B8C5A", "#7B68AE"}


@pytest.mark.integration
async def test_login_preserves_existing_avatar_color(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Login does not overwrite an already-set avatar_color."""
    user = UserFactory.build(preferences={"avatar_color": "#FF0000"})
    db_session.add(user)
    await db_session.flush()

    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.preferences["avatar_color"] == "#FF0000"


@pytest.mark.integration
async def test_login_uses_default_fallback_when_no_palette_setting(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """When the palette setting is absent, the default fallback color is used."""
    from sqlalchemy import delete

    # Remove any existing palette setting so fallback kicks in
    await db_session.execute(delete(Setting).where(Setting.key == "avatar_color_palette"))

    user = UserFactory.build(preferences={})
    db_session.add(user)
    await db_session.flush()
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": user.login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200

    await db_session.refresh(user)
    # Color should be one from the default palette
    from specivo.core.constants import DEFAULT_AVATAR_PALETTE

    assert user.preferences.get("avatar_color") in DEFAULT_AVATAR_PALETTE


# ---------------------------------------------------------------------------
# GET /my/preferences/ — palette rendering
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_preferences_page_redirects_when_unauthenticated(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /my/preferences/ without a token redirects to /login/."""
    resp = await client.get("/my/preferences/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_preferences_page_shows_palette_swatches(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /my/preferences/ renders all palette colors as swatches."""
    await _upsert_palette(db_session, '["#c49a3c","#5B8C5A"]')

    token = auth_client.state.token
    resp = await auth_client.get(
        "/my/preferences/",
        cookies={_ACCESS_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "#c49a3c" in resp.text
    assert "#5B8C5A" in resp.text
    assert "sp-color-swatch" in resp.text


@pytest.mark.integration
async def test_preferences_page_shows_fallback_swatch_without_setting(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """When palette setting is missing, preferences page shows the fallback color."""
    # No Setting row — SettingsService returns ["#c49a3c"]
    token = auth_client.state.token
    resp = await auth_client.get(
        "/my/preferences/",
        cookies={_ACCESS_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "#c49a3c" in resp.text


# ---------------------------------------------------------------------------
# POST /my/preferences/ — color change
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_preferences_post_changes_avatar_color(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/preferences/ with a valid palette color updates user.preferences."""
    await _upsert_palette(db_session, '["#c49a3c","#5B8C5A"]')

    token = auth_client.state.token
    resp = await auth_client.post(
        "/my/preferences/",
        data={"avatar_color": "#5B8C5A"},
        cookies={_ACCESS_COOKIE: token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    user = auth_client.state.user
    await db_session.refresh(user)
    assert user.preferences["avatar_color"] == "#5B8C5A"


@pytest.mark.integration
async def test_preferences_post_rejects_color_not_in_palette(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/preferences/ with a non-palette color is rejected (does not update)."""
    await _upsert_palette(db_session, '["#c49a3c"]')

    original_color = auth_client.state.user.preferences.get("avatar_color")

    token = auth_client.state.token
    resp = await auth_client.post(
        "/my/preferences/",
        data={"avatar_color": "#BADBAD"},
        cookies={_ACCESS_COOKIE: token},
        follow_redirects=False,
    )
    # The handler may re-render with 422 or redirect with an error flash;
    # the contract is: not a 200 success commit, user color must be unchanged.
    assert resp.status_code in {303, 422, 400}

    user = auth_client.state.user
    await db_session.refresh(user)
    # Color must not have been changed to the bad value
    assert user.preferences.get("avatar_color") != "#BADBAD"
    if original_color is not None:
        assert user.preferences.get("avatar_color") == original_color


@pytest.mark.integration
async def test_preferences_post_empty_color_is_noop(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/preferences/ with an empty avatar_color is a no-op (no update)."""
    await _upsert_palette(db_session, '["#c49a3c"]')

    user = auth_client.state.user
    user.preferences = {**user.preferences, "avatar_color": "#c49a3c"}
    await db_session.flush()

    token = auth_client.state.token
    resp = await auth_client.post(
        "/my/preferences/",
        data={"avatar_color": ""},
        cookies={_ACCESS_COOKIE: token},
        follow_redirects=False,
    )
    # Empty string → no update branch → redirect
    assert resp.status_code == 303

    await db_session.refresh(user)
    assert user.preferences["avatar_color"] == "#c49a3c"


@pytest.mark.integration
async def test_preferences_post_redirects_unauthenticated(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/preferences/ without auth redirects to /login/."""
    resp = await client.post(
        "/my/preferences/",
        data={"avatar_color": "#c49a3c"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /my/profile/avatar/delete/
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_avatar_clears_avatar_url(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/profile/avatar/delete/ sets user.avatar_url to None and redirects."""
    user = auth_client.state.user
    user.avatar_url = "/data/avatars/ab/abcdef.webp"
    await db_session.flush()

    token = auth_client.state.token
    resp = await auth_client.post(
        "/my/profile/avatar/delete/",
        cookies={_ACCESS_COOKIE: token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db_session.refresh(user)
    assert user.avatar_url is None


@pytest.mark.integration
async def test_delete_avatar_noop_when_no_avatar(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/profile/avatar/delete/ succeeds even when no avatar is set."""
    user = auth_client.state.user
    user.avatar_url = None
    await db_session.flush()

    token = auth_client.state.token
    resp = await auth_client.post(
        "/my/profile/avatar/delete/",
        cookies={_ACCESS_COOKIE: token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db_session.refresh(user)
    assert user.avatar_url is None


@pytest.mark.integration
async def test_delete_avatar_redirects_unauthenticated(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/profile/avatar/delete/ without auth redirects to /login/."""
    resp = await client.post(
        "/my/profile/avatar/delete/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /my/profile/avatar/ — upload
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_avatar_redirects_unauthenticated(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /my/profile/avatar/ without auth redirects to /login/."""
    resp = await client.post(
        "/my/profile/avatar/",
        files={"file": ("avatar.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Issue list renders avatar color (smoke test)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_issue_list_returns_200_for_authenticated_user(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """Issue list page loads successfully — smoke test for avatar color rendering.

    Full avatar color assertion requires a seeded project/issue; this test
    verifies the page at least renders without error for an admin with a color.
    """
    from specivo.schemas.project import ProjectCreate
    from specivo.services.project_service import ProjectService

    user = admin_client.state.user
    user.preferences = {**user.preferences, "avatar_color": "#5B8C5A"}
    await db_session.flush()

    project = await ProjectService().create(
        db_session,
        ProjectCreate(name="Color Test", identifier="color-test-il", key="CLRIL"),
        user,
    )
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{project.key}/issues/",
        cookies={_ACCESS_COOKIE: token},
    )
    assert resp.status_code == 200
