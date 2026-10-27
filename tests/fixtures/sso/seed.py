"""What the disposable IdPs in ``docker-compose.sso-test.yml`` are seeded with.

The Keycloak realm (``keycloak/specivo-test-realm.json``) and the authentik
blueprint (``authentik/specivo-test.yaml``) carry the same logical content.
Tests import the values from here instead of repeating them.

Every credential below is a fixed, public test value. None of them protects
anything: the IdPs run on localhost, keep their state in RAM and are discarded
by ``make sso-idp-down``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

KEYCLOAK_PORT = int(os.environ.get("SSO_KEYCLOAK_PORT", "8033"))
AUTHENTIK_PORT = int(os.environ.get("SSO_AUTHENTIK_PORT", "8034"))

REALM = "specivo-test"

# Overridable so the suite can point at an IdP on another host or port.
KEYCLOAK_ISSUER = os.environ.get("SSO_TEST_ISSUER", f"http://localhost:{KEYCLOAK_PORT}/realms/{REALM}")
KEYCLOAK_BASE_URL = KEYCLOAK_ISSUER.split("/realms/", 1)[0]
# Only set when authentik was started (make sso-idp-up-authentik), e.g.
# http://localhost:8034/application/o/specivo-test/
AUTHENTIK_ISSUER = os.environ.get("SSO_TEST_AUTHENTIK_ISSUER", "")

# Keycloak master-realm admin (KC_BOOTSTRAP_ADMIN_* in the compose file). Test-only.
KEYCLOAK_ADMIN_USER = "admin"
KEYCLOAK_ADMIN_PASSWORD = "admin-test-only"

# authentik API token (AUTHENTIK_BOOTSTRAP_TOKEN in the compose file). Test-only.
AUTHENTIK_API_TOKEN = "authentik-test-only-bootstrap-token"

CONFIDENTIAL_CLIENT_ID = "specivo"
CONFIDENTIAL_CLIENT_SECRET = "specivo-test-client-secret-not-a-real-secret"
PUBLIC_CLIENT_ID = "specivo-public"

# Provider slugs the redirect URIs are registered for.
KEYCLOAK_SLUG = "keycloak"
AUTHENTIK_SLUG = "authentik"

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
