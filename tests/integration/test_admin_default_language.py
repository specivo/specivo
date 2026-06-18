"""Integration tests for the multi-language workspace settings.

Covers:
- Admin POST /admin/settings/defaults/ persists the default language (and
  timezone) and updates the runtime override; an unavailable code is rejected.
- Preferences POST saves user.language and rejects an unavailable code.
- The authenticated user's language preference wins over the admin default
  when rendering a user-facing HTML page.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.setting import Setting
from specivo.models.user import User

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _reset_override():
    """Keep the module-global override clean across tests."""
    from specivo.core.runtime_settings import set_default_language_override

    set_default_language_override(None)
    yield
    set_default_language_override(None)


async def _get_default_language(db: AsyncSession) -> str | None:
    row = (
        await db.execute(select(Setting).where(Setting.key == "default_language"))
    ).scalar_one_or_none()
    return row.value if row else None


# ---------------------------------------------------------------------------
# Admin POST /admin/settings/defaults/
# ---------------------------------------------------------------------------


async def test_admin_sets_default_language(admin_client: AsyncClient, db_session: AsyncSession):
    """A valid code is persisted and the runtime override is updated."""
    from specivo.core.runtime_settings import get_default_language_override

    resp = await admin_client.post(
        "/admin/settings/defaults/",
        data={"default_language": "fr"},
        cookies={"access_token": admin_client.state.token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/settings/"

    assert await _get_default_language(db_session) == "fr"
    assert get_default_language_override() == "fr"


async def test_admin_rejects_unavailable_language(admin_client: AsyncClient, db_session: AsyncSession):
    """An uninstalled code ('de', no catalog) is rejected and nothing is persisted."""
    from specivo.core.runtime_settings import get_default_language_override

    resp = await admin_client.post(
        "/admin/settings/defaults/",
        data={"default_language": "de"},
        cookies={"access_token": admin_client.state.token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert await _get_default_language(db_session) is None
    assert get_default_language_override() is None


async def test_admin_saves_language_and_timezone_together(
    admin_client: AsyncClient, db_session: AsyncSession
):
    """A single POST persists both the default language and timezone."""
    resp = await admin_client.post(
        "/admin/settings/defaults/",
        data={"default_language": "th", "default_timezone": "Asia/Bangkok"},
        cookies={"access_token": admin_client.state.token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert await _get_default_language(db_session) == "th"
    row = (
        await db_session.execute(select(Setting).where(Setting.key == "default_timezone"))
    ).scalar_one_or_none()
    assert row is not None and row.value == "Asia/Bangkok"


async def test_admin_settings_page_renders_language_select(admin_client: AsyncClient):
    """The settings page exposes a 5-language default-language select."""
    resp = await admin_client.get(
        "/admin/settings/",
        cookies={"access_token": admin_client.state.token},
    )
    assert resp.status_code == 200
    html = resp.text
    assert 'name="default_language"' in html
    for code in ("en", "es", "fr", "ru", "th", "zh"):
        assert f'value="{code}"' in html
    assert 'value="de"' not in html


# ---------------------------------------------------------------------------
# Preferences POST /my/preferences/
# ---------------------------------------------------------------------------


def _csrf_from(resp) -> str:
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    return ""


async def test_preferences_saves_language(auth_client: AsyncClient, db_session: AsyncSession):
    """A valid language preference is saved on the user."""
    user: User = auth_client.state.user

    get_resp = await auth_client.get(
        "/my/preferences/", cookies={"access_token": auth_client.state.token}
    )
    csrf = _csrf_from(get_resp)

    resp = await auth_client.post(
        "/my/preferences/",
        data={"language": "ru", "csrf_token": csrf},
        cookies={"access_token": auth_client.state.token, "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db_session.refresh(user)
    assert user.language == "ru"


async def test_preferences_rejects_unavailable_language(
    auth_client: AsyncClient, db_session: AsyncSession
):
    """An uninstalled code ('de', no catalog) leaves user.language unchanged."""
    user: User = auth_client.state.user
    original = user.language

    get_resp = await auth_client.get(
        "/my/preferences/", cookies={"access_token": auth_client.state.token}
    )
    csrf = _csrf_from(get_resp)

    resp = await auth_client.post(
        "/my/preferences/",
        data={"language": "de", "csrf_token": csrf},
        cookies={"access_token": auth_client.state.token, "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db_session.refresh(user)
    assert user.language == original
    assert user.language != "de"


# ---------------------------------------------------------------------------
# Per-user preference beats the admin default at render time
# ---------------------------------------------------------------------------


async def test_user_language_beats_admin_default_on_render(
    admin_client: AsyncClient, db_session: AsyncSession
):
    """With admin default 'fr' but user.language 'th', the page renders Thai.

    The preferences page contains a {% trans %}Save{% endtrans %} string;
    'th' translates it to 'บันทึก' while 'fr' would render 'Enregistrer'.
    """
    from specivo.core.runtime_settings import set_default_language_override

    # Workspace default is French.
    set_default_language_override("fr")

    # The authenticated admin prefers Thai.
    user: User = admin_client.state.user
    user.language = "th"
    db_session.add(user)
    await db_session.commit()

    resp = await admin_client.get(
        "/my/preferences/", cookies={"access_token": admin_client.state.token}
    )
    assert resp.status_code == 200
    assert "บันทึก" in resp.text  # Thai "Save"
    assert "Enregistrer" not in resp.text  # not French
