"""What the disposable IdP in ``tests/fixtures/sso/docker-compose.yml`` are seeded with.

The Keycloak realm (``keycloak/specivo-test-realm.json``) and the authentik
blueprint (``authentik/specivo-test.yaml``) carry this content: the same users,
groups, client id and secret. Tests import the values from here instead of
repeating them.

Every credential below is a fixed, public test value. None of them protects
anything: the IdP runs on localhost, keep their state in RAM and are discarded
by ``make sso-idp-down``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

KEYCLOAK_PORT = int(os.environ.get("SSO_KEYCLOAK_PORT", "8033"))

REALM = "specivo-test"

# Overridable so the suite can point at an IdP on another host or port.
KEYCLOAK_ISSUER = os.environ.get("SSO_TEST_ISSUER", f"http://localhost:{KEYCLOAK_PORT}/realms/{REALM}")
KEYCLOAK_BASE_URL = KEYCLOAK_ISSUER.split("/realms/", 1)[0]

# Keycloak master-realm admin (KC_BOOTSTRAP_ADMIN_* in the compose file). Test-only.
KEYCLOAK_ADMIN_USER = "admin"
KEYCLOAK_ADMIN_PASSWORD = "admin-test-only"

CONFIDENTIAL_CLIENT_ID = "specivo"
CONFIDENTIAL_CLIENT_SECRET = "specivo-test-client-secret-not-a-real-secret"
PUBLIC_CLIENT_ID = "specivo-public"

# Provider slug the redirect URIs are registered for.
KEYCLOAK_SLUG = "keycloak"

# authentik (profile "authentik", ``make sso-authentik-up``). Same client id and
# secret as Keycloak; only the confidential client exists there.
AUTHENTIK_PORT = int(os.environ.get("SSO_AUTHENTIK_PORT", "8036"))
AUTHENTIK_APP_SLUG = "specivo-test"
AUTHENTIK_BASE_URL = f"http://localhost:{AUTHENTIK_PORT}"
AUTHENTIK_ISSUER = f"{AUTHENTIK_BASE_URL}/application/o/{AUTHENTIK_APP_SLUG}/"
AUTHENTIK_SLUG = "authentik"
# Admin API token (AUTHENTIK_BOOTSTRAP_TOKEN in the compose file), e.g. to
# deactivate a user. Test-only.
AUTHENTIK_API_TOKEN = "authentik-test-only-api-token"
# authentik's default email scope mapping always sends ``email_verified: false``,
# so every seeded user arrives unverified there, and ``locale``/``zoneinfo`` are
# not sent at all.

E2E_BASE_URL = "http://localhost:9944"
ASGI_BASE_URL = "http://testserver"
POST_LOGOUT_REDIRECT_URI = f"{E2E_BASE_URL}/login/?logged_out=1"

GROUP_USERS = "specivo-users"
GROUP_ADMINS = "specivo-admins"

# One password for every seeded user. Test-only.
USER_PASSWORD = "sso-test-password"


def callback_url(base_url: str, slug: str) -> str:
    return f"{base_url}/auth/oidc/{slug}/callback/"


def backchannel_logout_url(base_url: str, slug: str) -> str:
    return f"{base_url}/auth/oidc/{slug}/backchannel-logout/"


@dataclass(frozen=True)
class SeedUser:
    username: str
    email: str
    email_verified: bool
    groups: tuple[str, ...]
    locale: str | None = None
    zoneinfo: str | None = None


SSO_MEMBER = SeedUser("sso-member", "sso-member@example.com", True, (GROUP_USERS,))
SSO_ADMIN = SeedUser("sso-admin", "sso-admin@example.com", True, (GROUP_USERS, GROUP_ADMINS))
# Valid at the IdP, in none of the Specivo groups: required_groups must deny it.
SSO_OUTSIDER = SeedUser("sso-outsider", "sso-outsider@example.com", True, ())
SSO_UNVERIFIED = SeedUser("sso-unverified", "sso-unverified@example.com", False, (GROUP_USERS,))
# Same email as a local test user created with ``UserFactory(login="collide")``.
SSO_COLLIDE = SeedUser("sso-collide", "collide@example.com", True, (GROUP_USERS,))
SSO_THAI = SeedUser("sso-thai", "sso-thai@example.com", True, (GROUP_USERS,), locale="th", zoneinfo="Asia/Bangkok")

SEED_USERS: tuple[SeedUser, ...] = (SSO_MEMBER, SSO_ADMIN, SSO_OUTSIDER, SSO_UNVERIFIED, SSO_COLLIDE, SSO_THAI)
