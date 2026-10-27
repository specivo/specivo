---
description: Short notes for connecting Specivo to Okta, Google, Zitadel and Authelia over OpenID Connect — issuer format, groups claim and logout support.
---

# Okta, Google, Zitadel and Authelia

Specivo speaks standard OpenID Connect, so any provider that publishes a discovery document works.
These notes cover the points where these four differ. The steps are the ones in
[Single sign-on](index.md#setting-up-a-provider-the-outline). Unlike the
[Keycloak](keycloak.md), [authentik](authentik.md) and [Microsoft Entra ID](entra-id.md) pages,
they have not been walked through screen by screen, and the IdP's screens change. Check each
setting against your provider's current documentation, and use **Test connection** in Specivo: it
shows which logout features and client authentication methods the provider advertises.

In every case register the redirect URI exactly as the Specivo form shows it, use PKCE with `S256`
if the provider asks, and leave **Accept back-channel logout** off unless Test connection says the
provider supports it.

## Okta

- **Application**: create an **OIDC - OpenID Connect** app of type **Web Application**, with the
  **Authorization Code** grant. Add the redirect URI under **Sign-in redirect URIs** and
  `{APP_URL}/login/?logged_out=1` under **Sign-out redirect URIs**.
- **Issuer**: for the organization authorization server it is `https://example.okta.com`. For a
  custom authorization server, such as the one named `default`, it is
  `https://example.okta.com/oauth2/default`. Use the address your server shows in its discovery
  document; the two issuers differ and tokens carry only the one you used.
- **Groups**: Okta does not send groups unless asked. In the application's **Sign On** settings,
  under **OpenID Connect ID Token**, set **Groups claim type** to **Filter**, **Groups claim
  name** to `groups`, and a filter such as **Matches regex** `.*` (or a prefix that selects
  only the groups for Specivo). On a custom authorization server you add the claim under **Security
  → API → Authorization Servers → *server* → Claims** instead. Add the `groups` scope to the
  Specivo **Scopes** if you configure it that way. Set the Specivo **Groups claim** to `groups`.
- **Logout**: Okta supports sign-out at the provider (RP-initiated logout). Leave back-channel
  logout off unless Test connection says it is supported.

## Google

- **Client**: in the Google Cloud console, **APIs & Services → Credentials → Create credentials →
  OAuth client ID**, type **Web application**. Add the redirect URI under **Authorized redirect
  URIs**. The consent screen can be **Internal** if you use Google Workspace and want only your
  organization.
- **Issuer**: `https://accounts.google.com`.
- **Groups**: Google sends **no groups claim**. Do not use required groups, administrator groups or
  group mappings: with a required group set, nobody could sign in. Limit access with **Allowed
  email domains** instead.
- **Allowed email domains**: for Google Workspace accounts, Specivo checks Google's `hd` (hosted
  domain) claim, which Google sets from the account's Workspace domain. This is the only IdP where
  `hd` is used. For an account with no `hd`, such as a personal Google account, the check falls
  back to the verified email's domain, which needs **This provider really verifies email
  addresses** on. Google does set `email_verified` for its accounts, so turning that on is
  reasonable here. Prefer the **Internal** consent screen as well, so only your Workspace
  users can reach the client.
- **Logout**: Google publishes no end-session endpoint and no back-channel logout. Test connection
  warns about the first if sign-out at the provider is on. You can turn **Signing out of
  Specivo also signs out at the provider** off. Signing out of Specivo then does not sign the person
  out of Google. Use a short **Maximum session length** instead.

## Zitadel

- **Application**: in a project, create an application of type **Web** and the **Code**
  authentication method, with PKCE if you want to use a public client (**Client authentication**
  `none` in Specivo) or a secret (`client_secret_basic`) for a confidential one. Add the redirect
  URI and `{APP_URL}/login/?logged_out=1` as the post-logout URI. In **Token Settings**, enable
  **User roles inside ID Token** and **User info inside ID Token** if you want roles and profile
  claims in the ID token. Otherwise turn on **Also read claims from the userinfo endpoint** in
  Specivo.
- **Issuer**: the address of your Zitadel instance, for example `https://zitadel.example.com`, with
  no path and no trailing slash.
- **Groups**: Zitadel sends roles in the claim `urn:zitadel:iam:org:project:roles`, as an object
  keyed by role name rather than a list. Specivo reads a claim only if it is a list of strings or
  one string, so this claim cannot be used as it is. If you need groups, use a Zitadel Action
  to add a claim with a plain list of role names and point the Specivo **Groups claim** at it.
  Otherwise leave groups unused and control access with Zitadel's own project settings.
- **Logout**: Zitadel provides an end-session endpoint. Use Test connection to see whether it
  advertises back-channel logout on your version.

## Authelia

- **Client**: Authelia configures clients in its configuration file under
  `identity_providers.oidc.clients`. Use the client ID and the *plain* secret in Specivo. Authelia
  stores only a hash of the secret, so keep the plain value in your password manager. Set
  `redirect_uris` to the Specivo redirect URI, `scopes` to `openid`, `profile`, `email` and
  `groups`, `token_endpoint_auth_method` to `client_secret_basic` (or the method you choose in
  Specivo), and require PKCE with `S256`.
- **Issuer**: the public address of Authelia, for example `https://auth.example.com`, with no
  trailing slash.
- **Groups**: with the `groups` scope, Authelia sends the user's groups in the `groups` claim. Use
  `groups` as the Specivo **Groups claim**. Group names are exactly as in Authelia's user database.
- **Logout**: Authelia has no RP-initiated end-session endpoint and no back-channel logout in
  most versions. Turn off **Signing out of Specivo also signs out at the provider** and **Accept
  back-channel logout** if Test connection warns about them, and keep **Maximum session length**
  short.
