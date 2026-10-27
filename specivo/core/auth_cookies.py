"""The browser auth cookies, shared by password login (JSON API) and SSO (web).

Both cookies are HttpOnly and SameSite=Lax, ``Secure`` unless ``DEBUG``.
Without *remember* they are session cookies; with it they persist for the
token lifetimes, and *refresh_max_age* can shorten the refresh cookie (an SSO
session never outlives its absolute cap).

``TokenRefreshMiddleware`` builds the same attributes as raw headers in
``specivo.core.middleware._build_auth_cookie_headers``.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import Response

from specivo.core.config import Settings

__all__ = [
    "ACCESS_COOKIE",
    "REFRESH_COOKIE",
    "REFRESH_COOKIE_PATH",
    "clear_auth_cookies",
    "set_auth_cookies",
]

# NOTE: Plain names ("access_token", "refresh_token") so existing sessions keep
# working. The ``__Host-`` prefix (e.g. ``__Host-access_token``) would bind the
# cookie to the origin (requires Secure, Path=/, no Domain); changing the name
# invalidates every active session.
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
REFRESH_COOKIE_PATH = "/"


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    settings: Settings,
    *,
    remember: bool = False,
    refresh_max_age: int | None = None,
) -> None:
    """Attach the HttpOnly auth cookies for browser clients.

    When *remember* is ``False`` (the default), cookies are set without
    ``max_age`` so the browser discards them when it closes. When ``True``,
    persistent ``max_age`` values are used; *refresh_max_age*, if given, caps
    the refresh cookie's.
    """
    access_kwargs: dict[str, Any] = dict(
        key=ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
        path="/",
    )
    refresh_kwargs: dict[str, Any] = dict(
        key=REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
        path=REFRESH_COOKIE_PATH,
    )
    if remember:
        access_kwargs["max_age"] = settings.access_token_expire_minutes * 60
        refresh_age = settings.refresh_token_expire_days * 86400
        if refresh_max_age is not None:
            refresh_age = max(0, min(refresh_age, refresh_max_age))
        refresh_kwargs["max_age"] = refresh_age
    response.set_cookie(**access_kwargs)
    response.set_cookie(**refresh_kwargs)


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(key=ACCESS_COOKIE, path="/")
    response.delete_cookie(key=REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
