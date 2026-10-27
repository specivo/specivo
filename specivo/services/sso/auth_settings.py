"""Instance-wide sign-in settings stored in the ``settings`` table under ``sso.*`` keys.

Booleans are stored as ``"true"`` / ``"false"``; an unset key means the
default. These settings are stored and edited here. The login page and the
password login read the sign-in part through :func:`get_login_policy`; the
API key checks read :func:`get_auth_settings`.

``SSO_FORCE_LOCAL_LOGIN`` (env) always wins over ``hide_local_login``,
``auto_redirect_provider`` and ``local_login_admins_only``: it is the
break-glass switch for a broken IdP or a bad SSO configuration, and
:func:`get_login_policy` applies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import ValidationError
from specivo.models.sso import SsoProvider
from specivo.schemas.sso import AuthSettingsOut, AuthSettingsUpdate
from specivo.services.settings_service import SettingsService
from specivo.services.sso.log import logger

if TYPE_CHECKING:
    from specivo.models.user import User

__all__ = [
    "ALLOW_API_KEYS_FOR_SSO_USERS_KEY",
    "AUTH_SETTING_KEYS",
    "AUTO_REDIRECT_PROVIDER_KEY",
    "HIDE_LOCAL_LOGIN_KEY",
    "LOCAL_LOGIN_ADMINS_ONLY_KEY",
    "LoginPolicy",
    "get_auth_settings",
    "get_login_policy",
    "update_auth_settings",
]

HIDE_LOCAL_LOGIN_KEY = "sso.hide_local_login"
LOCAL_LOGIN_ADMINS_ONLY_KEY = "sso.local_login_admins_only"
AUTO_REDIRECT_PROVIDER_KEY = "sso.auto_redirect_provider"
ALLOW_API_KEYS_FOR_SSO_USERS_KEY = "sso.allow_api_keys_for_sso_users"

#: Field of :class:`AuthSettingsOut` -> settings key.
_FIELD_KEYS: dict[str, str] = {
    "hide_local_login": HIDE_LOCAL_LOGIN_KEY,
    "local_login_admins_only": LOCAL_LOGIN_ADMINS_ONLY_KEY,
    "auto_redirect_provider": AUTO_REDIRECT_PROVIDER_KEY,
    "allow_api_keys_for_sso_users": ALLOW_API_KEYS_FOR_SSO_USERS_KEY,
}
_BOOL_DEFAULTS: dict[str, bool] = {
    "hide_local_login": False,
    "local_login_admins_only": False,
    "allow_api_keys_for_sso_users": True,
}

#: Every key managed here; the generic settings API refuses them.
AUTH_SETTING_KEYS: frozenset[str] = frozenset(_FIELD_KEYS.values())

_settings_service = SettingsService()


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


async def get_auth_settings(session: AsyncSession) -> AuthSettingsOut:
    """The current authentication settings, with defaults for unset keys."""
    stored = await _settings_service.get_all(session)
    values: dict[str, object] = {
        field: _as_bool(stored.get(_FIELD_KEYS[field]), default) for field, default in _BOOL_DEFAULTS.items()
    }
    values["auto_redirect_provider"] = stored.get(AUTO_REDIRECT_PROVIDER_KEY) or None
    return AuthSettingsOut.model_validate(values)


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    """How the login page and the password login behave right now.

    The stored settings with ``SSO_FORCE_LOCAL_LOGIN`` applied: when the env
    switch is on, every restriction is off and ``forced_local`` says why.
    """

    hide_local_login: bool
    local_login_admins_only: bool
    auto_redirect_provider: str | None
    forced_local: bool


async def get_login_policy(session: AsyncSession) -> LoginPolicy:
    """The effective sign-in policy: the stored settings unless ``SSO_FORCE_LOCAL_LOGIN`` is on."""
    if get_settings().sso_force_local_login:
        return LoginPolicy(
            hide_local_login=False, local_login_admins_only=False, auto_redirect_provider=None, forced_local=True
        )
    stored = await get_auth_settings(session)
    return LoginPolicy(
        hide_local_login=stored.hide_local_login,
        local_login_admins_only=stored.local_login_admins_only,
        auto_redirect_provider=stored.auto_redirect_provider,
        forced_local=False,
    )


async def update_auth_settings(
    session: AsyncSession, data: AuthSettingsUpdate, *, actor: User | None = None
) -> AuthSettingsOut:
    """Apply the fields present in ``data``. The auto-redirect target must be an existing provider."""
    changes: dict[str, str | None] = {}
    for field in sorted(data.model_fields_set):
        value = getattr(data, field)
        if field == "auto_redirect_provider":
            if value is not None:
                exists = await session.execute(select(SsoProvider.id).where(SsoProvider.slug == value))
                if exists.scalar_one_or_none() is None:
                    raise ValidationError(f"No SSO provider with the slug '{value}'", field=field)
            changes[_FIELD_KEYS[field]] = value
        elif value is not None:
            changes[_FIELD_KEYS[field]] = "true" if value else "false"

    if changes:
        await _settings_service.set_many(session, changes)
        logger.info(
            "sso.auth_settings.updated actor=%s keys=%s",
            actor.id if actor is not None else None,
            ",".join(sorted(changes)),
        )
    return await get_auth_settings(session)


async def replace_auto_redirect_provider(session: AsyncSession, old_slug: str, new_slug: str | None) -> None:
    """Keep the auto-redirect setting pointing at a provider that was renamed (or clear it on delete)."""
    current = await _settings_service.get(session, AUTO_REDIRECT_PROVIDER_KEY)
    if current == old_slug:
        await _settings_service.set_many(session, {AUTO_REDIRECT_PROVIDER_KEY: new_slug})
        logger.info("sso.auth_settings.auto_redirect_followed old=%s new=%s", old_slug, new_slug)
