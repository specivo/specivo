---
description: Connect Specivo to Keycloak — create a confidential OpenID Connect client with PKCE, register the redirect and back-channel logout addresses, send groups, fill in the provider in Specivo, and offboard people.
---

# Keycloak

This page connects Specivo to a Keycloak realm. Read [Single sign-on](index.md) first for the
concepts and what each Specivo setting does. The values below are the ones Specivo's own
compatibility tests run against.

Sample values: Specivo at `https://specivo.example.com` (`APP_URL`), Keycloak at
`https://keycloak.example.com`, realm `example`, provider slug `keycloak`.

## 1. Create the client in Keycloak

In the **Admin console**, select your realm, then **Clients → Create client**.

**General settings**

- **Client type**: OpenID Connect.
- **Client ID**: `specivo`. You will enter the same value in Specivo.

**Capability config**

- **Client authentication**: **On**. This makes it a confidential client with a secret.
- **Authorization**: Off.
- **Authentication flow**: tick **Standard flow** only. Untick **Direct access grants**, and leave
  implicit flow and service accounts off.

**Login settings**

- **Valid redirect URIs**: `https://specivo.example.com/auth/oidc/keycloak/callback/`. Enter it
  exactly as the Specivo form shows it. No wildcard. Keycloak matches the address as written.
- **Valid post logout redirect URIs**: `https://specivo.example.com/login/?logged_out=1`.
- **Web origins**: not needed for sign-in.

Save the client, then:

- **Advanced → Advanced settings → Proof Key for Code Exchange Code Challenge Method**: **S256**.
  This makes Keycloak require PKCE, which Specivo always sends.
- **Settings → Logout settings** (only if you want back-channel logout):
    - **Front channel logout**: Off.
    - **Backchannel logout URL**: `https://specivo.example.com/auth/oidc/keycloak/backchannel-logout/`
    - **Backchannel logout session required**: **On**. Specivo then ends only the browser session
      that logged out, not every session of that person.
    - **Backchannel logout revoke offline sessions**: Off.
- **Credentials**: copy the **Client secret**.

!!! note "Keycloak must reach Specivo for back-channel logout"
    Back-channel logout is a request from the Keycloak server to Specivo. The address has to
    resolve and be reachable from Keycloak, and Keycloak must trust Specivo's certificate.

## 2. Send the groups

Skip this step if you do not use groups in Specivo.

**Groups.** Keycloak does not put groups in tokens by default. Add a mapper to the client's own
scope:

1. **Clients → specivo → Client scopes → specivo-dedicated → Configure a new mapper → Group
   Membership**.
2. **Name**: `groups`. **Token Claim Name**: `groups`.
3. **Full group path**: **Off**. With it on, groups arrive as `/engineering/admins`; with it off
   they arrive as `admins`.
4. **Add to ID token**: **On**. Add to userinfo too if you plan to use
   **Also read claims from the userinfo endpoint**.

In Specivo the **Groups claim** is then `groups` (the default), and **Required groups**,
**Administrator groups** and **Group mappings** use the group names exactly as Keycloak names them.

**Roles instead of groups.** You can use roles. Keycloak's realm roles are in the `roles` client
scope, in the claim `realm_access.roles`, and client roles are in
`resource_access.<client ID>.roles`. Set the Specivo **Groups claim** to that dotted path, for
example `realm_access.roles`. Make sure the role mapper has **Add to ID token** on (open the
`roles` client scope under **Client scopes** and check its mappers), or turn on **Also read claims
from the userinfo endpoint** in Specivo.

## 3. Add the provider in Specivo

**Administration → Single sign-on → + New provider**:

| Field | Value |
|---|---|
| Button label | `Keycloak` |
| Slug | `keycloak` |
| Issuer | `https://keycloak.example.com/realms/example` |
| Client ID | `specivo` |
| Client authentication | `client_secret_basic` |
| Client secret | The secret from the **Credentials** tab |
| Scopes | `openid profile email` |
| Groups claim | `groups` (or `realm_access.roles`) |
| Check sessions at the provider when they are renewed | On (the default) |
| Accept back-channel logout | On, if you set it up in step 1 |

The issuer is the realm address. It has no trailing slash and no `/.well-known/...` part. Specivo
adds that itself. Do not add `offline_access` to the scopes: Keycloak issues the refresh token the
session check needs for `openid` already, and an offline token outlives a Keycloak sign-out.

Save, press **Test connection**, then tick **Enabled** and save again. The test should show
back-channel logout as supported. For the remaining choices (accounts, JIT, groups) see
[Single sign-on](index.md#who-gets-an-account).

## Notes for Keycloak

- **Email verification.** Keycloak sets `email_verified` from each user's *Email verified* flag,
  and by default users can edit their own email unless you turned that off in the realm's user
  profile. Only turn on **This provider really verifies email addresses** if your realm verifies
  email before it sets the flag (**Realm settings → Login → Verify email**), and consider turning
  off **Edit username** and email editing for users.
- **Subject.** Keycloak's `sub` is the user's internal ID. Deleting and recreating a user in
  Keycloak gives a new `sub`, and that person must be connected again.
- **Language.** If you want the interface language to follow Keycloak, add a mapper that puts the
  user's `locale` attribute into the ID token, and keep the **Language claim** as `locale`.
- **Logout.** Keycloak shows a confirmation page on logout unless it gets the `id_token_hint`,
  which Specivo sends. Make sure the post-logout redirect address from step 1 is registered, or
  Keycloak does not send the person back.
- **Realm session lifetimes.** With the session check on, a Specivo session ends when the Keycloak
  session behind it ends: **Realm settings → Sessions → SSO Session Idle** (30 minutes by default)
  and **SSO Session Max** (10 hours) apply as well as Specivo's **Maximum session length**, and the
  shortest decides when someone is asked to sign in again. Someone who leaves Specivo idle for
  longer than the idle timeout signs in again at their next visit. Raise the idle timeout if that
  is too often, or turn the check off for the provider.

## Offboarding

Keycloak has no built-in SCIM client, so [provisioning with SCIM](scim.md) is not available here;
use the steps below.

Keycloak sends **no** back-channel logout when you disable or delete a user. To take someone's access
away:

1. **Disable the user** (**Users → *user* → Enabled: off**), or delete them. Keycloak refuses their
   refresh token at once, so with the session check on their Specivo session ends at its next
   renewal, within 15 minutes.
2. To end it **at once**, also open the user's **Sessions** tab and press **Sign out**. With
   back-channel logout set up, Keycloak then tells Specivo, which ends the session immediately.
3. If you use API keys, choose **End all of their sessions and switch off their API keys** for
   **When the provider ends a session** (step 2 then switches off the keys of a person without a
   Specivo password), or deactivate the user in Specivo as well (**Administration → Users →
   Deactivate account**), which also works for people with a password.

Without the session check (turned off, or the client issues no refresh tokens) step 1 alone leaves
the Specivo session running until **Maximum session length**: always do step 2 then.

## If it does not work

| Symptom | Check |
|---|---|
| Keycloak shows "Invalid parameter: redirect_uri" | The redirect URI in Keycloak is not byte-identical to the one on the Specivo form. Check `APP_URL`, the slug and the trailing slash. |
| "Invalid parameter: code_challenge_method" or similar | Keycloak's client is set to a PKCE method other than S256. Set it to S256 or leave it unset. |
| `sso_idp_error` after Keycloak accepted the password | The client secret is wrong, or the client is public while Specivo sends a secret. |
| `sso_token_invalid` | The issuer does not match. Open `{issuer}/.well-known/openid-configuration` in a browser and compare `issuer` with the field. If Keycloak runs behind a proxy, its configured hostname must be the public one. |
| `sso_not_in_required_group` | The groups mapper is missing, **Add to ID token** is off, or the group names differ (full path on, or different case). |

The full list is in the [error table](index.md#troubleshooting).
