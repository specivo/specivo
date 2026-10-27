---
description: Connect Specivo to authentik — create an OAuth2/OpenID provider and application, pick a signing key, keep the default subject mode, send groups, and fill in the provider in Specivo.
---

# authentik

This page connects Specivo to authentik. Read [Single sign-on](index.md) first for the concepts and
what each Specivo setting does. The values below follow the blueprint Specivo's compatibility
tests use.

Sample values: Specivo at `https://specivo.example.com` (`APP_URL`), authentik at
`https://authentik.example.com`, application slug `specivo`, provider slug `authentik`.

## 1. Create the provider and the application in authentik

In the authentik admin interface, go to **Applications → Applications → Create with Provider**
(or create the provider first under **Applications → Providers**).

**Application**

- **Name**: `Specivo`. **Slug**: `specivo`. The slug becomes part of the issuer.

**Provider: OAuth2/OpenID Provider**

- **Authorization flow**: `default-provider-authorization-implicit-consent` skips the consent
  screen. Choose the explicit consent flow if you want people to confirm each time.
- **Client type**: **Confidential**.
- **Client ID**: `specivo`, or the generated one. **Client Secret**: copy the generated one.
- **Redirect URIs/Origins**: add `https://specivo.example.com/auth/oidc/authentik/callback/` with
  matching mode **Strict**. Also add `https://specivo.example.com/login/?logged_out=1` with
  matching mode **Strict** and, where your authentik version offers the choice, the type
  **Logout**. That is the address Specivo returns to after sign-out. No regular expressions or
  wildcards.
- **Signing Key**: choose a certificate key pair, for example the built-in *authentik
  Self-signed Certificate*. **This is required.** Without a signing key authentik signs tokens
  with the client secret (`HS256`), and Specivo rejects those. With a key they are `RS256`.
- **Scopes**: keep the default mappings for `openid`, `email` and `profile`. The `profile`
  mapping is what puts a `groups` claim in the token.
- **Subject mode**: keep **Based on the hashed User ID** (the default).
- **Issuer mode**: **Each provider has a different issuer** (the default).
- **Include claims in ID token**: on (the default), so groups and the email arrive in the ID token.

!!! warning "Do not change the subject mode later"
    The subject authentik sends is how Specivo recognises a person. The default, the hashed user
    ID, stays the same when someone changes their username or email. Switching to a mode based on
    the email or the username gives every person a different subject, and everybody has to connect
    their account again. Pick the mode before the first sign-in and leave it.

**Back-channel logout** (optional). In the provider's advanced protocol settings, set the **Logout
URI** to `https://specivo.example.com/auth/oidc/authentik/backchannel-logout/` and the **Logout
method** to **Back-channel**. authentik then notifies Specivo when the person's authentik session
ends.

**Who may use the application.** Under the application's **Policy / Group / User Bindings**, bind
the groups or users that may open Specivo. This is authentik's own access control, and it is
enforced before Specivo sees anyone. Use Specivo's **Required groups** as a second layer if you
want it.

## 2. Groups

authentik sends the names of the user's groups in the `groups` claim, as a list of strings, when
the application uses the default `profile` scope mapping. Nothing more is needed. In Specivo the
**Groups claim** is `groups` (the default). **Required groups**, **Administrator groups** and
**Group mappings** use the group names exactly as they are in authentik, for example
`specivo-users` and `specivo-admins`.

The claim lists all of a person's groups, not only those bound to the application. Name the
groups you use in Specivo explicitly.

## 3. Add the provider in Specivo

**Administration → Single sign-on → + New provider**:

| Field | Value |
|---|---|
| Button label | `authentik` |
| Slug | `authentik` |
| Issuer | `https://authentik.example.com/application/o/specivo/` |
| Client ID | The client ID from the provider |
| Client authentication | `client_secret_basic` |
| Client secret | The client secret from the provider |
| Scopes | `openid profile email` |
| Groups claim | `groups` |
| Accept back-channel logout | On, if you set it up in step 1 |

The issuer **ends with a slash** and contains the application slug. Copy it from the provider's
**OpenID Configuration Issuer** field in authentik rather than typing it: a missing slash makes the
issuer differ and the sign-in fails.

Save, press **Test connection**, tick **Enabled** and save again. See
[Single sign-on](index.md#who-gets-an-account) for accounts, JIT and groups.

## Notes for authentik

- **Language.** authentik does not send a `zoneinfo` claim. A user's language is sent as `locale`
  only if you add it, for example with a property mapping that reads a user attribute. The
  tests use the attribute `settings.locale` for this.
- **Email verification.** authentik decides what `email_verified` means. Turn on **This provider
  really verifies email addresses** only if you know your sources verify email.
- **Test users.** Try both a person in the groups you map and one who is in none of them, and check
  that the second is refused (`sso_not_in_required_group`) if you set required groups.

## If it does not work

| Symptom | Check |
|---|---|
| authentik: "Redirect URI Error" | The redirect URI is not an exact (strict) match for the one on the Specivo form. |
| `sso_token_invalid` | The signing key is missing (HS256 tokens are refused), or the issuer differs from authentik's by the trailing slash or the application slug. |
| `sso_idp_error` | The secret is wrong, or the user has no access to the application (policy bindings). |
| Groups missing | The provider lost its `profile` scope mapping, or **Include claims in ID token** is off. |
| Sign-out leaves the authentik session open | RP-initiated logout needs the authentik invalidation flow to accept the post-logout address from step 1. |

The full list is in the [error table](index.md#troubleshooting).
