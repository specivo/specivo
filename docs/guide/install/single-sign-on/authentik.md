---
description: Connect Specivo to authentik — create an OAuth2/OpenID provider and application, allow the authorization code grant, pick a signing key, keep the default subject mode, send groups, set up logout, and fill in the provider in Specivo.
---

# authentik

This page connects Specivo to authentik. Read [Single sign-on](index.md) first for the concepts and
what each Specivo setting does. The steps were checked against authentik 2026.8 with the blueprint
Specivo's compatibility tests use: sign-in, accounts created on first sign-in, group mapping,
sign-out at authentik and back-channel logout all work.

Sample values: Specivo at `https://specivo.example.com` (`APP_URL`), authentik at
`https://authentik.example.com`, application slug `specivo`, provider slug `authentik`.

## 1. Create the provider and the application in authentik

In the authentik admin interface, go to **Applications → Applications** and press **New
Application**. The wizard creates the application and its provider together. You can also create
the provider first under **Applications → Providers** and then an application that uses it.

**Application**

- **Name**: `Specivo`. **Slug**: `specivo`. The slug becomes part of the issuer.

**Provider: OAuth2/OpenID Provider**

- **Authorization Flow**: `default-provider-authorization-implicit-consent` skips the consent
  screen. Choose the explicit consent flow if you want people to confirm each time.
- Under **Protocol settings**:
    - **Client Type**: **Confidential**.
    - **Client ID**: `specivo`, or the generated one. **Client Secret**: copy the generated one.
    - **Grant Types**: tick **Authorization Code** only. The form ticks almost every grant type by
      default; Specivo needs none of the others. Do not leave the list empty: a provider without the
      authorization code grant (for example one created through the API or a blueprint without
      `grant_types`) refuses every sign-in, and people see `sso_idp_error`.
    - **Redirect URIs/Origins**: add `https://specivo.example.com/auth/oidc/authentik/callback/`
      with matching mode **Strict** and type **Authorization**. Add
      `https://specivo.example.com/login/?logged_out=1` with matching mode **Strict** and type
      **Post Logout**: that is the address Specivo returns to after sign-out. No regular
      expressions or wildcards.
    - **Signing Key**: choose a certificate key pair, for example the built-in *authentik
      Self-signed Certificate*. **This is required.** Without a signing key authentik signs tokens
      with the client secret (`HS256`), and Specivo rejects those. With a key they are `RS256`.
- Under **Advanced flow settings**, keep the **Invalidation Flow**
  `default-provider-invalidation-flow`. It is what runs when Specivo sends someone to authentik to
  sign out (see [Logout](#3-logout)).
- Under **Advanced protocol settings**:
    - **Scopes**: keep the default mappings for `openid`, `email` and `profile`. The `profile`
      mapping is what puts a `groups` claim in the token.
    - **Subject Mode**: keep **Based on the User's hashed ID** (the default).
    - **Include claims in id_token**: on (the default), so groups and the email arrive in the ID
      token.
    - **Issuer mode**: keep **Each provider has a different issuer, based on the application
      slug** (the default).

!!! warning "Do not change the subject mode later"
    The subject authentik sends is how Specivo recognises a person. The default, the hashed user
    ID, is a 64-character value that stays the same when someone changes their username or email.
    Switching to another mode gives every person a different subject, and everybody has to connect
    their account again. Pick the mode before the first sign-in and leave it.

**Who may use the application.** On the application's **Policy Bindings** tab, bind the groups or
users that may open Specivo. This is authentik's own access control, and it is enforced before
Specivo sees anyone. Use Specivo's **Required groups** as a second layer if you want it.

## 2. Groups

authentik sends the names of the user's groups in the `groups` claim, as a list of strings, when
the application uses the default `profile` scope mapping. Nothing more is needed. In Specivo the
**Groups claim** is `groups` (the default). **Required groups**, **Administrator groups** and
**Group mappings** use the group names exactly as they are in authentik, for example
`specivo-users` and `specivo-admins`.

The claim lists all of a person's groups, not only those bound to the application. Name the
groups you use in Specivo explicitly. A person in no group gets an empty list, so **Required
groups** refuses them with `sso_not_in_required_group`.

## 3. Logout

**Sign-out at authentik** works with the settings above. When a person signs out of Specivo,
Specivo sends the browser to the provider's end-session endpoint
(`https://authentik.example.com/application/o/specivo/end-session/`) with the ID token and the
post-logout address. authentik runs the invalidation flow, which ends the authentik session without
asking anything, and sends the browser back to `https://specivo.example.com/login/?logged_out=1`.
The post-logout address must be registered with type **Post Logout** (step 1), or authentik does
not send the browser back.

**Back-channel logout** (optional) ends Specivo sessions when the authentik session ends: when the
person signs out at authentik, or an administrator ends their sessions in authentik. Under
**Protocol settings**, set the **Logout URI** to
`https://specivo.example.com/auth/oidc/authentik/backchannel-logout/` and the **Logout Method** to
**Back-channel**. The authentik **worker** sends these requests, so the worker must be able to
reach Specivo at that address. In Specivo, tick **Accept back-channel logout from the provider**.

## 4. Add the provider in Specivo

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
| Accept back-channel logout | On, if you set it up in step 3 |

The issuer **ends with a slash** and contains the application slug. Copy it from the **Issuer**
shown on the provider's page in authentik rather than typing it: a missing slash makes the issuer
differ and the sign-in fails.

Save, press **Test connection**, tick **Enabled** and save again. Test connection should report an
end-session endpoint and back-channel logout support. See
[Single sign-on](index.md#who-gets-an-account) for accounts, JIT and groups.

## Notes for authentik

- **Email verification.** authentik's default `email` scope mapping always sends
  `email_verified: false`. With it, leave **This provider really verifies email addresses** off:
  it changes nothing, because Specivo never sees a verified address. In practice this means a
  person's Specivo email does not follow authentik, automatic linking never applies, accounts
  created on first sign-in have an unverified email, and **Allowed email domains** refuses
  everyone. Use **Required groups** or authentik's policy bindings to limit who can sign in, and let
  people connect existing accounts from their profile. Only if authentik's sources really verify
  email addresses, replace the default `email` mapping with your own that returns
  `email_verified: true`, and then turn the Specivo setting on.
- **Language and time zone.** The default mappings send neither `locale` nor `zoneinfo`. To set a
  person's language from authentik, add a scope mapping that returns a `locale` claim and select it
  on the provider.
- **Test users.** Try both a person in the groups you map and one who is in none of them, and check
  that the second is refused (`sso_not_in_required_group`) if you set required groups.

## If it does not work

| Symptom | Check |
|---|---|
| authentik: "Redirect URI Error" | The redirect URI is not an exact (strict) match for the one on the Specivo form. |
| `sso_idp_error` right after pressing the button, without an authentik sign-in page | The provider does not allow the **Authorization Code** grant type. The Specivo log shows `invalid_request`. |
| `sso_idp_error` after signing in | The secret is wrong, or the user has no access to the application (policy bindings). |
| `sso_token_invalid` | The signing key is missing (HS256 tokens are refused), or the issuer differs from authentik's by the trailing slash or the application slug. |
| Groups missing | The provider lost its `profile` scope mapping, or **Include claims in id_token** is off. |
| Sign-out leaves the authentik session open, or authentik does not return to Specivo | The post-logout address is not registered with type **Post Logout**, the invalidation flow was changed, or **Signing out of Specivo also signs out at the provider** is off in Specivo. |
| Back-channel logout does nothing | The authentik worker cannot reach the logout URI, or **Accept back-channel logout from the provider** is off. |

The full list is in the [error table](index.md#troubleshooting).
