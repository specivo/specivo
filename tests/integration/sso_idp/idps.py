"""The disposable IdPs the ``sso_idp`` tests run against, behind one interface.

Each driver plays the browser at its IdP's sign-in pages (httpx, no browser)
and administers users through the IdP's admin API: create a throwaway user,
disable, delete or sign them out. Tests run once per IdP (the ``idp`` fixture
in ``conftest.py``); an IdP that is not up skips its half.

Where they differ, as the tests see it:

* Keycloak sends ``email_verified: true`` and the ``locale`` and ``zoneinfo``
  claims; authentik always sends ``email_verified: false`` and neither claim.
* Keycloak's ``sub`` is the user id; authentik's is a hash of it (the user's
  ``uid`` in its API).
* Keycloak has a public client as well; authentik only the confidential one.
* Keycloak issues a refresh token for ``openid``; authentik only with
  ``offline_access``, so its provider asks for that scope.
* Keycloak refuses the refresh token after a disable, a delete and an admin
  sign-out; authentik after a disable or a delete, but keeps an
  ``offline_access`` token valid when an admin ends the session (it sends a
  back-channel logout for it instead).
* Keycloak sends a back-channel logout only for an admin sign-out; authentik
  for a disable, a delete and an admin sign-out.

Every credential is a fixed test value from ``tests/fixtures/sso/seed.py``.
"""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote, urlsplit

import httpx

from tests.fixtures.sso import seed

_KC_FORM_ACTION = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', re.S)


@dataclass
class IdpUser:
    """A user at the IdP. ``ref`` is the IdP's own id for admin calls."""

    username: str
    email: str
    subject: str
    ref: str


class Idp:
    """What a test needs from an IdP."""

    name: ClassVar[str]
    slug: ClassVar[str]
    issuer: ClassVar[str]
    #: Scheme, host and port of the IdP.
    base_url: ClassVar[str]
    display_name: ClassVar[str]
    #: Scopes the Specivo provider asks for.
    scopes: ClassVar[str]
    email_verified: ClassVar[bool]
    sends_locale: ClassVar[bool]
    #: Whether an admin sign-out makes the IdP refuse the refresh token.
    signout_refuses_refresh: ClassVar[bool]

    async def subject(self, username: str) -> str:
        raise NotImplementedError

    async def sign_in(self, authorize_url: str, username: str, password: str = seed.USER_PASSWORD) -> str:
        """Play the browser from the authorize URL to the callback URL Specivo gets back."""
        raise NotImplementedError

    async def create_user(self) -> IdpUser:
        """A throwaway user in the Specivo users group, with the seeded password."""
        raise NotImplementedError

    async def disable(self, user: IdpUser) -> None:
        raise NotImplementedError

    async def delete(self, user: IdpUser) -> None:
        raise NotImplementedError

    async def sign_out(self, user: IdpUser) -> None:
        """End every IdP session of ``user`` as an administrator would."""
        raise NotImplementedError

    async def cleanup(self, user: IdpUser) -> None:
        """Delete ``user`` if it still exists."""
        raise NotImplementedError


def _username() -> str:
    return f"offboard-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Keycloak
# ---------------------------------------------------------------------------


class Keycloak(Idp):
    name = "keycloak"
    slug = seed.KEYCLOAK_SLUG
    issuer = seed.KEYCLOAK_ISSUER
    base_url = seed.KEYCLOAK_BASE_URL
    display_name = "Keycloak"
    scopes = "openid profile email"
    email_verified = True
    sends_locale = True
    signout_refuses_refresh = True

    async def _admin(self) -> httpx.AsyncClient:
        async with httpx.AsyncClient(timeout=10.0) as http:
            token = await http.post(
                f"{seed.KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": seed.KEYCLOAK_ADMIN_USER,
                    "password": seed.KEYCLOAK_ADMIN_PASSWORD,
                },
            )
            assert token.status_code == 200, token.text
        return httpx.AsyncClient(
            base_url=f"{seed.KEYCLOAK_BASE_URL}/admin/realms/{seed.REALM}",
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            timeout=10.0,
        )

    async def subject(self, username: str) -> str:
        async with await self._admin() as admin:
            users = await admin.get("/users", params={"username": username, "exact": "true"})
            assert users.status_code == 200, users.text
            [user] = users.json()
            return str(user["id"])

    async def sign_in(self, authorize_url: str, username: str, password: str = seed.USER_PASSWORD) -> str:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as browser:
            form = await browser.get(authorize_url)
            assert form.status_code == 200, form.text
            match = _KC_FORM_ACTION.search(form.text)
            assert match, "Keycloak login form not found"
            # Keycloak marks its cookies Secure (SameSite=None), which httpx never
            # sends over plain http, so carry them by hand like a browser on localhost.
            cookies = "; ".join(
                header.split(";", 1)[0] for header in form.headers.get_list("set-cookie") if "=" in header
            )
            submitted = await browser.post(
                html.unescape(match.group(1)),
                data={"username": username, "password": password, "credentialId": ""},
                headers={"Cookie": cookies},
            )
            assert submitted.status_code == 302, submitted.text
            return submitted.headers["location"]

    async def create_user(self) -> IdpUser:
        username = _username()
        email = f"{username}@example.com"
        async with await self._admin() as admin:
            created = await admin.post(
                "/users",
                json={
                    "username": username,
                    "email": email,
                    "firstName": "Offboard",
                    "lastName": "Test",
                    "enabled": True,
                    "emailVerified": True,
                    "groups": [f"/{seed.GROUP_USERS}"],
                    "credentials": [{"type": "password", "value": seed.USER_PASSWORD, "temporary": False}],
                },
            )
            assert created.status_code == 201, created.text
            ref = created.headers["location"].rsplit("/", 1)[1]
        return IdpUser(username=username, email=email, subject=ref, ref=ref)

    async def disable(self, user: IdpUser) -> None:
        async with await self._admin() as admin:
            resp = await admin.put(f"/users/{user.ref}", json={"enabled": False})
            assert resp.status_code == 204, resp.text

    async def delete(self, user: IdpUser) -> None:
        async with await self._admin() as admin:
            resp = await admin.delete(f"/users/{user.ref}")
            assert resp.status_code == 204, resp.text

    async def sign_out(self, user: IdpUser) -> None:
        async with await self._admin() as admin:
            sessions = await admin.get(f"/users/{user.ref}/sessions")
            assert sessions.status_code == 200, sessions.text
            assert sessions.json(), f"{user.username} has no Keycloak session to sign out"
            resp = await admin.post(f"/users/{user.ref}/logout")
            assert resp.status_code == 204, resp.text

    async def cleanup(self, user: IdpUser) -> None:
        async with await self._admin() as admin:
            await admin.delete(f"/users/{user.ref}")


# ---------------------------------------------------------------------------
# authentik
# ---------------------------------------------------------------------------


class Authentik(Idp):
    name = "authentik"
    slug = seed.AUTHENTIK_SLUG
    issuer = seed.AUTHENTIK_ISSUER
    base_url = seed.AUTHENTIK_BASE_URL
    display_name = "authentik"
    scopes = "openid profile email offline_access"
    email_verified = False
    sends_locale = False
    signout_refuses_refresh = False

    def _admin(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{seed.AUTHENTIK_BASE_URL}/api/v3",
            headers={"Authorization": f"Bearer {seed.AUTHENTIK_API_TOKEN}"},
            timeout=10.0,
        )

    async def _user(self, username: str) -> dict:
        async with self._admin() as admin:
            found = await admin.get("/core/users/", params={"username": username})
            assert found.status_code == 200, found.text
            [user] = found.json()["results"]
            return dict(user)

    async def subject(self, username: str) -> str:
        # The default subject mode: the user's hashed id, which the API calls uid.
        return str((await self._user(username))["uid"])

    async def sign_in(self, authorize_url: str, username: str, password: str = seed.USER_PASSWORD) -> str:
        """Drive authentik's flow executor API, which its sign-in pages are built on."""
        base = seed.AUTHENTIK_BASE_URL
        async with httpx.AsyncClient(base_url=base, timeout=20.0) as browser:
            url = authorize_url
            for _ in range(12):
                response = await browser.get(url)
                assert response.status_code in (301, 302), f"{response.status_code} at {url}: {response.text[:300]}"
                location = response.headers["location"]
                if not location.startswith(base) and "://" in location:
                    return location  # back to the relying party
                url = location if "://" in location else base + location
                if "/if/flow/" in url:
                    url = await self._run_flow(browser, url, username, password)
            raise AssertionError("authentik never redirected back to Specivo")

    @staticmethod
    async def _run_flow(browser: httpx.AsyncClient, url: str, username: str, password: str) -> str:
        split = urlsplit(url)
        slug = split.path.split("/if/flow/", 1)[1].strip("/")
        api = f"/api/v3/flows/executor/{slug}/?query={quote(split.query)}"
        challenge = (await browser.get(api)).json()
        for _ in range(8):
            component = challenge.get("component")
            if component == "xak-flow-redirect":
                to = str(challenge["to"])
                return to if "://" in to else seed.AUTHENTIK_BASE_URL + to
            if component == "ak-stage-identification":
                body = {"component": component, "uid_field": username}
                if challenge.get("password_fields"):
                    body["password"] = password
            elif component == "ak-stage-password":
                body = {"component": component, "password": password}
            else:
                raise AssertionError(f"unexpected authentik stage {component}: {challenge}")
            csrf = browser.cookies.get("authentik_csrf") or ""
            response = await browser.post(api, json=body, headers={"X-authentik-CSRF": csrf})
            for _hop in range(5):
                if response.status_code not in (301, 302):
                    break
                response = await browser.get(response.headers["location"])
            assert response.status_code == 200, response.text
            challenge = response.json()
        raise AssertionError("authentik flow did not finish")

    async def create_user(self) -> IdpUser:
        username = _username()
        email = f"{username}@example.com"
        async with self._admin() as admin:
            groups = await admin.get("/core/groups/", params={"name": seed.GROUP_USERS})
            [group] = groups.json()["results"]
            created = await admin.post(
                "/core/users/",
                json={
                    "username": username,
                    "name": "Offboard Test",
                    "email": email,
                    "path": "users",
                    "groups": [group["pk"]],
                    "is_active": True,
                },
            )
            assert created.status_code == 201, created.text
            user = created.json()
            password = await admin.post(
                f"/core/users/{user['pk']}/set_password/", json={"password": seed.USER_PASSWORD}
            )
            assert password.status_code == 204, password.text
        return IdpUser(username=username, email=email, subject=str(user["uid"]), ref=str(user["pk"]))

    async def disable(self, user: IdpUser) -> None:
        async with self._admin() as admin:
            resp = await admin.patch(f"/core/users/{user.ref}/", json={"is_active": False})
            assert resp.status_code == 200, resp.text

    async def delete(self, user: IdpUser) -> None:
        async with self._admin() as admin:
            resp = await admin.delete(f"/core/users/{user.ref}/")
            assert resp.status_code == 204, resp.text

    async def sign_out(self, user: IdpUser) -> None:
        async with self._admin() as admin:
            sessions = await admin.get("/core/authenticated_sessions/", params={"user__username": user.username})
            assert sessions.status_code == 200, sessions.text
            results = sessions.json()["results"]
            assert results, f"{user.username} has no authentik session to end"
            for session in results:
                resp = await admin.delete(f"/core/authenticated_sessions/{session['uuid']}/")
                assert resp.status_code == 204, resp.text

    async def cleanup(self, user: IdpUser) -> None:
        async with self._admin() as admin:
            await admin.delete(f"/core/users/{user.ref}/")


IDPS: dict[str, type[Idp]] = {"keycloak": Keycloak, "authentik": Authentik}
