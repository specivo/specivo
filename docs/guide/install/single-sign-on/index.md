---
description: Let people sign in to Specivo through your OpenID Connect identity provider. How single sign-on works, the provider settings, account linking, groups, logout, and a table of the errors people can see.
---

# Single sign-on

Single sign-on (SSO) lets people sign in to Specivo with the account they already have at your
identity provider (IdP), such as Keycloak, authentik or Microsoft Entra ID.

This page explains how it works and what every setting does. For the steps at a specific provider,
use its page:

- [Keycloak](keycloak.md)
- [authentik](authentik.md)
- [Microsoft Entra ID](entra-id.md)
- [Okta, Google, Zitadel and Authelia](other-providers.md)

## What Specivo supports

- **OpenID Connect only.** Specivo is an OpenID Connect *relying party*: it asks your IdP who the
  person is. It is never an IdP itself and does not issue identities to other applications.
  SAML is not supported. If your IdP speaks only SAML, put an IdP in front of it that bridges SAML
  to OpenID Connect (Keycloak and authentik can).
- **Several providers at once.** Each provider gets its own button on the login page.
- **Local accounts stay.** Passwords keep working unless you hide or restrict them, and you can
  always get back in as an administrator. See
  [Getting back in](../configuration.md#single-sign-on-getting-back-in).
- **Humans only.** Service accounts (the accounts AI agents use) can never sign in through SSO.
  API keys are not affected by it.
- **No directory sync.** Specivo does not support SCIM or LDAP. People appear when they first sign
  in, and leave through the steps in [Offboarding people](#offboarding-people).

## Before you start

1. **Serve Specivo over HTTPS** at its public address. Most IdPs refuse plain `http://`
   redirect addresses, and Specivo refuses `http://` IdPs unless you opt in (see
   [Settings in the environment](#settings-in-the-environment)).
2. **Set `APP_URL` to that public address**, for example `APP_URL=https://specivo.example.com`
   in `.env`, and recreate the containers (`docker compose up -d`). Every address Specivo gives
   your IdP is built from `APP_URL`. It is never taken from the request, so behind a reverse proxy
   nothing else needs to change. Do this first: a wrong `APP_URL` is the most common cause of a
   failed first sign-in.
3. **Keep query strings out of your proxy's access logs.** The redirect back from the IdP carries
   a one-time `code` and `state` in its query string. The bundled nginx logs paths without query
   strings. If another proxy sits in front (Caddy, Traefik, Cloudflare, a load balancer), configure
   its access log to leave out the query string too, at least for `/auth/oidc/`. For the per-address
   rate limits behind such a proxy, see
   [Client addresses behind a proxy](../configuration.md#client-addresses-behind-a-proxy).
4. **Make sure you can sign in as an administrator with a password.** Keep that account while you
   test.

## Setting up a provider: the outline

The steps are the same for every IdP. Only the screens differ.

1. In Specivo, open **Administration → Single sign-on** and press **+ New provider**. Choose a
   **Slug** (lowercase letters, digits and `-`, 2 to 40 characters) such as `keycloak`. The
   **Connection** card shows two addresses built from it:

    | Address | Format |
    |---|---|
    | Redirect URI | `{APP_URL}/auth/oidc/{slug}/callback/` |
    | Back-channel logout URI | `{APP_URL}/auth/oidc/{slug}/backchannel-logout/` |

    For example `https://specivo.example.com/auth/oidc/keycloak/callback/`. The trailing slash is
    part of the address. If Specivo is served under a path prefix (`STEALTH_PREFIX`), the prefix
    comes right after `{APP_URL}` in these addresses and in the post-logout address below. The
    **Connection** card shows the exact addresses, prefix included: copy them from there.

2. At the IdP, create a client (an "application") for Specivo and register the redirect URI
   **exactly as shown**. Do not use wildcards. If the IdP asks for a post-logout redirect
   address, register `{APP_URL}/login/?logged_out=1`. If you want back-channel logout, register
   the back-channel logout URI too.
3. Copy the **issuer**, the **client ID** and the **client secret** from the IdP into Specivo.
4. Save, then press **Test connection**. Specivo fetches the IdP's discovery document and signing
   keys and reports what it found (see [Test connection](#test-connection)).
5. Tick **Enabled (shown on the login page)** and save. A new provider starts disabled, so nobody
   sees a half-configured button.
6. Open a private browser window and sign in through the new button.

!!! warning "Changing the slug changes the addresses"
    The slug is part of the redirect URI and the back-channel logout URI. If you rename a provider,
    update both addresses at the IdP, or sign-in at that provider fails. If the provider was
    selected under **Redirect the login page to**, that setting follows the new slug.

## Provider settings

The provider form has five cards. The tables list every field.

### Connection

| Field | What it does |
|---|---|
| Button label | The name on the login button: "Sign in with *label*". |
| Slug | Part of the addresses above. Unique. |
| Issuer | The IdP's issuer address, copied character for character. It must equal the `issuer` the IdP publishes and the `iss` in every ID token. A trailing slash, `http` instead of `https`, or a different host makes it fail. |
| Discovery URL | Optional. Empty means `{issuer}/.well-known/openid-configuration`. Set it only if your IdP publishes its configuration elsewhere. |
| Client ID | The client ID registered at the IdP. |
| Client authentication | How Specivo proves itself at the token endpoint: `client_secret_basic` (default), `client_secret_post`, or `none` for a public client that relies on PKCE alone. Use `client_secret_basic` unless your IdP's documentation says otherwise. |
| Client secret | See [The client secret](#the-client-secret). Not used with `none`. |
| Scopes | Space-separated. Default `openid profile email`. `openid` is required and is always sent first. Add `groups` or other scopes your IdP needs to release a claim. |
| Extra sign-in parameters | Optional `prompt`, `domain_hint`, `login_hint`, `acr_values` and `max_age`, added to the sign-in request. Nothing else can be set here. With `max_age`, the ID token must carry an `auth_time` no older than `max_age` seconds (plus `SSO_CLOCK_SKEW_SECONDS`), or the sign-in is refused with `sso_token_invalid`. **Connect** from the profile always sends `prompt=login` and `max_age=0`, whatever is set here. |
| Enabled | Shows the button on the login page. A provider cannot be enabled while a confidential client has no secret. Turning it off signs out everyone whose session came from this provider (see [Sessions](#sessions-and-logout)). |
| Button order | Lower numbers come first. |

Specivo always uses the authorization code flow with PKCE (`S256`), `state` and `nonce`, even for
confidential clients. Your IdP must allow PKCE with `S256`. ID tokens must be signed with an
asymmetric algorithm (RS256, PS256, ES256 and similar). Tokens signed with a shared secret
(`HS256`) are always rejected.

### The client secret

The secret is write-only. Specivo shows only its status:

| Status | Meaning |
|---|---|
| Stored (encrypted) | Saved in the database, encrypted with a key derived from `SECRET_KEY`. |
| Read from the environment variable | The provider points at an environment variable that is set. |
| The environment variable is not set | The provider points at a variable the containers do not have. |
| This environment variable name is not allowed | The name does not start with `SPECIVO_SSO_SECRET_`. Specivo never reads it. |
| Unreadable after a SECRET_KEY change: enter it again | See below. |
| Not set | Nothing is configured. |
| Not needed for a public client | Client authentication is `none`. |

Pick one source:

- **Store it in Specivo.** Paste the secret into the field. Leave the field empty on later saves to
  keep the current one.
- **Read it from an environment variable.** Enter a name that starts with `SPECIVO_SSO_SECRET_`,
  followed by capital letters, digits and underscores, for example `SPECIVO_SSO_SECRET_KEYCLOAK`.
  Set it in `.env.local` and recreate the containers. Set it for every Specivo container that
  serves requests. Other names are refused, so a provider setting can never be used to read
  `SECRET_KEY` or `DATABASE_URL`. This keeps the secret out of the database and out of database
  backups.

!!! warning "Rotating SECRET_KEY makes stored secrets unreadable"
    The stored secret is encrypted with a key derived from `SECRET_KEY`. If you change
    `SECRET_KEY`, every stored client secret becomes unreadable and the provider list shows
    **Unreadable, re-enter it**. Open each provider, paste the secret again and save. Secrets read
    from environment variables are not affected.

### Sign-in policy

| Field | What it does |
|---|---|
| Create an account on first sign-in | Just-in-time (JIT) provisioning. **Off by default.** See [Who gets an account](#who-gets-an-account). |
| Existing accounts | How a person with a Specivo account gets connected to their IdP identity: users connect it themselves (default), or automatic linking by verified email. See [Linking an existing account](#linking-an-existing-account). |
| This provider really verifies email addresses | Tells Specivo it may believe the `email_verified` claim. Required for automatic linking. Off by default. **Never turn it on for Microsoft Entra ID.** |
| Allowed email domains | One domain per line. Empty allows every domain. See [Limiting who can sign in](#limiting-who-can-sign-in). |
| Maximum session length (hours) | The longest a sign-in through this provider lasts, from 1 to 168 (one week). Default 24. See [Sessions](#sessions-and-logout). |

### Claims

Which claims in the ID token fill the Specivo profile. Leave a field empty to use the standard
claim.

| Field | Default | Used for |
|---|---|---|
| Username claim | `preferred_username` | The login name of a newly created account. |
| Email claim | `email` | The email address. |
| Name claim | `name` | The display name. |
| Language claim | `locale` | The interface language. `th`, `th-TH` and `th_TH` all map to `th`. A value Specivo has no translation for is ignored. |
| Groups claim | `groups` | The groups or roles used for [group mapping](#groups-and-roles). A dotted path such as `groups`, `realm_access.roles` or `resource_access.specivo.roles`. |
| Also read claims from the userinfo endpoint | off | Merges the IdP's userinfo response into the claims. Use it when the IdP leaves claims out of the ID token. The subject in userinfo must match the ID token or the sign-in is refused. |

!!! warning "Keep the email claim as `email` if you rely on email verification"
    `email_verified` vouches for the standard `email` claim only. If you set **Email claim** to
    anything else (`upn`, a custom attribute), Specivo always treats the address as unverified, even
    with **This provider really verifies email addresses** on. The email domain list then refuses
    everyone, a person's Specivo email does not follow the IdP, accounts created on first sign-in are
    created with an unverified email, and automatic linking never applies.

### Groups

Required groups, administrator groups and the group mappings. See
[Groups and roles](#groups-and-roles).

### Session check

| Field | What it does |
|---|---|
| Check sessions at the provider when they are renewed | **On by default.** Before Specivo renews a session (every 15 minutes while someone is active), it asks the IdP whether the person may still sign in. Someone disabled, deleted or signed out there loses their Specivo session at the next renewal. Needs a refresh token from the IdP; see [The session check](#the-session-check). |
| If the provider cannot be reached | **Keep the session** (default): the renewal goes ahead and the next one asks again. Someone who can block the traffic between Specivo and the IdP can keep a session this way, but never past **Maximum session length**. **End the session**: everybody from this provider is signed out while it is down. Local sign-in always keeps working. |

### Logout

| Field | What it does |
|---|---|
| Signing out of Specivo also signs out at the provider | RP-initiated logout, on by default. See [Sessions and logout](#sessions-and-logout). |
| Accept back-channel logout from the provider | Off by default. See [Sessions and logout](#sessions-and-logout). |
| When the provider ends a session | What a back-channel logout ends: the matching session (default), every session of that person from this provider, or all of their sessions and their API keys. See [What a back-channel logout ends](#what-a-back-channel-logout-ends). |

## Test connection

Press **Test connection** on a saved provider. Specivo fetches the discovery document and the
signing keys right now, stores nothing, and shows:

- the addresses it found (authorization, token, userinfo and end-session endpoints),
- whether the issuer reported by the IdP matches the one you entered,
- the signing algorithms it can use, the client authentication methods the IdP offers, and how many
  signing keys it published,
- whether the IdP advertises back-channel logout and the refresh token grant,
- whether sessions are checked at the provider, as the last sign-in found out: **Works**, **Not
  available: the provider sent no refresh token**, **Not available: the provider refused the
  refresh token**, or **Not known yet** when nobody has signed in since the check was turned on.
  Only a real sign-in gets a refresh token, so the test cannot try it itself,
- warnings for settings that work but probably are not what you want: the IdP does not advertise
  the chosen client authentication method or PKCE, sign-out at the provider is on but the IdP has
  no end-session endpoint, back-channel logout is on but the IdP does not support it, userinfo is
  on but the IdP has none, the client secret is missing, unreadable or points at a bad
  environment variable, or the session check is on but cannot work (see
  [The session check](#the-session-check)).

The test does not sign anyone in. It proves that Specivo can reach the IdP and that the issuer is
right. It cannot prove that the client ID, the secret or the redirect URI are right. Only a real
sign-in does.

## How people sign in

The login page shows one **Sign in with *label*** button per enabled provider, above the password
form. After the IdP confirms the person, Specivo starts its own session, the same kind a password
sign-in starts. The IdP's tokens are not kept, apart from the ID token (encrypted, on the session,
so that sign-out can tell the IdP which session to end).

The identity Specivo recognises is the pair **(issuer, subject)**: the IdP's address plus the
person's permanent ID there. It is never the email address. Changing someone's email at the IdP
does not move their Specivo account, and nobody can take over an account by changing their email to
match.

### Who gets an account

For a person with no connected account, Specivo decides in this order:

1. If the (issuer, subject) is already connected to a Specivo user, that user signs in.
2. Otherwise, if [automatic linking](#linking-an-existing-account) applies, the identity is linked
   to the matching user.
3. Otherwise, if **Create an account on first sign-in** is on, a new account is created. It has no
   password, is active, and takes its login name from the username claim (cleaned up to lowercase
   letters, digits, `_` and `-`, with a number added if the name is taken). A missing email stops it
   (`sso_email_missing`). An email that another account already uses stops it too
   (`sso_email_conflict`): the person signs in with their password and connects the account from
   their profile instead. A new account is never linked to an existing one through its email.
4. Otherwise the sign-in is refused with `sso_account_not_provisioned`.

Just-in-time creation is off by default. Turn it on when everyone your IdP lets in should get an
account, and combine it with [required groups](#groups-and-roles) or an
[email domain list](#limiting-who-can-sign-in) when they should not.

An account can be refused even when it is connected: deactivated accounts, accounts locked by an
administrator, accounts still waiting for email verification, and service accounts cannot sign in
through SSO (`sso_account_disabled`). A lockout
from repeated wrong passwords does not block SSO, because it protects the password only.

### Linking an existing account

Most installs start with local accounts. There are two ways to connect them to the IdP.

**Manual (default).** The person signs in with their password, opens **Profile → Connected
accounts** (in the account menu), and presses **Connect *label***. The IdP asks them to sign in again even if they
already have a session there (Specivo sends `prompt=login` and `max_age=0`), so a browser left
signed in at the IdP cannot be used to connect somebody else's account. After signing in at the
IdP they return to their profile with the account connected. From then on the button on the login page works for them.
They can press **Disconnect** later, but only if they keep another way in (a password or another
connected account). An administrator can disconnect any identity from the user's admin page.
Disconnecting ends the sessions that were started through that identity.

An identity can belong to one user only (`sso_identity_in_use`), and a user can connect one
account per provider (`sso_provider_already_linked`).

**Automatic by verified email.** Choose **Link automatically by verified email (never for
administrators)** and turn on **This provider really verifies email addresses**. At a person's
first sign-in Specivo links the identity to a local account only if **all** of these are true:

- the ID token has `email_verified` set to the boolean `true` (the text `"true"` does not count),
- exactly one local user has that email address, ignoring case,
- that user's own email address is marked verified in Specivo,
- that user is not an administrator, a service account or the anonymous user,
- that user has no connected account at this provider yet.

Anything else falls through to the steps in [Who gets an account](#who-gets-an-account).

!!! danger "Why automatic linking is off by default"
    Matching on email is how accounts get taken over. If the IdP lets a person set an unverified
    email, or lets a user of another tenant claim yours, whoever types `alice@example.com` into
    their profile signs in as Alice. Turn this on only for an IdP you run yourself, that verifies
    email addresses before it sends `email_verified: true`. Administrators are never linked
    automatically, so a misconfigured IdP cannot capture an admin account. **Never enable
    automatic linking or "really verifies email addresses" for Microsoft Entra ID**: its `email`
    claim is not verified and the user can change it.

### What follows the IdP on every sign-in

When a person signs in again, Specivo updates:

- the **display name**,
- the **language**, from the language claim,
- the **email address**, **only** when the provider is trusted (**This provider really verifies
  email addresses**), the token says `email_verified: true` and the address comes from the standard
  `email` claim. The new address is skipped if
  another account already has it. Without that trust, a person's Specivo email does not follow the
  IdP, because anyone who can edit their email at the IdP could otherwise change it here.
- the **administrator flag** and **group memberships**, if you turned on group sync.

Nothing is updated when a person connects an account from their profile.

### Limiting who can sign in

Two checks run before an account is created, linked or signed in, so a refused person is never
provisioned.

**Allowed email domains.** One domain per line, matched exactly and ignoring case. A subdomain does
not match its parent. The domain comes from:

- Google's `hd` (hosted domain) claim, only when the issuer is exactly `https://accounts.google.com`.
  Google sets it from Workspace membership. The same claim from any other IdP is ignored.
- Otherwise the email address, and **only when the provider is trusted** (**This provider really
  verifies email addresses**), the token says `email_verified: true` and **Email claim** is the
  standard `email`. Where users can edit their
  own email, an unverified address proves nothing, so an unverified address is refused.

A person who fails the check gets `sso_domain_not_allowed`. This means the list does nothing useful
for an IdP whose emails are not verified, such as Entra ID. Use required groups there.

**Required groups.** See below.

## Groups and roles

Specivo can read a groups or roles claim from the ID token and use it three ways. All matching is
exact and case-sensitive: `Specivo-Admins` and `specivo-admins` are different groups.

Set **Groups claim** to the claim's name or a dotted path to it. The value must be a list of
strings or a single string. Anything else counts as missing.

| Setting | Effect |
|---|---|
| **Required groups** | Only people in at least one of these groups may sign in. Empty lets everyone from this provider in. Someone with none of them gets `sso_not_in_required_group`. A token with no groups claim at all counts as having no groups, so it is refused too. |
| **Administrator groups** with **Set the administrator flag from the administrator groups at every sign-in** | People in these groups are administrators in Specivo. People who are not lose the flag at their next sign-in. A person is never demoted if that would leave no other active administrator who can sign in. The demotion is skipped and a warning is logged. |
| **Group mappings** with **Update Specivo group memberships from the mappings below at every sign-in** | Each row maps one IdP group to one Specivo [user group](../../projects/index.md). People in the IdP group are added to the Specivo group, and removed when they leave it. Project roles come from the Specivo group's own project memberships, so one mapping can grant a role in many projects. |

Group mappings can be added once the provider is saved. Create the Specivo group first under
**Administration → Groups**.

Sync only touches what it created. Memberships that an administrator added by hand, or that another
provider manages, are never removed.

!!! note "A missing claim never removes anyone"
    If the groups claim is missing from a token, which happens when an IdP mapper breaks, Specivo
    changes no memberships and no administrator flags. It removes a membership only when the claim
    is present and no longer contains the group. If **Required groups** is set, a missing claim
    still refuses the sign-in.

!!! warning "Microsoft Entra ID sends too many groups"
    When a user is in more groups than fit in a token, Entra ID sends a marker instead of the list
    (the "group overage"). Specivo cannot read the real list. If required groups or administrator
    groups decide access, it refuses the sign-in with `sso_groups_overage`. Otherwise it signs the
    person in and changes no memberships. The fix is in the IdP: see
    [Microsoft Entra ID](entra-id.md#group-overage).

## Sessions and logout

**Session length.** A sign-in through a provider lasts at most **Maximum session length** (default
24 hours, at most 168 hours, one week), even if the person keeps using Specivo. After that they go
through the IdP again. With [the session check](#the-session-check) working, a person disabled at
the IdP is stopped much sooner; without it, this number is how long they can keep working in
Specivo, unless back-channel logout ends the session first.

**A session follows its provider and its identity.** Each time its access token expires (every 15
minutes by default) the browser renews the session in the background. An SSO session is not renewed,
and ends, once its provider is disabled or its identity is disconnected. Turning a provider off ends
every session that came from it at once (the admin page says how many; the API returns the count in
the `X-Revoked-Sessions` header of the `PATCH`). Turning it on again does not bring them back:
people sign in again. Password sessions and sessions from other providers are not affected.

### The session check

Specivo issues its own session tokens. Without help it would not ask the IdP again until the
session reaches its maximum length. With **Check sessions at the provider when they are renewed**
on (the default), it keeps the refresh token the IdP issued at sign-in, encrypted, and presents it
to the IdP every time it renews the session, at most once per `SSO_IDP_RECHECK_MINUTES` (by default
the access-token lifetime, 15 minutes):

- the IdP accepts it: the session is renewed (and keeps the new refresh token, if the IdP rotates
  it);
- the IdP refuses it (`invalid_grant`): the person was disabled, deleted or signed out there, or
  their IdP session expired. The Specivo session ends at once, and the next page they open is the
  login page. The security audit log records `sso_session_rejected`;
- the IdP cannot be reached or answers with another error: **If the provider cannot be reached**
  decides, and the log has an `sso.session.idp_check_failed` line.

This works with any OpenID Connect IdP that issues refresh tokens, without back-channel logout.
API keys are not affected by it.

**When it cannot work.** Some IdPs issue a refresh token only when Specivo asks for the
`offline_access` scope (authentik, Microsoft Entra ID, Okta and others); add it to **Scopes** for
them. Keycloak issues one for `openid` already, and `offline_access` there creates offline sessions
that outlive a Keycloak sign-out, so do not add it for Keycloak. Specivo tries the refresh token
once right after each sign-in. If the IdP sent none, or refused it (the client may not use the
refresh token grant), that person's sessions are renewed as before, and Test connection shows why.
Nobody is signed out because the check cannot work.

**The IdP's own session timeouts now apply.** A refresh token usually lives only as long as the
person's session at the IdP. When that session idles out or reaches its maximum at the IdP (in
Keycloak: **SSO Session Idle**, 30 minutes by default, and **SSO Session Max**, 10 hours), the next
Specivo renewal is refused and the person signs in again. Raise those timeouts at the IdP, or turn
the check off for the provider, if that is too often.

**The token belongs to the IdP that issued it.** The stored refresh token is only ever sent to the
issuer, token endpoint and client ID it came from. Changing the provider's issuer, discovery URL or
client ID drops the stored tokens of its open sessions, and so does a new token endpoint in the
IdP's discovery document. Those sessions are then renewed without the check until the person signs
in again.

**Signing out of Specivo.** The **Sign out** button in the header is a form that posts to
`/logout/`. A plain link or bookmark to `/logout/` opens a "Sign out?" confirmation page and does
not sign anyone out. If the session came from an SSO provider, **Signing out of Specivo also signs
out at the provider** is on, and the IdP has an end-session endpoint, the browser is sent to the IdP
to end its session too. It then returns to `{APP_URL}/login/?logged_out=1`. That address is fixed,
so register it as an allowed post-logout redirect at the IdP. If the IdP is down or the setting is
off, the person is simply signed out of Specivo.

**Back-channel logout** is off by default. When it is on, the IdP can tell Specivo directly (server
to server) that a person's session ended, and Specivo ends the matching sessions at once. Register
the **Back-channel logout URI** at the IdP, tick **Accept back-channel logout from the provider**,
and make sure Specivo can be reached by the IdP at that address. Microsoft Entra ID does not support
it. Test connection shows whether the IdP advertises it.

Specivo answers a valid logout request with 200 whether or not a session matched. It answers 400 to
a missing, invalid or replayed token, 404 when the provider is unknown, disabled or has back-channel
logout off, and 503 when it cannot check the token right now, so the IdP can retry: Redis or the
IdP's keys are unreachable, or the token is signed with a key Specivo has not seen and it re-read
the IdP's keys less than a minute ago (right after a key rotation). It answers 429 to more than
60 requests a minute for one provider from one sender address (for IPv6, from one /64 network).

### What a back-channel logout ends

**When the provider ends a session** decides:

| Choice | What ends |
|---|---|
| End the matching Specivo session (default) | The Specivo sessions of the IdP session the logout names (its `sid`). A logout that names only the person (`sub`) ends all their sessions from this provider. |
| End every session of that person from this provider | Every session that came from this identity, whichever IdP session it belongs to. |
| End all of their sessions and switch off their API keys | As above; and if the account signs in only through SSO (it has no password), every one of its sessions, also from other providers, and all of its API keys, MCP keys included, are switched off. The security audit log records `user_deprovisioned` with reason `sso_backchannel`. |

The last choice never deactivates the account and never touches an account with a password or a
service account (those get the second behaviour). If the person signs in through the IdP again
later, that works, but their API keys stay off until they or an administrator switch them back on.
IdPs send the same logout for a person who signed out as for one who was disabled, so choose it
only if you accept that signing out at the IdP also switches off that person's API keys. A repeated
logout for the same session changes nothing.

## Offboarding people

Specivo has no SCIM, so it learns that someone left through the following. Use as many as your IdP
allows. The provider pages have a short runbook each:
[Keycloak](keycloak.md#offboarding), [authentik](authentik.md#offboarding).

1. **Disable the person at the IdP.** They cannot sign in again. With
   [the session check](#the-session-check) working, their Specivo session ends at its next renewal,
   usually within one access-token lifetime (15 minutes), at most `SSO_IDP_RECHECK_MINUTES` plus that
   lifetime. Without it, the session keeps working until you end it or it reaches the
   maximum session length (at most a week).
2. **Back-channel logout**, where the IdP supports it, ends the session when the IdP ends theirs,
   at once. Which IdP actions send one differs: Keycloak sends it when an administrator signs the
   person out (not when they are disabled), authentik when the person is disabled, deleted or
   signed out. With **End all of their sessions and switch off their API keys** it also switches
   off their API keys (see [What a back-channel logout ends](#what-a-back-channel-logout-ends)).
3. **A refused sign-in** because they lost a required group, their email domain no longer
   qualifies, or a group overage hides their groups (`sso_groups_overage`), ends the remaining
   sessions that came from that identity.
4. **Deactivate the user in Specivo.** On the user's page in **Administration → Users**, press
   **Deactivate account** (or call `POST /api/v1/admin/users/{id}/deactivate/`). This works for
   every account, with or without SSO, service accounts included. In one step it ends every
   session (the browser is refused on its next request) and switches off all of their API keys.
   The keys are kept, switched off, so their history stays readable. **Reactivate account** lets
   the person sign in again, but their API keys stay off: they switch them back on from their API
   keys page, or they or an administrator create new ones. You cannot deactivate your own account or the last active administrator.
5. **Disconnect their identity** from the user's admin page to end the sessions that came from it.

Each provider also has the option **Deactivate the Specivo account when a sign-in is refused by the
required groups or allowed email domains** (`deactivate_on_access_loss`, off by default). With it
on, a refused sign-in (step 3, a group overage included) deactivates the account as in step 4, but only for an account
already connected to that identity. Specivo never deactivates a service account this way, nor the
last active administrator (it logs a warning instead).

**API keys.** People who sign in through SSO can create API keys (on by default). A key does not
pass through the IdP, so neither the session check nor an ordinary back-channel logout stops it: it
keeps working after the IdP disables the person, until the key expires, you deactivate the user, or
a back-channel logout with **End all of their sessions and switch off their API keys** switches it
off. That is the trade-off of keeping keys for SSO users: agents and scripts keep working across an
IdP outage or a forgotten group, but offboarding needs one of those steps. Turn off **Users who sign
in through SSO may use API keys** under **Administration → Settings → Authentication** if you do not
want that.
It applies to accounts with no password and a connected identity: they cannot create keys, and
their existing keys are refused while the setting is off. Whatever this setting says, such accounts
cannot get a password through **Forgot password**; an administrator can still set one deliberately. Users with a password
and service accounts are not affected.

An administrator's API key can read the single sign-on configuration through the admin API
(`/api/v1/admin/sso/...`), but cannot change it. Creating, editing or deleting a provider or its
group mappings, and saving the authentication settings, need an administrator signed in to Specivo;
an API key gets 403 `api_key_not_allowed`. A leaked key therefore cannot add an identity provider
that maps its own users to administrators. The same goes for every admin action that hands out a
way to sign in, such as resetting a password; see
[What an administrator's key cannot do](../../ai-agents/api-keys.md#what-an-administrators-key-cannot-do).

## Authentication settings

**Administration → Settings → Authentication** applies to the whole instance:

| Setting | What it does |
|---|---|
| Hide the password form on the login page | Replaces the form with a link, **Sign in with a local account**, to `/login/?local=1`. With no enabled provider the form is always shown. |
| Only administrators may sign in with a password | Everyone else must use SSO. The link above still shows the form, but a password sign-in by a non-administrator is refused. Every refused password sign-in then gets the same "Invalid login or password" answer, also for an administrator account that is locked, deactivated or not verified, so the form does not reveal who is an administrator. The reason is in the `specivo.sso` log (`sso.local_login.refused_admin`). Wrong passwords still lock an administrator out for a while; they can still sign in through SSO meanwhile. |
| Users who sign in through SSO may use API keys | On by default. |
| Redirect the login page to | Sends visitors straight to one provider. `/login/?local=1` bypasses it, and so does a signed-in visitor, a sign-out, a password reset and any SSO error, so a failing provider cannot loop. |

If you lock yourself out of these settings, see
[Getting back in](../configuration.md#single-sign-on-getting-back-in).

## Settings in the environment

These are set in `.env` like other [configuration](../configuration.md). They apply to every provider.

| Variable | Default | Purpose |
|---|---|---|
| `APP_URL` | `http://localhost:8030` | The public address of Specivo. Redirect and logout addresses are built from it. Set it before you configure a provider. |
| `SSO_FORCE_LOCAL_LOGIN` | `false` | Break-glass switch: always show the password form, never redirect, ignore the hide and admins-only settings. |
| `SSO_ALLOW_INSECURE_HTTP` | `false` | Allow `http://` IdP addresses. For a disposable test IdP only. Never use it on a real one: tokens and secrets travel in clear text. |
| `SSO_HTTP_TIMEOUT_SECONDS` | `5` | How long each request to the IdP may take. |
| `SSO_IDP_RECHECK_MINUTES` | access-token lifetime (15) | How often a session renewal asks the IdP at most, per session. See [The session check](#the-session-check). `0` asks at every renewal. |
| `SSO_CLOCK_SKEW_SECONDS` | `60` | How far the clocks of Specivo and the IdP may differ when checking token times. Values above 300 are lowered to 300. Fix the clocks (use NTP) rather than raising this. |
| `SPECIVO_SSO_SECRET_<NAME>` | none | A client secret you point a provider at. |

## Upgrading to a release with SSO

- **Set `APP_URL`** before you configure the first provider.
- **`/logout/` now needs a POST.** The header link is a button now. Bookmarks to `/logout/` show a
  confirmation page.
- **`SECRET_KEY`**: if you rotate it later, re-enter stored client secrets (see
  [The client secret](#the-client-secret)).
- **Existing sessions** keep working.
- **The session check** is on for every provider after the upgrade, existing ones included. Nobody
  is signed out by it: sessions open at the upgrade carry no IdP refresh token and are renewed as
  before; sessions started afterwards are checked if the IdP issues refresh tokens (add
  `offline_access` to **Scopes** where the IdP needs it). Press **Test connection** after the first
  sign-in to see whether it works.

## Troubleshooting

When a sign-in fails, the person lands on the login page with a message. For their own account
page (a failed **Connect**) they land on their **Profile** instead. The message never repeats anything the IdP
said, so use the code and the log. The code is in the address as `?sso_error=<code>`. The reason is in
the Specivo log. The `api` container writes one JSON object per line (plain text with
`DEBUG=true`). SSO lines come from the logger `specivo.sso`, and their message starts with the
event name, such as `sso.id_token.rejected ... reason=wrong_issuer` or `sso.login.refused_policy`.
To see them: `docker compose logs api | grep specivo.sso`. Logs never contain tokens or secrets.

| Code | What the person sees | What to check |
|---|---|---|
| `sso_state_invalid` | "Your sign-in expired or was started in another browser." | The sign-in took longer than 10 minutes, the person used the back button, opened the link in a second browser or tab group, or blocks cookies. The cookie is scoped to `/auth/oidc/`. Behind a proxy, check that it does not strip cookies. Retry. |
| `sso_idp_error` | "The identity provider refused the sign-in." | The IdP answered with an error, for example `access_denied` because the user is not assigned to the application, or it refused the authorization code. The same message covers a token request that failed. Check the client secret, the redirect URI and the user's access at the IdP. The log has the IdP's error code. |
| `sso_idp_unreachable` | "The identity provider is unavailable." | Specivo could not reach the IdP's discovery, keys or token endpoint within `SSO_HTTP_TIMEOUT_SECONDS`, or got a server error. It is also shown for a minute when a token names a signing key Specivo does not know and it has just re-read the IdP's keys (the IdP may have rotated them); trying again a minute later works. Check DNS, firewall and certificates from the Specivo container. Administrators can use the **Sign in with a local account** link meanwhile. |
| `sso_discovery_invalid` | "Single sign-on is not configured correctly." | The discovery document or keys are unusable: the issuer in the document is not the one you entered, an endpoint is `http://`, or there are no usable signing keys. Press **Test connection**. |
| `sso_provider_misconfigured` | "Single sign-on is not configured correctly." | The client secret cannot be read or is missing. Re-enter it, or check the environment variable name and that the variable is set in the containers. |
| `sso_token_invalid` | "Single sign-on failed. Please try again." | The ID token failed a check. See the reason in the log: a wrong issuer or audience (the issuer or client ID does not match), a bad signature, an expired token (clock drift between servers), a missing `nonce` or `sub`, a token signed with an algorithm Specivo does not accept (such as `HS256`), or a missing or too old `auth_time` when `max_age` was sent (always for **Connect**: the IdP did not make the person sign in again). |
| `sso_unavailable` | "Single sign-on is temporarily unavailable." | Specivo could not reach Redis, which it needs to store sign-in state. Local sign-in keeps working. Check the `redis` container. |
| `sso_account_not_provisioned` | "No account here is connected to your identity provider account." | No account is connected, automatic linking did not apply and JIT creation is off. Turn on **Create an account on first sign-in**, connect the account from the person's profile, or check the [linking conditions](#linking-an-existing-account). |
| `sso_account_disabled` | "This account is disabled." | The connected user is deactivated, locked by an administrator, has not verified their email address yet, or is a service account. Check **Administration → Users**. |
| `sso_email_missing` | "The identity provider did not send an email address." | JIT needs an email. Add the `email` scope and claim at the IdP, or set the **Email claim** to the right name. For Entra ID, add the `email` optional claim. |
| `sso_email_conflict` | "An account with this email address already exists." | JIT would create a second account with an email that exists. The person should sign in with their password and connect the account from their profile. |
| `sso_identity_in_use` | "This identity provider account is already connected to another account." | Seen when connecting. The IdP identity belongs to another Specivo user. Disconnect it there first. |
| `sso_provider_already_linked` | "An account at this identity provider is already connected to your profile." | Seen when connecting. The user already has an account at this provider. |
| `sso_domain_not_allowed` | "Your email domain is not allowed to sign in here." | The email domain is not in the list, or the email is not verified, or the provider is not set to trust email verification, or the IdP sent no email. See [Limiting who can sign in](#limiting-who-can-sign-in). |
| `sso_not_in_required_group` | "Your identity provider account is not in a group that may sign in here." | The person is in none of the required groups, or the token has no groups claim. Check the **Groups claim** path, the IdP's group mapper, the scopes, and that group names match exactly, including case. |
| `sso_groups_overage` | "Your identity provider sent too many groups to read." | Entra ID overage. See [Microsoft Entra ID](entra-id.md#group-overage). |
| `sso_last_sign_in_method` | "This is your only way to sign in." | Seen when disconnecting. The person has no password and no other connected account. Set a password or connect another account first. |
| `sso_access_denied` | "Single sign-on failed. Please try again." | The general form of the three policy codes above. You should see one of the specific codes instead. |
| `sso_token_exchange_failed` | Shown as `sso_idp_error`. | The IdP's token endpoint refused the authorization code. Check the client secret, redirect URI and PKCE settings at the IdP. |

Other things administrators run into:

- **The IdP says "invalid redirect URI".** It must equal the address on the **Connection** card
  character for character, including the scheme (`https`), host, port and trailing slash. Check
  `APP_URL`.
- **The provider list shows "Unreadable, re-enter it".** `SECRET_KEY` changed. Enter the secret again.
- **A person sees the 404 page when they click a provider button.** The provider is disabled, or its
  slug changed and the old link is cached.
- **A person's display name or language changed, but their email did not.** Expected unless the
  provider is trusted for email verification.
- **A person is signed out of Specivo after 24 hours.** That is **Maximum session length**.
- **A person is signed out after a break of half an hour or so.** Their session at the IdP idled out,
  and the session check found that at the next renewal. Raise the IdP's idle timeout, or turn off
  the session check for the provider. See [The session check](#the-session-check).
- **Test connection says the provider refused the refresh token.** The client may not use the
  refresh token grant at the IdP. Allow it (authentik: **Grant Types → Refresh token**).
